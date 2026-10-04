"""Memoria de longo prazo: isolamento, segredo, limite e comando.

Sem rede: o banco e substituido por um falso.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes import memoria  # noqa: E402
from integracoes.worker.relay import Relay  # noqa: E402

DONO = "558592494552"
OUTRO = "5511999990000"


def m(chave="preferencia_tom", valor="respostas curtas", **kw):
    base = dict(proprietario_id=DONO, projeto_id=None, chave=chave, valor=valor,
                tipo="preferencia", confirmada=True, status="ativa")
    base.update(kw)
    return base


# --- o que chega ao prompt -------------------------------------------

def test_so_confirmada_ativa_do_dono_e_global_entra():
    linhas = [
        m("ok"),
        m("de_outro", proprietario_id=OUTRO),
        m("hipotese", confirmada=False, tipo="hipotese"),
        m("velha", status="substituida"),
        m("outro_projeto", projeto_id="proj-b"),
    ]
    chaves = [x["chave"] for x in memoria.memorias_visiveis(linhas, DONO)]
    assert chaves == ["ok"]


def test_memoria_do_projeto_atual_entra_e_a_de_outro_nao():
    linhas = [m("a", projeto_id="proj-a"), m("b", projeto_id="proj-b"), m("g")]
    chaves = [x["chave"] for x in memoria.memorias_visiveis(linhas, DONO, "proj-a")]
    assert sorted(chaves) == ["a", "g"]


def test_segredo_nunca_vai_ao_prompt_mesmo_se_estiver_no_banco():
    linhas = [m("chave_x", valor="use sk_" + "a1" * 16),
              m("ok", valor="gosta de numeros em destaque")]
    bloco = memoria.bloco_de_memorias(linhas, DONO)
    assert "sk_" not in bloco and "ok:" in bloco


def test_limite_e_truncamento():
    linhas = [m(f"chave_{i:02d}", valor="x" * 1000) for i in range(30)]
    bloco = memoria.bloco_de_memorias(linhas, DONO)
    corpo = [l for l in bloco.splitlines() if l.startswith("- ")]
    assert len(corpo) == memoria.LIMITE_NO_PROMPT
    assert all(len(l) < memoria.MAX_VALOR_NO_PROMPT + 40 for l in corpo)


def test_sem_memorias_nao_cria_bloco():
    assert memoria.bloco_de_memorias([], DONO) == ""
    assert memoria.bloco_de_memorias([m(confirmada=False, tipo="hipotese")], DONO) == ""


def test_so_o_dono_tem_memoria(monkeypatch):
    monkeypatch.setenv("WHATSAPP_NUMERO_AUTORIZADO", DONO)
    monkeypatch.delenv("WORKI_MEMORIA_DONO", raising=False)
    assert memoria.dono_do_prompt(f"{DONO}@s.whatsapp.net") == DONO
    assert memoria.dono_do_prompt(f"{OUTRO}@s.whatsapp.net") is None
    assert memoria.dono_do_prompt("") is None


# --- validacao para gravar -------------------------------------------

@pytest.mark.parametrize("valor", [
    "sk_" + "e1" * 20, "ghp_" + "A" * 30, "github_pat_" + "B" * 30,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw", "apikey_" + "c" * 20,
    "-----BEGIN PRIVATE KEY-----", "a" * 0 + "0123456789abcdef" * 3,
])
def test_recusa_segredo(valor):
    with pytest.raises(ValueError, match="segredo"):
        memoria.validar_para_salvar("chave_ok", valor, "fato", "origem")


def test_recusa_chave_tipo_valor_e_fonte_invalidos():
    for args in [("Chave Ruim", "v", "fato", "f"), ("chave_ok", "v", "xyz", "f"),
                 ("chave_ok", "  ", "fato", "f"), ("chave_ok", "v", "fato", ""),
                 ("chave_ok", "x" * 601, "fato", "f")]:
        with pytest.raises(ValueError):
            memoria.validar_para_salvar(*args)
    memoria.validar_para_salvar("chave_ok", "valor normal", "fato", "origem")


# --- comando ----------------------------------------------------------

class DBFalso:
    def __init__(self, mensagens=()):
        self.salvas, self._msgs = [], set(mensagens)

    def mensagem_e_do_dono(self, mid, dono):
        return mid in self._msgs

    def salvar_memoria(self, *a):
        self.salvas.append(a)
        return "uuid-1"

    def memorias_do_dono(self, dono):
        return [m("preferencia_tom"), m("vazou", valor="sk_" + "a1" * 16)]


@pytest.fixture(autouse=True)
def _dono(monkeypatch):
    monkeypatch.setenv("WHATSAPP_NUMERO_AUTORIZADO", DONO)
    monkeypatch.delenv("WORKI_MEMORIA_DONO", raising=False)


def cli(db, *argv):
    return memoria.principal(list(argv), db=db)


def test_confirmada_exige_mensagem_do_dono():
    db = DBFalso()
    rc = cli(db, "salvar", "--chave", "preferencia_tom", "--valor", "curto",
             "--tipo", "preferencia", "--fonte", "texto-solto", "--confirmada")
    assert rc == 1 and db.salvas == []


def test_confirmada_com_mensagem_do_dono_grava():
    mid = "11111111-2222-3333-4444-555555555555"
    db = DBFalso([mid])
    rc = cli(db, "salvar", "--chave", "preferencia_tom", "--valor", "curto",
             "--tipo", "preferencia", "--fonte", mid, "--confirmada")
    assert rc == 0
    assert db.salvas == [(DONO, None, "preferencia_tom", "curto",
                          "preferencia", mid, True)]


def test_sem_confirmada_grava_como_hipotese_com_fonte_livre():
    db = DBFalso()
    rc = cli(db, "salvar", "--chave", "talvez_tarde", "--valor", "pode preferir tarde",
             "--tipo", "hipotese", "--fonte", "observacao do agente")
    assert rc == 0 and db.salvas[0][-1] is False


def test_comando_recusa_segredo_e_nao_grava():
    db = DBFalso()
    rc = cli(db, "salvar", "--chave", "token", "--valor", "ghp_" + "A" * 30,
             "--tipo", "fato", "--fonte", "x")
    assert rc == 1 and db.salvas == []


def test_listar_nao_mostra_segredo(capsys):
    assert cli(DBFalso(), "listar") == 0
    saida = capsys.readouterr().out
    assert "preferencia_tom" in saida and "sk_" not in saida


# --- integracao com o prompt -----------------------------------------

class DBContexto:
    def __init__(self, memorias, falha=False):
        self._m, self._falha = memorias, falha

    def tarefas_ativas(self, _):
        return []

    def memorias_ativas(self, dono):
        if self._falha:
            raise RuntimeError("banco fora")
        return self._m


class Entrada:
    texto = "oi"
    conversa_id = "c1"
    session_id = None
    mensagem_id = "11111111-2222-3333-4444-555555555555"

    def __init__(self, de):
        self.de = de


def contexto(db, de):
    r = Relay.__new__(Relay)
    r.db = db
    return r.montar_contexto(Entrada(de))


def test_prompt_do_dono_traz_memorias_e_id_da_mensagem():
    c = contexto(DBContexto([m(valor="respostas curtas")]), f"{DONO}@s.whatsapp.net")
    assert "Memorias confirmadas" in c and "respostas curtas" in c
    assert "11111111-2222-3333-4444-555555555555" in c


def test_prompt_de_outra_pessoa_nao_traz_memoria():
    c = contexto(DBContexto([m()]), f"{OUTRO}@s.whatsapp.net")
    assert "Memorias confirmadas" not in c and "Id desta mensagem" not in c


def test_falha_do_banco_nao_derruba_o_prompt():
    c = contexto(DBContexto([], falha=True), f"{DONO}@s.whatsapp.net")
    assert "pt-BR" in c
