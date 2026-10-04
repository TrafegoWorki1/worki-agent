"""A linha de acessos do prompt: diz o que existe, nunca o valor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.ferramentas import linha_de_ferramentas  # noqa: E402
from integracoes.worker.relay import Relay  # noqa: E402

SEGREDO_GH = "github_pat_" + "X" * 30
SEGREDO_VC = "vcp_" + "Y" * 30


def test_com_os_dois_tokens_diz_disponivel_e_proibe_deploy_anonimo():
    t = linha_de_ferramentas({"GITHUB_TOKEN": SEGREDO_GH, "VERCEL_TOKEN": SEGREDO_VC})
    assert "GitHub (gh e git) DISPONIVEL" in t and "Vercel (vercel) DISPONIVEL" in t
    assert "NUNCA use deploy anonimo" in t
    assert "SEM TOKEN" not in t


def test_nunca_imprime_o_valor_dos_tokens():
    t = linha_de_ferramentas({"GITHUB_TOKEN": SEGREDO_GH, "VERCEL_TOKEN": SEGREDO_VC})
    assert SEGREDO_GH not in t and SEGREDO_VC not in t and "X" * 10 not in t


def test_sem_vercel_manda_avisar_em_vez_de_publicar_anonimo():
    t = linha_de_ferramentas({"GITHUB_TOKEN": SEGREDO_GH})
    assert "Vercel (vercel) SEM TOKEN" in t
    assert "Sem token de Vercel" in t and "avise o Herickson" in t


def test_gh_token_tambem_conta_e_valor_vazio_nao():
    assert "GitHub (gh e git) DISPONIVEL" in linha_de_ferramentas({"GH_TOKEN": SEGREDO_GH})
    assert "GitHub (gh e git) SEM TOKEN" in linha_de_ferramentas({"GITHUB_TOKEN": "  "})


class DB:
    def tarefas_ativas(self, _):
        return []


class Entrada:
    texto, conversa_id, session_id, de = "oi", "c1", None, ""


def test_prompt_traz_acessos_e_brevidade(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SEGREDO_GH)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    r = Relay.__new__(Relay)
    r.db = DB()
    c = r.montar_contexto(Entrada())
    assert "Acessos desta execucao" in c and "Vercel (vercel) SEM TOKEN" in c
    assert "Brevidade: resultado primeiro" in c
    assert SEGREDO_GH not in c
