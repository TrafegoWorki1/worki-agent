"""Guard de acao protegida (AGENTS.md secao 3).

Por que isto e um modulo e nao um if dentro do prompt: o texto "aprova" no
WhatsApp nao e controle de acesso. Se a unica verificacao for o agente ler o
prompt e decidir, qualquer desvio — alucinar que o dono aprovou, ou uma
frase que contenha a palavra por acaso — passa. Aqui a autorizacao e uma
linha no banco, gravada quando o pedido de aprovacao foi feito, e consumida
na hora da acao.

Fluxo:

    1. o executor chama `exigir(tarefa, "merge", alvo, artefato)`
    2. sem aprovacao valida, cria a linha e levanta `AguardandoAprovacao`
    3. o Herickson responde "aprova" no WhatsApp
    4. o relay chama `aprovar(...)` ligando acao + alvo + artefato
    5. o executor chama de novo `exigir(...)` e agora passa

Regras que o modulo garante:

- Vinculacao: a aprovacao vale para aquele alvo e aquela versao do artefato.
  Aprovar o merge do PR #12 nao autoriza o merge do #13.
- Uma vez so: `consumir` muda o status para 'consumida'. A segunda vez a
  mesma aprovacao nao passa.
- Validade: expirada nao vale, mesmo se o Herickson aprovou.
- Mudanca material invalida: se o artefato mudou, o hash deixa de bater e
  a aprovacao antiga morre. Isso fecha o "aprovei, ai o agente mudou tudo
  e mergeou assim mesmo".

Uma palavra isolada ("aprova") so vira aprovacao se existir UMA acao
pendente na conversa. Com duas pendentes, `aprovar` recebe `ambiguousas` e
recusa — quem decide o alvo e o dono, nao o interpretador.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class Bloqueado(Exception):
    """Acao protegida sem autorizacao valida."""

    def __init__(self, motivo: str, acao: str = "", alvo: str = ""):
        super().__init__(motivo)
        self.motivo = motivo
        self.acao = acao
        self.alvo = alvo


class Ambiguo(Bloqueado):
    """Mais de uma acao pendente: a palavra nao diz qual."""

    def __init__(self, pendentes: list[str]):
        super().__init__(
            f"ha {len(pendentes)} acoes pendentes nesta conversa; "
            "diga qual: " + ", ".join(pendentes),
            acao="ambiguo",
        )
        self.pendentes = pendentes


class AguardandoAprovacao(Exception):
    """Pedido registrado. Nao e erro: o executor espera o dono responder."""

    def __init__(self, acao: str, alvo: str, pedido: str):
        super().__init__(pedido)
        self.acao = acao
        self.alvo = alvo
        self.pedido = pedido


# AGENTS.md secao 3. A palavra e o que o dono digita; a tabela mapeia para
# a acao. "confirma" cobre varias linhas de proposito: sao a mesma intencao
# (gastar dinheiro) com alvos diferentes.
PALAVRAS: dict[str, set[str]] = {
    "aprova": {"merge"},
    "sobe": {"deploy_producao"},
    "confirma": {
        "campanha_anuncio",
        "alterar_orcamento",
        "pausar_anuncio",
        "gasto",
    },
}

# Validade por tipo de acao. Merge e deploy mudam o que esta no ar: prazo
# curto. Gasto e tambem curto pelo mesmo motivo — a intencao envelhece.
VALIDADE = {
    "merge": timedelta(hours=24),
    "deploy_producao": timedelta(minutes=30),
    "campanha_anuncio": timedelta(hours=2),
    "alterar_orcamento": timedelta(hours=2),
    "pausar_anuncio": timedelta(minutes=30),
    "gasto": timedelta(hours=1),
}


def agora() -> datetime:
    return datetime.now(timezone.utc)


def hash_artefato(conteudo: str) -> str:
    """Hash do artefato aprovado.

    Serve para amarrar a aprovacao a uma versao exata. Sem isso, "aprova o
    merge" valeria para qualquer coisa que aparecesse depois no mesmo PR.
    """
    return hashlib.sha256(conteudo.encode("utf-8")).hexdigest()


def acao_da_palavra(palavra: str) -> set[str]:
    """Mapeia a palavra digitada para as acoes que ela cobre."""
    return PALAVRAS.get(palavra.strip().lower().rstrip("!."), set())


@dataclass
class Aprovacao:
    id: str
    acao: str
    alvo: str
    artifact_hash: str
    requested_by: str
    expires_at: datetime
    status: str = "aprovada"
    approved_by: str = ""
    approved_at: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def valida(self, acao: str, alvo: str, artifact_hash: str) -> tuple[bool, str]:
        """Confere se esta aprovacao cobre exatamente esta acao."""
        if self.status == "consumida":
            return False, "aprovacao ja consumida"
        if self.status != "aprovada":
            return False, f"aprovacao em estado {self.status}"
        if self.expires_at < agora():
            return False, "aprovacao expirada"
        if self.acao != acao:
            return False, f"aprovacao e para '{self.acao}', nao '{acao}'"
        if self.alvo != alvo:
            return False, f"aprovacao e para '{self.alvo}', nao '{alvo}'"
        if self.artifact_hash != artifact_hash:
            # O artefato mudou depois da aprovacao. E o caso que invalida
            # de proposito, nao um detalhe.
            return False, "artefato mudou depois da aprovacao"
        return True, "ok"


class Guard:
    """Verifica e emite autorizacoes. Guarda em memoria por padrao.

    O store e injetado de proposito: em producao quem implementa e o cliente
    Supabase (integracoes/supabase/approvals.py), e o modulo e testavel
    aqui sem banco. O contrato do store e pequeno de proposito.
    """

    def __init__(self, store=None, pedido_texto=None):
        # store: listar(task_id, acao) -> [Aprovacao]; salvar(Aprovacao);
        #        registrar_pendente(...); consumir(id)
        self.store = store
        self._pedido_texto = pedido_texto or self._texto_padrao

    def _texto_padrao(self, acao: str, alvo: str) -> str:
        palavra = next(
            (p for p, acoes in PALAVRAS.items() if acao in acoes), "confirma"
        )
        return (
            f"Preciso da sua autorizacao para {acao} em {alvo}.\n"
            f"Responda APENAS '{palavra}' para eu seguir, ou diga o que mudar."
        )

    def exigir(
        self,
        acao: str,
        alvo: str,
        conteudo_artefato: str,
        task_id: str = "",
        conversation_id: str = "",
        requested_by: str = "herickson",
    ) -> Aprovacao:
        """Pede autorizacao. Levanta se nao houver uma valida.

        Devolve a Aprovacao consumivel quando ja existe uma valida — nesse
        caminho nao levanta e o executor segue.
        """
        artefato = hash_artefato(conteudo_artefato)

        if self.store is not None:
            for ap in self.store.listar(task_id=task_id, acao=acao):
                ok, _ = ap.valida(acao, alvo, artefato)
                if ok:
                    self.store.consumir(ap.id, conversation_id)
                    ap.status = "consumida"
                    return ap

        nova = Aprovacao(
            id="",
            acao=acao,
            alvo=alvo,
            artifact_hash=artefato,
            requested_by=requested_by,
            expires_at=agora() + VALIDADE.get(acao, timedelta(hours=1)),
        )
        if self.store is not None:
            self.store.registrar_pendente(nova, conversation_id=conversation_id)

        # Resposta pro dono: o que o agente faria, e para. Nao executa.
        raise AguardandoAprovacao(acao, alvo, self._pedido_texto(acao, alvo))

    def aprovar_palavra(
        self,
        palavra: str,
        task_id: str = "",
        conversation_id: str = "",
        approved_by: str = "herickson",
    ) -> Aprovacao:
        """Transforma a palavra do dono em aprovacao consumivel.

        Com mais de uma pendencia, recusa e diz o que falta. Sem
        pendencia, recusa tambem — palavra solta nao cria poder.
        """
        acoes = acao_da_palavra(palavra)
        if not acoes:
            raise Bloqueado(
                f"'{palavra}' nao e palavra de aprovacao "
                f"(valem: {', '.join(PALAVRAS)})",
                acao="desconhecida",
            )

        if self.store is None:
            raise Bloqueado("guard sem store: nao ha onde ler pendencias")

        pendentes = [
            ap for ap in self.store.listar(task_id=task_id) if ap.status == "pendente"
        ]
        if not pendentes:
            raise Bloqueado(
                "nao ha acao pendente para aprovar nesta conversa", acao="nenhuma"
            )
        if len(pendentes) > 1:
            raise Ambiguo([f"{ap.acao} em {ap.alvo}" for ap in pendentes])

        alvo = pendentes[0]
        # A palavra cobre a acao, mas o alvo tem de ser um destes.
        if alvo.acao not in acoes:
            raise Bloqueado(
                f"'{palavra}' autoriza {sorted(acoes)}, nao '{alvo.acao}'",
                acao=alvo.acao,
                alvo=alvo.alvo,
            )

        alvo.status = "aprovada"
        alvo.approved_by = approved_by
        alvo.approved_at = agora()
        self.store.salvar(alvo, conversation_id=conversation_id)
        return alvo

    def expirar(self, task_id: str = "") -> int:
        """Marca como expirada o que ja passou do prazo."""
        if self.store is None:
            return 0
        n = 0
        for ap in self.store.listar(task_id=task_id):
            if ap.status in ("pendente", "aprovada") and ap.expires_at < agora():
                ap.status = "expirada"
                self.store.salvar(ap)
                n += 1
        return n
