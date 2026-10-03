"""Cliente da fila real (tabelas em portugues). Nao contem o worker.

Este modulo e so a **fala com o banco**. O ciclo do worker — quando rodar,
em que ordem, com que contexto — esta em `integracoes/worker/relay.py`.

Separacao proposital: o relay fica testavel sem banco porque as funcoes
aqui sao injetaveis, e este modulo e testavel sem Hermes porque nao importa
o adaptador.

Os nomes de RPC aqui sao o contrato acordado com o Codex em
`docs/contrato-rpcs.md`. Se ele renomear, muda-se num lugar so: o resto do
codigo chama por metodo, nunca por nome de RPC.

**Ate o Codex aplicar o contrato, estas funcoes levantam
`FilaNaoDisponivel`.** Nao existe fallback silencioso: um worker que
acha que gravou e nao gravou e pior do que um worker parado.

O `queue_legacy_en.py` (PR #1) e a versao em ingles, contra as tabelas que
NUNCA serao criadas. Fica no repo como historico, e nao e importado.
"""

import hashlib
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integracoes.config import Config  # noqa: E402

log = logging.getLogger("worki.queue")

# Mensagem neutra que o usuario recebe quando uma tarefa falha. O detalhe
# tecnico NUNCA vai para o WhatsApp: fica no log do servidor e no banco.
MENSAGEM_FALHA_PADRAO = (
    "Tive um problema ao processar isso agora. Pode reenviar ou reformular?"
)

_CFG = None


def cfg():
    global _CFG
    if _CFG is None:
        _CFG = Config.carregar()
    return _CFG


def agora() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Entrada:
    """Mensagem reservada, com o contexto da conversa junto.

    `session_id` vem na mesma query da reserva: e o caminho quente, e uma
    query extra custaria uma ida ao banco a mais por mensagem.
    """

    id: str
    mensagem_id: str
    conversa_id: str
    session_id: str | None
    texto: str
    de: str
    chat_jid: str
    tentativas: int = 0
    metadata: dict = field(default_factory=dict)

    @classmethod
    def de_row(cls, row: dict) -> "Entrada":
        return cls(
            id=str(row["entrada_id"]),
            mensagem_id=str(row["mensagem_id"]),
            conversa_id=str(row.get("conversa_id") or ""),
            session_id=row.get("session_id"),
            texto=row.get("texto") or "",
            de=row.get("de") or "",
            chat_jid=row.get("chat_jid") or "",
            tentativas=int(row.get("tentativas") or 0),
        )


class FilaNaoDisponivel(RuntimeError):
    """As RPCs do contrato ainda nao existem no banco."""


