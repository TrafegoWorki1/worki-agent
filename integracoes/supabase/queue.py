"""Fila duravel: reserva atomica, lease e conclusao.

O relay antigo lia `falas` com GET e marcava com PATCH depois de responder.
Duas workers nesse esquema rodavam o Hermes duas vezes para a mesma fala —
e o Hermes cria branch, commit e deploy. Duplicar isso custa dinheiro.

Aqui a reserva e uma RPC (worki_reservar_job) que faz o UPDATE ... WHERE
status='pendente' com `for update skip locked`. Duas workers concorrentes
disputam a mesma linha: uma ganha, a outra recebe zero rows. Nao existe
janel[a] entre ler e marcar.

`lease_owner` + `lease_until` sao a rede de seguranca: se o worker morre no
meio da tarefa (container reiniciado, OOM), a lease vence e o job volta
para a fila sozinho. Nao precisa de um limpador.

O cliente fala PostgREST, igual ao resto do repo. Sem dependencia nova.
"""

import os
import socket
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integracoes.supabase.env import chave  # noqa: E402

SB = chave("SUPABASE_URL").rstrip("/")
SR = chave("SUPABASE_SERVICE_ROLE_KEY")
HEADERS = {
    "apikey": SR,
    "Authorization": f"Bearer {SR}",
    "Content-Type": "application/json",
}

