"""Aviso de falha ao usuario, nos dois estados possiveis do banco.

A RPC worki_concluir_entrada pode (a) criar uma saida com o aviso, ou
(b) nao criar nenhuma (migration 20261003120000, "outbox so no sucesso").
Garantias:
1. banco em (b): o worker avisa o usuario direto (senao a falha e silenciosa);
2. banco em (a): o worker NAO avisa direto (a outbox entrega; evita duplicar);
3. falha ao avisar nao muda o desfecho nem levanta excecao;
4. queue.concluir devolve o id da saida criada, ou None.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.supabase import queue  # noqa: E402
from integracoes.worker.relay import Relay  # noqa: E402


class HermesFalha:
    def responder(self, texto, session_id=None, contexto=""):
        return SimpleNamespace(ok=False, texto=None, session_id="20261003_120000_abcdef",
                               log="Traceback (most recent call last): segredo interno")


class DBFake:
    """`saida_na_falha`: o que a RPC devolve numa falha (None = estado (b))."""

    def __init__(self, saida_na_falha):
        self.saida_na_falha = saida_na_falha
        self.concluidas = []

    def sessao_da_conversa(self, cid):
        return None

    def tarefas_ativas(self, cid):
        return []

    def iniciar_entrada(self, eid):
        return "tarefa-1"

    def renovar(self, eid, lease_s=120):
        return True

    def concluir(self, eid, erro=None, session_id=None, mensagem_usuario=None):
        self.concluidas.append((eid, session_id))
        return self.saida_na_falha


class EvFake:
    def __init__(self, falhar=False):
        self.enviados = []
        self.falhar = falhar

    def enviar_texto(self, chat, texto):
        if self.falhar:
            raise RuntimeError("Evolution fora do ar")
        self.enviados.append((chat, texto))
        return "entregue", "pmid"


def _rodar(saida_na_falha, ev_falha=False):
    db, ev = DBFake(saida_na_falha), EvFake(ev_falha)
    cfg = SimpleNamespace(WORKI_WORKER_LEASE_S=3600, WORKI_ACK_AFTER_SECONDS=0)
    relay = Relay(cfg=cfg, adaptador=HermesFalha(), dbmod=db, evmod=ev)
    entrada = SimpleNamespace(id="e1", conversa_id="c1", chat_jid="5585@s.whatsapp.net",
                              texto="faca algo", session_id="sessao-anterior")
    return relay.processar(entrada), db, ev


def test_banco_sem_saida_na_falha_worker_avisa_direto():
    r, db, ev = _rodar(saida_na_falha=None)
    assert not r.ok
    assert ev.enviados == [("5585@s.whatsapp.net", queue.MENSAGEM_FALHA_PADRAO)]


def test_banco_com_saida_na_falha_worker_nao_duplica():
    r, db, ev = _rodar(saida_na_falha="saida-123")
    assert not r.ok
    assert ev.enviados == []  # a outbox entrega; aviso direto seria duplicata


def test_log_tecnico_nunca_vai_ao_usuario():
    for saida in (None, "saida-123"):
        _, _, ev = _rodar(saida_na_falha=saida)
        assert all("Traceback" not in texto for _, texto in ev.enviados)


def test_sessao_preservada_na_falha():
    _, db, _ = _rodar(saida_na_falha=None)
    assert db.concluidas == [("e1", "20261003_120000_abcdef")]


def test_falha_ao_avisar_nao_levanta_nem_muda_desfecho():
    r, db, ev = _rodar(saida_na_falha=None, ev_falha=True)
    assert not r.ok
    assert len(db.concluidas) == 1  # a entrada foi concluida mesmo assim


def _rpc_devolvendo(monkeypatch, valor):
    monkeypatch.setattr(queue, "_rpc", lambda nome, payload, timeout=25: valor)
    monkeypatch.setattr(queue, "dono", lambda: "w")


def test_queue_concluir_devolve_id_da_saida(monkeypatch):
    _rpc_devolvendo(monkeypatch, "11111111-1111-1111-1111-111111111111")
    assert queue.concluir("e1", erro="x") == "11111111-1111-1111-1111-111111111111"


def test_queue_concluir_aceita_lista(monkeypatch):
    _rpc_devolvendo(monkeypatch, ["22222222-2222-2222-2222-222222222222"])
    assert queue.concluir("e1", erro="x") == "22222222-2222-2222-2222-222222222222"


def test_queue_concluir_devolve_none_sem_saida(monkeypatch):
    for vazio in (None, [], ""):
        _rpc_devolvendo(monkeypatch, vazio)
        assert queue.concluir("e1", erro="x") is None
