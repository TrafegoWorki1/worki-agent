"""Aviso de andamento do worker (WORKI_ACK_AFTER_SECONDS).

Fixa quatro comportamentos:
1. desligado (0, o padrao): nunca manda aviso;
2. resposta rapida: nao manda aviso nenhum (nao polui o chat);
3. resposta lenta: manda exatamente um aviso, antes da resposta;
4. falha ao enviar o aviso nao derruba a tarefa nem muda o resultado.

Usa pecas falsas, sem banco, sem Hermes e sem Evolution.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.worker.relay import MENSAGEM_AVISO, Relay  # noqa: E402


class HermesFake:
    def __init__(self, demora_s):
        self.demora_s = demora_s

    def responder(self, texto, session_id=None, contexto=""):
        time.sleep(self.demora_s)
        return SimpleNamespace(ok=True, texto="resposta pronta",
                               session_id="20261003_120000_abcdef", log="")


class DBFake:
    def sessao_da_conversa(self, cid):
        return None

    def tarefas_ativas(self, cid):
        return []

    def iniciar_entrada(self, eid):
        return "tarefa-1"

    def renovar(self, eid, lease_s=120):
        return True

    def salvar_sessao(self, cid, sid):
        pass

    def concluir_atomic(self, eid, sid, texto, sucesso):
        return "saida-1"

    def marcar_saida(self, sid, status, provider_message_id=None, erro=None):
        pass

    def concluir(self, eid, erro=None, **kw):
        pass

    def cancelar_tarefa(self, tid, motivo):
        pass


class EvFake:
    """Registra tudo que o worker tenta enviar, em ordem."""

    def __init__(self, falhar_aviso=False):
        self.enviados = []
        self.falhar_aviso = falhar_aviso

    def enviar_texto(self, chat, texto):
        if self.falhar_aviso and texto.startswith("Recebi"):
            raise RuntimeError("Evolution fora do ar")
        self.enviados.append(texto)
        return "entregue", "pmid"


def _worker(demora_s, ack_s, falhar_aviso=False):
    ev = EvFake(falhar_aviso)
    cfg = SimpleNamespace(WORKI_WORKER_LEASE_S=3600, WORKI_ACK_AFTER_SECONDS=ack_s)
    relay = Relay(cfg=cfg, adaptador=HermesFake(demora_s), dbmod=DBFake(), evmod=ev)
    # O worker compara o estado com ev.Estado.ENTREGUE do modulo real; "entregue"
    # e o mesmo valor, entao o fake nao precisa importar o modulo.
    entrada = SimpleNamespace(id="e1", conversa_id="c1", chat_jid="5585@s.whatsapp.net",
                              texto="faca algo", session_id=None)
    return relay, ev, entrada


def test_desligado_nao_manda_aviso():
    relay, ev, entrada = _worker(demora_s=0.4, ack_s=0)
    r = relay.processar(entrada)
    assert r.ok
    assert ev.enviados == ["resposta pronta"]


def test_resposta_rapida_nao_gera_aviso():
    relay, ev, entrada = _worker(demora_s=0.05, ack_s=1)
    r = relay.processar(entrada)
    assert r.ok
    time.sleep(1.2)  # passa do prazo do aviso: o timer cancelado nao pode disparar
    assert ev.enviados == ["resposta pronta"]


def test_resposta_lenta_manda_um_aviso_antes_da_resposta():
    relay, ev, entrada = _worker(demora_s=1.0, ack_s=0.2)
    r = relay.processar(entrada)
    assert r.ok
    assert ev.enviados == [MENSAGEM_AVISO, "resposta pronta"]


def test_falha_no_aviso_nao_derruba_a_tarefa():
    relay, ev, entrada = _worker(demora_s=0.6, ack_s=0.1, falhar_aviso=True)
    r = relay.processar(entrada)
    assert r.ok
    assert r.texto == "resposta pronta"
    assert ev.enviados == ["resposta pronta"]