# Identidade do worker. No EasyPanel cada container tem seu nome como
# hostname, entao dois containers nunca compartilham o mesmo dono — que e
# o que faz o `where lease_owner = eu` da conclusao ser seguro.
DONO = os.environ.get("WORKI_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"


def agora() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    id: int
    message_id: int
    status: str
    attempts: int
    lease_owner: str | None = None
    lease_until: str | None = None
    last_error: str | None = None

    @classmethod
    def de_row(cls, row: dict) -> "Job":
        return cls(
            id=row["id"],
            message_id=row["message_id"],
            status=row["status"],
            attempts=row.get("attempts", 0),
            lease_owner=row.get("lease_owner"),
            lease_until=row.get("lease_until"),
            last_error=row.get("last_error"),
        )


def _rpc(nome: str, payload: dict) -> list[dict] | None:
    """Chama uma RPC. Devolve None em erro de transporte."""
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{SB}/rest/v1/rpc/{nome}",
        data=json.dumps(payload).encode(),
        headers=HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            bruto = r.read().decode()
            return json.loads(bruto) if bruto else []
    except urllib.error.HTTPError as e:
        # A RPC e SECURITY DEFINER com revoke de anon/public. Se aparecer
        # 401/403 aqui, o problema e grant, nao logica — vale dizer.
        raise RuntimeError(
            f"{nome} recusou: HTTP {e.code}. "
            f"Se 401/403, confira o revoke/grant da migracao 001. "
            f"{e.read().decode()[:200]}"
        ) from e
    except Exception as e:
        print(f"  rpc {nome} falhou: {e}")
        return None


def _rest(metodo: str, caminho: str, body=None, headers=None):
    import json
    import urllib.error
    import urllib.request

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
        print(f"  {metodo} {caminho} falhou: HTTP {e.code} {e.read().decode()[:150]}")
        return None


class Fila:
    """Reserva e conclui jobs. Uma instancia por processo."""

    def __init__(self, lease_s: int = 60):
        self.lease_s = lease_s
        self.dono = DONO

    def reservar(self, limite: int = 1) -> list[Job]:
        """Pega ate `limite` jobs. Vazio quando nao ha nada."""
        linhas = _rpc(
            "worki_reservar_job",
            {
                "p_owner": self.dono,
                "p_agora": agora().isoformat(),
                "p_lease_s": self.lease_s,
                "p_limite": limite,
            },
        )
        if not linhas:
            return []
        return [Job.de_row(l) for l in linhas]

    def concluir(self, job_id: int) -> None:
        _rpc(
            "worki_concluir_job",
            {"p_job_id": job_id, "p_owner": self.dono, "p_erro": None},
        )

    def falhar(self, job_id: int, erro: str) -> None:
        # O erro vai para o banco com os 5 primeiros digitos do contexto.
        # A mensagem completa fica no log do container, nao no banco: o
        # last_error vira saida de auditoria e nao deve carregar token.
        _rpc(
            "worki_concluir_job",
            {"p_job_id": job_id, "p_owner": self.dono, "p_erro": erro[:500]},
        )

    def renovar(self, job_id: int) -> None:
        """Estica a lease. Chamar durante tarefa longa."""
        _rpc(
            "worki_renovar_lease",
            {
                "p_job_id": job_id,
                "p_owner": self.dono,
                "p_agora": agora().isoformat(),
                "p_lease_s": self.lease_s,
            },
        )

    def recuperar(self) -> int:
        """Devolve leases vencidas para a fila. Chame no boot."""
        linhas = _rpc("worki_recuperar_leases", {"p_agora": agora().isoformat()})
        if linhas is None:
            return 0
        if isinstance(linhas, int):
            return linhas
        return len(linhas)

    # ---- mensagens ----

    def enfileirar_evento(
        self,
        conversation_id: int,
        instance: str,
        chat_jid: str,
        sender_jid: str,
        provider_message_id: str | None,
        texto: str,
        tipo_mensagem: str = "conversation",
        instance_token: str | None = None,
    ) -> tuple[int | None, bool]:
        """Grava a mensagem e cria o job, na mesma transcao.

        Devolve (job_id, ja_existia). `ja_existia` True significa evento
        duplicado da Evolution: sucesso sem job novo. O receptor responde
        200 nos dois casos — o webhook nao deve repetir.

        A transacao vem da RPC `worki_registrar_evento`, nao de dois POSTs.
        Dois POSTs sao duas transacoes: se a segunda falha, sobra uma
        mensagem sem job e ela nunca e processada.
        """
        linhas = _rpc(
            "worki_registrar_evento",
            {
                "p_conversation_id": conversation_id,
                "p_instance": instance,
                "p_chat_jid": chat_jid,
                "p_sender_jid": sender_jid,
                "p_provider_message_id": provider_message_id,
                "p_texto": texto,
                "p_tipo": tipo_mensagem,
                "p_instance_token": instance_token,
            },
        )
        if not linhas:
            return None, False
        linha = linhas[0] if isinstance(linhas[0], dict) else {"job_id": linhas[0]}
        return linha.get("job_id"), bool(linha.get("ja_existia"))

    def mensagem(self, message_id: int) -> dict | None:
        linhas = _rest(
            "GET",
            f"/rest/v1/inbound_messages?id=eq.{message_id}&select=*,"
            f"conversations(*)",
            headers={**HEADERS, "Prefer": "return=representation"},
        )
        return linhas[0] if linhas else None

    def conversa(self, conversation_id: int) -> dict | None:
        linhas = _rest(
            "GET",
            f"/rest/v1/conversations?id=eq.{conversation_id}",
            headers={**HEADERS, "Prefer": "return=representation"},
        )
        return linhas[0] if linhas else None

    def salvar_sessao(self, conversation_id: int, session_id: str) -> None:
        """Grava o session_id do Hermes na conversa.

        E o que faz a segunda mensagem da mesma conversa continuar o
        contexto. Falha aqui nao repete o job: a tarefa ja rodou, e o
        preco de perder contexto e menor que o de reexecutar.
        """
        _rest(
            "PATCH",
            f"/rest/v1/conversations?id=eq.{conversation_id}",
            body={"hermes_session_id": session_id, "updated_at": agora().isoformat()},
        )

    def marcar_lida(self, conversation_id: int) -> None:
        _rest(
            "PATCH",
            f"/rest/v1/conversations?id=eq.{conversation_id}",
            body={"ultimo_mensagem_em": agora().isoformat(),
                  "updated_at": agora().isoformat()},
        )


if __name__ == "__main__":
    f = Fila()
    print(f"worker: {f.dono}")
    print(f"leases vencidas recuperadas: {f.recuperar()}")
    pendentes = f.reservar(limite=3)
    print(f"jobs reservados: {len(pendentes)}")
    for j in pendentes:
        print(f"  #{j.id} msg={j.message_id} tentativas={j.attempts}")
        f.falhar(j.id, "inspecao manual — devolvido para a fila")
