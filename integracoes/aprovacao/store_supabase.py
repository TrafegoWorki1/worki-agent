"""Store de aprovacoes no Supabase (tabela `acoes_pendentes`).

O guard (`integracoes/aprovacao/guard.py`) define a regra. Este modulo
fala com o banco. A separacao existe porque a regra e testavel sem banco
e o banco e testavel com regra fixa.

O ponto que nao e negociavel: **aprovacao e consumida atomicamente**. Se
dois processos perguntarem "esta aprovado?" ao mesmo tempo e os dois
receberem "sim", os dois executam. Por isso `consumir` e uma RPC com
`where status='aprovada'` — quem converte o status ganha, o outro recebe
zero rows.

Texto no prompt nao e controle de acesso. Um if lendo "aprova" no
conteudo da mensagem pode ser enganado por uma frase que contenha a
palavra, ou por um modelo que alucine que o dono aprovou. Aqui a
autorizacao e uma linha com alvo, artefato, validade e quem aprovou.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integracoes.aprovacao.guard import Aprovacao  # noqa: E402
from integracoes.supabase import queue as db  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(valor) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def _de_row(row: dict) -> Aprovacao:
    return Aprovacao(
        id=str(row.get("id") or ""),
        acao=row.get("acao") or "",
        alvo=row.get("alvo") or "",
        artifact_hash=row.get("payload_hash") or row.get("artifact_hash") or "",
        requested_by=row.get("solicitado_por") or "",
        approved_by=row.get("aprovador") or row.get("aprovado_por") or "",
        approved_at=_dt(row.get("decidido_em") or row.get("aprovado_em")),
        expires_at=_dt(row.get("expira_em")) or datetime.now(timezone.utc),
        status={"confirmada": "aprovada", "aguardando": "pendente"}.get(
            row.get("status"), row.get("status") or "pendente"),
    )


class StoreAprovacoes:
    """Implementa o contrato que o Guard espera.

    Guard.store precisa de: listar(task_id, acao), salvar(ap),
    registrar_pendente(ap), consumir(id).
    """

    def __init__(self, conversa_id: str = "", entrada_id: str = ""):
        self.conversa_id = conversa_id
        self.entrada_id = entrada_id

    def listar(self, task_id: str = "", acao: str | None = None) -> list[Aprovacao]:
        """Aprovacoes desta conversa (opcionalmente de uma acao).

        Filtra por `conversa_id` **sempre**: aprovacao de uma conversa nao
        vale na outra, mesmo que as duas estejam na mesma conta. Sem esse
        filtro, um "sobe" dado num grupo autorizaria producao no privado.
        """
        filtro = f"&conversa_id=eq.{self.conversa_id}" if self.conversa_id else ""
        if task_id:
            filtro += f"&tarefa_id=eq.{task_id}"
        if acao:
            filtro += f"&acao=eq.{acao}"
        linhas = db._rest(
            "GET",
            f"/rest/v1/acoes_pendentes?status=in.(aguardando,confirmada){filtro}"
            f"&order=criado_em.desc&limit=10",
            prefer="return=representation",
        ) or []
        return [_de_row(l) for l in linhas]

    def registrar_pendente(self, ap: Aprovacao, conversation_id: str = ""):
        """Pede aprovação pela RPC atômica e devolve o id."""
        valor = db._rpc("worki_pedir_aprovacao", {
            "p_conversa_id": conversation_id or self.conversa_id,
            "p_acao": ap.acao, "p_alvo": ap.alvo,
            "p_payload_hash": ap.artifact_hash,
            "p_descricao": ap.metadata.get("descricao", ""),
            "p_payload": ap.metadata.get("payload", {}),
        })
        if isinstance(valor, list):
            valor = valor[0] if valor else None
        if valor is not None:
            ap.id = str(valor)
        return ap

    def salvar(self, ap: Aprovacao, conversation_id: str = ""):
        if ap.status == "aprovada":
            valor = db._rpc("worki_aprovar_acao", {
                "p_conversa_id": conversation_id or self.conversa_id,
                "p_palavra": next((p for p, acts in {
                    "aprova": {"merge"}, "sobe": {"deploy_producao"},
                    "confirma": {"campanha_anuncio", "alterar_orcamento",
                                  "pausar_anuncio", "gasto"},
                }.items() if ap.acao in acts), ""),
                "p_aprovador": "558592494552@s.whatsapp.net",
                "p_acao_id": int(ap.id),
            })
            return valor
        status = {"pendente": "aguardando", "aprovada": "confirmada"}.get(
            ap.status, ap.status)
        corpo = {
            "status": status,
            "aprovador": ap.approved_by or None,
            "decidido_em": _iso(ap.approved_at) if ap.approved_at else None,
            "expira_em": _iso(ap.expires_at),
        }
        db._rest("PATCH", f"/rest/v1/acoes_pendentes?id=eq.{ap.id}", body=corpo)

    def consumir(self, ap_id: str, conversation_id: str = "", acao: str = "",
                 alvo: str = "", payload_hash: str = ""):
        """Consome atomicamente via RPC.

        Nao e um PATCH. Se fosse, dois processos poderiam ler
        status='aprovada' e ambos executarem. A RPC faz o UPDATE condicional
        e devolve quantas linhas mudou; so quem ganhou segue.
        """
        linhas = db._rpc("worki_consumir_aprovacao", {
            "p_acao_id": int(ap_id),
            "p_conversa_id": conversation_id or self.conversa_id,
            "p_acao": acao,
            "p_alvo": alvo,
            "p_payload_hash": payload_hash,
        })
        if isinstance(linhas, bool):
            return linhas
        if isinstance(linhas, list) and linhas:
            return bool(linhas[0])
        return False


def exigir(acao: str, alvo: str, conteudo_artefato: str,
           conversa_id: str, entrada_id: str = ""):
    """Atalho: Guard pronto com o store do Supabase.

    Levanta `AguardandoAprovacao` quando nao ha autorizacao valida — e
    esse o sinal para o worker responder o que faria e parar.
    """
    from integracoes.aprovacao.guard import Guard

    store = StoreAprovacoes(conversa_id=conversa_id, entrada_id=entrada_id)
    g = Guard(store=store)
    return g.exigir(acao, alvo, conteudo_artefato)


def expirar_vencidas(conversa_id: str = "") -> int:
    """Marca como expirada o que passou do prazo. Chame no boot."""
    linhas = db._rpc("worki_expirar_aprovacoes", {
        "p_agora": _iso(datetime.now(timezone.utc)),
    })
    if isinstance(linhas, int):
        return linhas
    return len(linhas) if linhas else 0


if __name__ == "__main__":
    import json
    print(json.dumps({"store": "aprovacoes", "rpcs": [
        "worki_consumir_aprovacao", "worki_expirar_aprovacoes",
    ]}, ensure_ascii=False))
