"""Pin do caminho de falha do worker.

Fecha tres bugs que, juntos, deixavam a conversa muda apos um unico erro do
Hermes (ver migracao 20261003190000_falha_nao_trava_conversa.sql e
integracoes/supabase/queue.py::concluir):

1. a sessao nao pode ser sobrescrita com a string 'erro';
2. o log tecnico nunca vai como texto para o WhatsApp;
3. o detalhe do erro vai para o log do servidor.

Teste de unidade: intercepta a RPC e inspeciona o payload, sem banco.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.supabase import queue  # noqa: E402


def _interceptar(monkeypatch):
    capturado = {}

    def fake_rpc(nome, payload, timeout=25):
        capturado["nome"] = nome
        capturado["payload"] = payload
        return None

    monkeypatch.setattr(queue, "_rpc", fake_rpc)
    monkeypatch.setattr(queue, "dono", lambda: "worker-teste")
    return capturado


def test_concluir_nao_corrompe_sessao(monkeypatch):
    cap = _interceptar(monkeypatch)
    queue.concluir("entrada-1", erro="traceback tecnico gigante")
    # p_session_id None: o SQL faz coalesce e mantem a sessao atual da conversa.
    # NUNCA a string 'erro', que quebrava o --resume seguinte.
    assert cap["payload"]["p_session_id"] is None
    assert cap["payload"]["p_session_id"] != "erro"


def test_concluir_nao_vaza_log_no_whatsapp(monkeypatch):
    cap = _interceptar(monkeypatch)
    segredo = "ERRO TECNICO: Traceback (most recent call last) ... /opt/data/secrets"
    queue.concluir("entrada-1", erro=segredo)
    # O que vai virar saida (texto do WhatsApp) e a mensagem neutra, nao o log.
    assert cap["payload"]["p_resposta"] == queue.MENSAGEM_FALHA_PADRAO
    assert segredo not in cap["payload"]["p_resposta"]


def test_concluir_marca_falha(monkeypatch):
    cap = _interceptar(monkeypatch)
    queue.concluir("entrada-1", erro="x")
    assert cap["payload"]["p_sucesso"] is False
    assert cap["nome"] == "worki_concluir_entrada"


def test_concluir_preserva_sessao_explicita(monkeypatch):
    cap = _interceptar(monkeypatch)
    queue.concluir("entrada-1", erro="x", session_id="20261003_120000_abcdef")
    # Quando o worker conhece o id real da execucao, ele e repassado tal qual.
    assert cap["payload"]["p_session_id"] == "20261003_120000_abcdef"


def test_concluir_mensagem_customizada(monkeypatch):
    cap = _interceptar(monkeypatch)
    queue.concluir("entrada-1", mensagem_usuario="Nao consegui gerar resposta.")
    assert cap["payload"]["p_resposta"] == "Nao consegui gerar resposta."
