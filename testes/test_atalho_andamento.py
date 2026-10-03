"""Atalho de andamento: responde "terminou?" na hora quando ha pedido rodando.

Garantias:
1. a regra reconhece cobrancas curtas e NAO pega pedidos novos;
2. a resposta diz so o que o banco sabe (pedido e ha quanto tempo);
3. com pedido rodando: responde e cancela a entrada (o Hermes nao responde de novo);
4. sem pedido rodando, falha na consulta, falha no envio, evento duplicado,
   atalho desligado, ou mensagem que nao e de andamento: nada muda, segue a fila;
5. o receptor devolve 200 em todos os casos.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes import triagem  # noqa: E402
from integracoes.evolution.webhook import Receptor  # noqa: E402

DONO = "558592494552"
CHAT = f"{DONO}@s.whatsapp.net"


@pytest.mark.parametrize("texto", [
    "Terminou?", "terminou", "Cadê ?", "cade", "E aí?", "e ai", "Já foi?",
    "Tá pronto?", "ficou pronto?", "Como está?", "alguma novidade?", "E então?",
    "Vai demorar?", "Conseguiu?", "ja terminou?", "Tá aí?", "status",
])
def test_regra_reconhece_cobranca_de_andamento(texto):
    assert triagem.e_pergunta_de_andamento(texto)


@pytest.mark.parametrize("texto", [
    "Oi", "Oii", "", "   ", "Cria uma página para o Ritchie",
    "Terminou a página? agora cria outra", "foi mal, errei",
    "Continua aquela página", "Troca o título para este",
    "Quais skills você tem?", "e ai, cria um post sobre trafego pago",
])
def test_regra_nao_pega_pedido_novo(texto):
    assert not triagem.e_pergunta_de_andamento(texto)


def test_mensagem_diz_pedido_e_tempo():
    agora = datetime(2026, 10, 3, 17, 10, tzinfo=timezone.utc)
    msg = triagem.mensagem_andamento(
        {"objetivo": "Cria uma página para o Ritchie", "iniciada_em": "2026-10-03T17:04:00+00:00"},
        agora)
    assert "Ainda estou trabalhando nisso." in msg
    assert '"Cria uma página para o Ritchie"' in msg
    assert "Comecei ha 6 min." in msg


def test_mensagem_trunca_pedido_longo_e_aguenta_sem_horario():
    msg = triagem.mensagem_andamento({"objetivo": "x" * 300, "iniciada_em": None})
    assert "..." in msg and len(msg) < 200
    assert "Comecei" not in msg


# ------------------------------------------------------------------ receptor

def _cfg(atalho=True):
    return SimpleNamespace(
        WORKI_WEBHOOK_SECRET="", EVOLUTION_INSTANCE="agent-domintante",
        WHATSAPP_GRUPO_COMANDO_JIDS=[], WHATSAPP_NUMERO_AUTORIZADO=DONO,
        WORKI_ATALHO_ANDAMENTO=atalho, EVOLUTION_API_URL="http://x")


def _evento(texto, mid="M1"):
    return {"event": "messages.upsert", "data": {
        "key": {"remoteJid": CHAT, "fromMe": False, "id": mid},
        "message": {"conversation": texto}}}


TAREFA = {"entrada_id": "entrada-em-curso", "objetivo": "Cria uma página",
          "iniciada_em": "2026-10-03T17:04:00+00:00"}


def _receptor(*, atalho=True, ja_existia=False, tarefa=TAREFA,
              estado_envio="entregue", buscar_falha=False):
    reg = {"enviados": [], "cancelados": [], "buscas": 0}

    def buscar(instancia, chat):
        reg["buscas"] += 1
        if buscar_falha:
            raise RuntimeError("Supabase fora do ar")
        return tarefa

    def enviar(chat, texto):
        reg["enviados"].append((chat, texto))
        return estado_envio, "pmid"

    rec = Receptor(_cfg(atalho), gravar=lambda ev: ("entrada-nova", ja_existia),
                   buscar_tarefa=buscar, enviar=enviar,
                   cancelar=lambda eid, motivo: reg["cancelados"].append(eid),
                   executar=lambda fn: fn())  # sincrono no teste
    return rec, reg


def test_com_pedido_rodando_responde_e_cancela_a_entrada():
    rec, reg = _receptor()
    status, corpo = rec.processar(_evento("Terminou?"), {})
    assert status == 200 and corpo["ok"]
    assert len(reg["enviados"]) == 1
    assert reg["enviados"][0][0] == CHAT
    assert "Ainda estou trabalhando nisso." in reg["enviados"][0][1]
    assert reg["cancelados"] == ["entrada-nova"]


def test_sem_pedido_rodando_segue_a_fila():
    rec, reg = _receptor(tarefa=None)
    assert rec.processar(_evento("Terminou?"), {})[0] == 200
    assert reg["enviados"] == [] and reg["cancelados"] == []


def test_evento_duplicado_nao_responde_de_novo():
    rec, reg = _receptor(ja_existia=True)
    assert rec.processar(_evento("Terminou?"), {})[0] == 200
    assert reg["buscas"] == 0 and reg["enviados"] == []


def test_envio_falhou_nao_cancela():
    rec, reg = _receptor(estado_envio="falhou")
    assert rec.processar(_evento("Cadê ?"), {})[0] == 200
    assert len(reg["enviados"]) == 1 and reg["cancelados"] == []


def test_consulta_falhou_nao_quebra_o_receptor():
    rec, reg = _receptor(buscar_falha=True)
    assert rec.processar(_evento("Cadê ?"), {})[0] == 200
    assert reg["enviados"] == [] and reg["cancelados"] == []


def test_atalho_desligado_nao_faz_nada():
    rec, reg = _receptor(atalho=False)
    assert rec.processar(_evento("Terminou?"), {})[0] == 200
    assert reg["buscas"] == 0


def test_mensagem_comum_nao_dispara_o_atalho():
    rec, reg = _receptor()
    assert rec.processar(_evento("Cria uma página para o Ritchie"), {})[0] == 200
    assert reg["buscas"] == 0


def test_nao_responde_sobre_a_propria_entrada():
    rec, reg = _receptor(tarefa={**TAREFA, "entrada_id": "entrada-nova"})
    assert rec.processar(_evento("Terminou?"), {})[0] == 200
    assert reg["enviados"] == [] and reg["cancelados"] == []
