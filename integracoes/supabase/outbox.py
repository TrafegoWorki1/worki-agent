"""Outbox: entrega separada do raciocinio.

O bug do relay antigo: ele respondia no WhatsApp e DEPOIS marcava a fala.
Se o PATCH falhasse (rede, timeout, container morrendo), a proxima rodada
pegava a mesma fala e rodava o Hermes de novo — criando branch e commit
duplicados.

Aqui a ordem e invertida e obrigatoria:

    1. o worker termina o raciocinio
    2. grava o resultado na `outbox`  <- o trabalho esta seguro
    3. so entao envia

Se o envio falhar, o item fica `pendente` e a proxima rodada reenvia
**a mensagem ja pronta**. O Hermes nao roda de novo, porque o passo 1 ja
foi feito.

## Sobre 'incerto'

A Evolution devolve 201 quando aceita. Se a chamada estourar o timeout
depois do envio, nao da para saber se a mensagem chegou. Reenviar as cegas
significa mandar a mesma resposta duas vezes para o Herickson.

Por isso esse caso vira `incerto`, nao `falhou`. O reconciliador tenta
descobrir se a mensagem saiu (pela lista de mensagens da instancia) antes
de decidir. Sem exactly-once na API externa — isso e o melhor que da para
fazer, e fingir o contrario e pior que dizer que nao sabe.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integracoes.supabase.env import chave  # noqa: E402
from integracoes.supabase.queue import _rpc  # noqa: E402

SB = chave("SUPABASE_URL").rstrip("/")
SR = chave("SUPABASE_SERVICE_ROLE_KEY")
EV_URL = chave("EVOLUTION_API_URL").rstrip("/")
EV_KEY = chave("EVOLUTION_API_KEY")
# Grafia 'agent-domintante' confirmada na Instances API do deploy atual.
# NAO 'corrigir' para 'dominante': o nome da instancia e do deploy.
INSTANCE = chave("EVOLUTION_INSTANCE") or "agent-domintante"

HEADERS = {
    "apikey": SR,
    "Authorization": f"Bearer {SR}",
    "Content-Type": "application/json",
}


def agora() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Entrega:
    id: int
    destino: str
    response_text: str
    status: str
    attempts: int
    conversation_id: int
    task_id: int | None = None

    @classmethod
    def de_row(cls, row: dict) -> "Entrega":
        return cls(
            id=row["id"],
            destino=row["destino"],
            response_text=row["response_text"],
            status=row["status"],
            attempts=row.get("attempts", 0),
            conversation_id=row["conversation_id"],
            task_id=row.get("task_id"),
        )


class ResultadoEnvio:
    """Tres estados, nao dois.

    ENTREGUE   — a Evolution aceitou e temos o id.
    FALHOU     — recusou com erro claro; reenviar faz sentido.
    INCERTO    — timeout ou erro de rede depois do envio; reenviar pode
                 duplicar, entao reconcilia antes.
    """

    ENTREGUE = "entregue"
    FALHOU = "falhou"
    INCERTO = "incerto"


def enviar(destino: str, texto: str, timeout: int = 45) -> tuple[str, str | None]:
    """Envia texto. Devolve (estado, provider_message_id).

    `destino` e o numero, so digitos, sem @s.whatsapp.net. A Evolution
    aceita assim e a resposta vai para quem merece — em grupo, o
    `destino` ja vem resolvido para o participante autorizado.
    """
    req = urllib.request.Request(
        f"{EV_URL}/message/sendText/{INSTANCE}",
        data=json.dumps({"number": destino, "text": texto}).encode(),
        headers={"apikey": EV_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # A Evolution devolve 201 Created em envio aceito. Aceitar
            # so 200 faz todo envio parecer falha e a resposta some.
            if r.status not in (200, 201):
                return ResultadoEnvio.FALHOU, None
            bruto = r.read().decode()
            try:
                dados = json.loads(bruto) if bruto else {}
            except json.JSONDecodeError:
                return ResultadoEnvio.ENTREGUE, None
            # O id vem em chaves diferentes conforme a versao.
            for chave_id in ("key", "messageId", "message_id", "id"):
                v = dados.get(chave_id)
                if isinstance(v, dict):
                    v = v.get("id")
                if v:
                    return ResultadoEnvio.ENTREGUE, str(v)
            return ResultadoEnvio.ENTREGUE, None
    except urllib.error.HTTPError as e:
        corpo = e.read().decode()[:200]
        print(f"  sendText recusou: HTTP {e.code} {corpo}")
        return ResultadoEnvio.FALHOU, None
    except urllib.error.URLError as e:
        # A requisicao pode ter saido antes da rede cair. Incerto.
        print(f"  sendText erro de rede: {e.reason}")
        return ResultadoEnvio.INCERTO, None
    except TimeoutError:
        print("  sendText timeout — nao da para saber se saiu")
        return ResultadoEnvio.INCERTO, None


def _rest(metodo: str, caminho: str, body=None, headers=None):
    req = urllib.request.Request(
        f"{SB}{caminho}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers or {**HEADERS, "Prefer": "return=minimal"},
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            bruto = r.read().decode()
            return json.loads(bruto) if bruto else []
    except urllib.error.HTTPError as e:
        print(f"  {metodo} {caminho}: HTTP {e.code} {e.read().decode()[:150]}")
        return None


class Outbox:
    """Grava a intencao de enviar e entrega sem refazer o trabalho."""

    def enfileirar(
        self,
        conversation_id: int,
        destino: str,
        texto: str,
        task_id: int | None = None,
    ) -> int:
        """Grava a resposta ANTES de enviar. Devolve o id da linha.

        Texto vazio nao entra: uma resposta sem conteudo e um no-op que
        ainda assim marcaria a entrega como feita.
        """
        if not texto or not texto.strip():
            raise ValueError("texto vazio nao vai para a outbox")
        linhas = _rest(
            "POST",
            "/rest/v1/outbox",
            body={
                "conversation_id": conversation_id,
                "task_id": task_id,
                "destino": destino,
                "response_text": texto,
                "status": "pendente",
            },
            headers={**HEADERS, "Prefer": "return=representation"},
        )
        if not linhas:
            raise RuntimeError("outbox: nao gravou a resposta")
        return linhas[0]["id"]

    def pendentes(self, limite: int = 10) -> list[Entrega]:
        # 'falhou' entra de proposito. Se so 'pendente' e 'incerto' fossem
        # lidos, um envio recusado com erro claro ficaria para sempre sem
        # entrega — a resposta se perderia e ninguem saberia. O backoff
        # fica em available_at, advanced pela RPC de tentativa.
        linhas = _rest(
            "GET",
            f"/rest/v1/outbox?status=in.(pendente,incerto,falhou)"
            f"&available_at=lte.{agora().isoformat()}"
            f"&order=created_at.asc&limit={limite}",
            headers={**HEADERS, "Prefer": "return=representation"},
        )
        return [Entrega.de_row(l) for l in linhas] if linhas else []

    def marcar(
        self,
        outbox_id: int,
        estado: str,
        provider_message_id: str | None = None,
        erro: str = "",
    ) -> None:
        corpo: dict = {"status": estado, "updated_at": agora().isoformat()}
        if estado == ResultadoEnvio.ENTREGUE:
            corpo["sent_at"] = agora().isoformat()
        if provider_message_id:
            corpo["provider_message_id"] = provider_message_id
        if erro:
            corpo["last_error"] = erro[:500]
        _rest("PATCH", f"/rest/v1/outbox?id=eq.{outbox_id}", body=corpo)

    def incrementar_tentativa(self, outbox_id: int) -> None:
        """Soma 1 em attempts.

        Nao pode ser um PATCH com valor: o PostgREST nao avalia expressao
        e gravaria a string 'attempts + 1' num integer. Por isso a RPC.
        """
        _rpc(
            "worki_outbox_tentar",
            {"p_outbox_id": outbox_id},
        )

    def entregar_pendentes(self) -> tuple[int, int, int]:
        """Entrega o que esta na fila. Devolve (entregues, falhos, incertos)."""
        entregues = falhos = incertos = 0
        for item in self.pendentes():
            self.incrementar_tentativa(item.id)
            # Ja entregue antes? Nao reenvia. Esta checagem e o que
            # segura o duplo envio quando o processo morre entre a
            # gravacao do 'entregue' e o fim da funcao.
            if item.status == "entregue":
                continue

            estado, mid = enviar(item.destino, item.response_text)

            if estado == ResultadoEnvio.ENTREGUE:
                self.marcar(item.id, estado, mid)
                entregues += 1
                print(f"  entregue outbox #{item.id} -> ...{item.destino[-4:]}")
            elif estado == ResultadoEnvio.INCERTO:
                # Nao reenviar as cegas. Espera o reconciliador.
                self.marcar(item.id, estado, erro="timeout de rede")
                incertos += 1
                print(f"  incerto outbox #{item.id} — reconciliar")
            else:
                # Recusou com erro claro: reenviar faz sentido, com backoff.
                self.marcar(item.id, estado, erro="Evolution recusou")
                falhos += 1
                print(f"  falhou outbox #{item.id} — tenta de novo")

        return entregues, falhos, incertos


if __name__ == "__main__":
    o = Outbox()
    e, f, i = o.entregar_pendentes()
    print(f"entregues={e} falhos={f} incertos={i}")