def _headers() -> dict:
    c = cfg()
    return {
        "apikey": c.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {c.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _rpc(nome: str, payload: dict, timeout: int = 25):
    """Chama uma RPC do Supabase.

    404 = RPC nao existe (contrato nao aplicado ainda).
    401/403 = grant/revoke mal configurado.
    Os dois sao erro de configuracao, nao transitorio.
    """
    c = cfg()
    req = urllib.request.Request(
        f"{c.SUPABASE_URL}/rest/v1/rpc/{nome}",
        data=json.dumps(payload).encode(),
        headers=_headers(), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            bruto = r.read().decode()
            return json.loads(bruto) if bruto else []
    except urllib.error.HTTPError as e:
        corpo = e.read().decode()[:300]
        if e.code == 404:
            raise FilaNaoDisponivel(
                f"RPC {nome} nao existe no banco. O Codex aplicou o "
                f"contrato de docs/contrato-rpcs.md? ({corpo})"
            ) from e
        if e.code in (401, 403):
            raise RuntimeError(
                f"RPC {nome} recusou com {e.code}. A funcao precisa de "
                f"SECURITY DEFINER, revoke de public/anon/authenticated e "
                f"grant a service_role. ({corpo})"
            ) from e
        raise RuntimeError(f"RPC {nome} falhou: HTTP {e.code} {corpo}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"RPC {nome} erro de rede: {e}") from e


def _rest(metodo: str, caminho: str, body=None,
          prefer: str = "return=minimal", timeout: int = 20):
    c = cfg()
    req = urllib.request.Request(
        f"{c.SUPABASE_URL}{caminho}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={**_headers(), "Prefer": prefer},
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            bruto = r.read().decode()
            return json.loads(bruto) if bruto else []
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"{metodo} {caminho}: HTTP {e.code} {e.read().decode()[:200]}"
        ) from e


def ping() -> bool:
    """O banco responde? Usado no /ready."""
    try:
        _rest("GET", "/rest/v1/entradas?select=id&limit=1")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Receptor -> banco
# --------------------------------------------------------------------------

def registrar_evento(ev) -> tuple[str | None, bool]:
    """Grava mensagem + entrada numa transacao. Usado pelo receptor.

    Devolve (entrada_id, ja_existia). `ja_existia=True` = evento repetido:
    sucesso, sem trabalho novo. `entrada_id=None` com `ja_existia=True`
    significa "ja processado antes" — o receptor trata como sucesso.
    """
    linhas = _rpc("worki_registrar_evento", {
        "p_instancia": ev.instancia,
        "p_chat_jid": ev.chat_jid,
        "p_remetente": ev.remetente_jid or ev.remetente_numeros,
        "p_provider_message_id": ev.provider_message_id,
        "p_tipo": ev.tipo_mensagem or "conversation",
        "p_texto": ev.texto,
    })
    if not linhas:
        return None, False
    linha = linhas[0]
    return (str(linha["entrada_id"]) if linha.get("entrada_id") else None,
            bool(linha.get("duplicado")))


# --------------------------------------------------------------------------
# Worker -> banco
# --------------------------------------------------------------------------

def dono() -> str:
    """Identidade do worker: hostname + pid.

    Dois containers nunca compartilham, nem dois processos no mesmo
    container. E o que torna seguro o `where lease_owner = eu` da
    conclusao.
    """
    return os.environ.get("WORKI_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"


def reservar(limite: int = 1, lease_s: int = 120) -> list[Entrada]:
    """Reserva atomicamente. Vazio quando nao ha nada."""
    linhas = _rpc("worki_reservar_entrada", {
        "p_owner": dono(), "p_lease_s": lease_s,
    }, timeout=30)
    if not linhas:
        return []
    # A reserva retorna apenas IDs. Buscar os dados fora da RPC mantém a
    # transação curta e evita depender de joins que não fazem parte do
    # contrato.
    entradas = []
    for row in linhas:
        entrada_id = str(row["id"])
        eid = urllib.parse.quote(entrada_id, safe="")
        erows = _rest("GET", f"/rest/v1/entradas?id=eq.{eid}&select=id,mensagem_id,conversa_id,tentativas",
                      prefer="return=representation")
        if not erows:
            continue
        e = erows[0]
        mids = urllib.parse.quote(str(e["mensagem_id"]), safe="")
        # A coluna do remetente em `mensagens` chama-se `de`, nao
        # `remetente`. Pedir `remetente` faz o PostgREST responder 400 e
        # o worker falhar ANTES de chamar o Hermes: a entrada volta para
        # a fila e o ciclo se repete. O schema versionado em
        # supabase/migrations/ usa `de`.
        msgs = _rest("GET", f"/rest/v1/mensagens?id=eq.{mids}&select=texto,de",
                     prefer="return=representation")
        cid = urllib.parse.quote(str(e["conversa_id"]), safe="")
        convs = _rest("GET", f"/rest/v1/conversas?id=eq.{cid}&select=session_id,chat_jid",
                      prefer="return=representation")
        m = msgs[0] if msgs else {}
        c = convs[0] if convs else {}
        entradas.append(Entrada(id=entrada_id, mensagem_id=str(e["mensagem_id"]),
                                conversa_id=str(e["conversa_id"]),
                                session_id=c.get("session_id"), texto=m.get("texto") or "",
                                de=m.get("de") or "", chat_jid=c.get("chat_jid") or "",
                                tentativas=int(e.get("tentativas") or 0)))
    return entradas


def renovar(entrada_id: str, lease_s: int = 120) -> bool:
    """Estica a lease de tarefa longa. False = perdemos a lease."""
    return bool(_rpc("worki_renovar_lease", {
        "p_entrada_id": entrada_id,
        "p_owner": dono(),
        "p_lease_s": lease_s,
    }))


def iniciar_entrada(entrada_id: str) -> str:
    valor = _rpc("worki_iniciar_entrada", {"p_entrada_id": entrada_id, "p_owner": dono()})
    return str(valor[0] if isinstance(valor, list) else valor)


def concluir_atomic(entrada_id: str, session_id: str, resposta: str,
                    sucesso: bool = True) -> str:
    valor = _rpc("worki_concluir_entrada", {
        "p_entrada_id": entrada_id, "p_owner": dono(),
        "p_session_id": session_id, "p_resposta": resposta,
        "p_sucesso": sucesso,
    })
    return str(valor[0] if isinstance(valor, list) else valor)


def concluir(entrada_id: str, erro: str | None = None,
             session_id: str | None = None,
             mensagem_usuario: str | None = None) -> str | None:
    """Conclui uma entrada que falhou. Caminho de erro; o sucesso e atomico.

    Devolve o id da saida criada, ou None quando a falha NAO gerou saida.
    Quem chama usa isso para saber se a outbox vai entregar o aviso ao
    usuario: a migration 20261003120000 (outbox so no sucesso) faz a RPC
    devolver null numa falha, enquanto a versao anterior da RPC cria a saida.
    O codigo funciona nos dois estados do banco; ver relay._avisar_falha.

    Tres garantias, cada uma fechando um bug real:

    1. A sessao do Hermes e PRESERVADA. `p_session_id=None` faz o SQL manter
       (coalesce) o session_id atual da conversa. Antes gravava-se a string
       'erro', e o --resume da proxima mensagem quebrava com um id inexistente.
    2. O usuario recebe uma mensagem curta e neutra, NUNCA o log tecnico.
       Antes o `erro` (ate 1000 chars de traceback do Hermes) ia direto para
       o WhatsApp como texto da saida.
    3. O detalhe tecnico (`erro`) vai so para o log do servidor.

    `session_id`: passe o id real da execucao para preserva-lo explicitamente;
    None mantem o que ja esta na conversa. `mensagem_usuario`: sobrescreve a
    mensagem neutra padrao quando o chamador tem algo melhor a dizer.
    """
    if erro:
        log.warning("entrada %s concluida com falha: %s",
                    entrada_id, str(erro)[:500])
    valor = _rpc("worki_concluir_entrada", {
        "p_entrada_id": entrada_id, "p_owner": dono(),
        "p_session_id": session_id,
        "p_resposta": mensagem_usuario or MENSAGEM_FALHA_PADRAO,
        "p_sucesso": False,
    })
    if isinstance(valor, list):
        valor = valor[0] if valor else None
    return str(valor) if valor else None


def tarefa_em_andamento(instancia: str, chat_jid: str) -> dict | None:
    """Pedido que o worker esta executando agora nesta conversa, ou None.

    Usado pelo atalho de andamento do receptor. Devolve
    {"entrada_id", "objetivo", "iniciada_em"}; o objetivo e o texto do pedido
    (gravado por worki_iniciar_entrada). So leitura.
    """
    q = urllib.parse.quote
    convs = _rest("GET", f"/rest/v1/conversas?instancia=eq.{q(instancia, safe='')}"
                         f"&chat_jid=eq.{q(chat_jid, safe='')}&select=id&limit=1",
                  prefer="return=representation")
    if not convs:
        return None
    ents = _rest("GET", f"/rest/v1/entradas?conversa_id=eq.{q(str(convs[0]['id']), safe='')}"
                        f"&status=eq.processando&select=id,execucao_iniciada_em,atualizado_em"
                        f"&order=ordem.asc&limit=1",
                 prefer="return=representation")
    if not ents:
        return None
    e = ents[0]
    tars = _rest("GET", f"/rest/v1/tarefas?entrada_id=eq.{q(str(e['id']), safe='')}"
                        f"&select=objetivo&limit=1",
                 prefer="return=representation")
    return {
        "entrada_id": str(e["id"]),
        "objetivo": (tars[0].get("objetivo") if tars else None) or "",
        "iniciada_em": e.get("execucao_iniciada_em") or e.get("atualizado_em"),
    }


def cancelar_entrada(entrada_id: str, motivo: str) -> None:
    """Cancela uma entrada que AINDA nao foi pega pelo worker.

    O filtro status=aguardando garante que nao se cancela trabalho em curso:
    se o worker ja reservou, a atualizacao nao casa e nada muda.
    """
    eid = urllib.parse.quote(entrada_id, safe="")
    _rest("PATCH", f"/rest/v1/entradas?id=eq.{eid}&status=eq.aguardando",
          {"status": "cancelada", "ultimo_erro": motivo[:200]})


def recuperar_leases() -> int:
    """Devolve leases vencidas para a fila. Chame no boot.

    Um worker morto no meio deixa a entrada em 'processando'. Sem isto,
    ela fica presa para sempre.
    """
    linhas = _rpc("worki_recuperar_leases", {})
    if isinstance(linhas, dict):
        return int(linhas.get("tarefas_bloqueadas", 0))
    if linhas is None:
        return 0
    if isinstance(linhas, int):
        return linhas
    return len(linhas)


# --------------------------------------------------------------------------
# Continuidade: sessao e tarefas
# --------------------------------------------------------------------------

def sessao_da_conversa(conversa_id: str) -> str | None:
    """session_id gravado nesta conversa. Nunca 'latest' global."""
    linhas = _rest(
        "GET", f"/rest/v1/conversas?id=eq.{conversa_id}&select=session_id",
        prefer="return=representation")
    return linhas[0].get("session_id") if linhas else None


def salvar_sessao(conversa_id: str, session_id: str) -> None:
    """Grava o session_id devolvido pelo Hermes.

    Falha aqui nao repete a tarefa: o trabalho ja foi feito, e reexecutar
    custa mais (branch, commit, deploy) do que perder o vinculo de
    contexto. O proximo turno abre sessao nova e segue.
    """
    _rest("PATCH", f"/rest/v1/conversas?id=eq.{conversa_id}",
          body={"session_id": session_id, "atualizado_em": iso(agora())})


def tarefas_ativas(conversa_id: str) -> list[dict]:
    """Tarefas em andamento, para retomar em vez de recomecar do zero."""
    return _rest(
        "GET",
        f"/rest/v1/tarefas?conversa_id=eq.{conversa_id}"
        f"&status=in.(ativa,bloqueada)&order=criado_em.desc&limit=5",
        prefer="return=representation") or []


def criar_tarefa(conversa_id: str, entrada_id: str, objetivo: str) -> str | None:
    """Registra objetivo e proxima acao antes de comecar.

    Objective e next_action vivem no banco, nao no resumo do chat: se o
    processo morrer, a proxima execucao le daqui o que falta.
    """
    linhas = _rest(
        "POST", "/rest/v1/tarefas",
        body={
            "conversa_id": conversa_id,
            "entrada_id": entrada_id,
            "objetivo": objetivo[:500],
            "status": "ativa",
            "proxima_acao": "executar pedido",
            "criado_em": iso(agora()),
            "atualizado_em": iso(agora()),
        },
        prefer="return=representation")
    return str(linhas[0]["id"]) if linhas else None


def salvar_progresso(tarefa_id: str, checkpoint: str,
                     resultado: str | None = None, status: str = "ativa",
                     proxima: str | None = None) -> None:
    """Grava checkpoint depois de cada etapa material.

    `checkpoint` e texto livre, nao jsonb: o conteudo evolui por fase e o
    snapshot e reescrito inteiro mesmo.
    """
    corpo = {
        "checkpoint": checkpoint[:8000],
        "status": status,
        "atualizado_em": iso(agora()),
    }
    if resultado is not None:
        corpo["resultado"] = resultado[:8000]
    if proxima is not None:
        corpo["proxima_acao"] = proxima[:500]
    _rest("PATCH", f"/rest/v1/tarefas?id=eq.{tarefa_id}", body=corpo)


def concluir_tarefa(tarefa_id: str, resultado: str) -> None:
    salvar_progresso(tarefa_id, resultado, resultado=resultado,
                     status="concluida", proxima="")


def cancelar_tarefa(tarefa_id: str, motivo: str) -> None:
    salvar_progresso(tarefa_id, motivo, status="cancelada", proxima="")


# --------------------------------------------------------------------------
# Outbox: gravado ANTES de enviar
# --------------------------------------------------------------------------

def chave_idempotencia(entrada_id: str, resposta: str) -> str:
    """Chave deterministica da saida.

    Determinismo e o que fecha a janela "gravar saida -> concluir entrada":
    se o processo morre entre os dois, a retentativa recria **a mesma**
    chave, o indice unico rejeita, nao nasce uma segunda saida — e logo o
    Hermes nao roda de novo.

    Nao incluir timestamp nem uuid: quebraria o dedupe.
    """
    return hashlib.sha256(f"{entrada_id}|{resposta}".encode()).hexdigest()


def registrar_saida(entrada_id: str, chat_jid: str, texto: str) -> str | None:
    """Grava a resposta ANTES do envio.

    `texto` vazio levanta: seria um no-op que marcaria entrega como feita.
    """
    if not texto or not texto.strip():
        raise ValueError("texto vazio nao vai para a outbox")
    raise FilaNaoDisponivel("saida deve ser criada por worki_concluir_entrada")


def marcar_saida(saida_id: str, status: str,
                provider_message_id: str | None = None,
                erro: str | None = None) -> None:
    estado = "enviada" if status in ("entregue", "enviada") else status
    _rpc("worki_registrar_envio", {
        "p_saida_id": saida_id, "p_owner": dono(),
        "p_estado": estado, "p_provider_message_id": provider_message_id,
    })


def saidas_pendentes(limite: int = 10) -> list[dict]:
    """Saias a tratar: 'pendente', 'enviando', 'falhou' e 'incerto'.

    'incerto' entra aqui **para ser reconciliado**, nunca para reenviar:
    o worker chama `reconciliar()` antes de qualquer envio quando o status
    e 'enviando' ou 'incerto' (ver relay.py). Se o veredito for
    'entregue', marca; se for 'falhou', so entao reenvia.

    Filtrar 'incerto' fora desta lista parecia mais seguro, mas deixava a
    linha presa para sempre — ninguem a reconciliaria e ela nunca sairia
    da fila.
    """
    return _rest(
        "GET", f"/rest/v1/saidas?status=in.(pendente,enviando,falhou,incerto)"
        f"&order=criado_em.asc&limit={limite}",
        prefer="return=representation") or []


def reservar_saida(limite: int = 10, lease_s: int = 120) -> list[dict]:
    """Reserva atomicamente uma saida; a RPC ja marca `enviando`."""
    linhas = _rpc("worki_reservar_saida", {"p_owner": dono(), "p_lease_s": lease_s})
    return linhas or []


def saidas_incertainas(limite: int = 10) -> list[dict]:
    """Saias com entrega desconhecida. Nao reenviar sem reconciliar."""
    return _rest(
        "GET",
        f"/rest/v1/saidas?status=eq.incerto&order=criado_em.asc&limit={limite}",
        prefer="return=representation") or []
