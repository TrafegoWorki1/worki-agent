"""A linha de acessos do prompt e os wrappers de credencial.

O que o agente consegue USAR e o que conta: o Hermes tira os tokens do ambiente
dos comandos dele, entao a prova e o arquivo lido pelos wrappers.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from integracoes.ferramentas import linha_de_ferramentas  # noqa: E402
from integracoes.worker.relay import Relay  # noqa: E402

SEGREDO_GH = "github_pat_" + "X" * 30
SEGREDO_VC = "vcp_" + "Y" * 30
AGENTE = RAIZ / "integracoes" / "agente"


def cred(tmp_path, gh=None, vc=None):
    pasta = tmp_path / "cred"
    pasta.mkdir()
    if gh:
        (pasta / "github_token").write_text(gh)
    if vc:
        (pasta / "vercel_token").write_text(vc)
    return pasta


# --- linha do prompt -------------------------------------------------

def test_com_os_dois_arquivos_diz_disponivel_e_proibe_deploy_anonimo(tmp_path):
    t = linha_de_ferramentas({}, cred(tmp_path, SEGREDO_GH, SEGREDO_VC))
    assert "GitHub (gh e git) DISPONIVEL" in t and "Vercel (vercel) DISPONIVEL" in t
    assert "NUNCA use deploy anonimo" in t and "SEM TOKEN" not in t


def test_nunca_imprime_o_valor_dos_tokens(tmp_path):
    t = linha_de_ferramentas({}, cred(tmp_path, SEGREDO_GH, SEGREDO_VC))
    assert SEGREDO_GH not in t and SEGREDO_VC not in t


def test_sem_vercel_manda_avisar_em_vez_de_publicar_anonimo(tmp_path):
    t = linha_de_ferramentas({}, cred(tmp_path, SEGREDO_GH))
    assert "Vercel (vercel) SEM TOKEN" in t
    assert "Sem token de Vercel" in t and "avise o Herickson" in t


def test_variavel_de_ambiente_sozinha_nao_conta(tmp_path):
    """O container pode ter o token no env e o agente ainda assim nao ve."""
    env = {"GITHUB_TOKEN": SEGREDO_GH, "VERCEL_TOKEN": SEGREDO_VC}
    t = linha_de_ferramentas(env, cred(tmp_path))
    assert "GitHub (gh e git) SEM TOKEN" in t and "Vercel (vercel) SEM TOKEN" in t


def test_arquivo_vazio_nao_conta(tmp_path):
    assert "GitHub (gh e git) SEM TOKEN" in linha_de_ferramentas({}, cred(tmp_path, "  "))


class DB:
    def tarefas_ativas(self, _):
        return []


class Entrada:
    texto, conversa_id, session_id, de = "oi", "c1", None, ""


def test_prompt_traz_acessos_e_brevidade(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKI_CREDENCIAIS_DIR", str(cred(tmp_path, SEGREDO_GH)))
    r = Relay.__new__(Relay)
    r.db = DB()
    c = r.montar_contexto(Entrada())
    assert "Acessos desta execucao" in c and "Vercel (vercel) SEM TOKEN" in c
    assert "Brevidade: resultado primeiro" in c and SEGREDO_GH not in c


# --- preparar_credenciais.sh e wrappers -------------------------------

def rodar(args, env, **kw):
    base = {"PATH": os.environ["PATH"], "HOME": str(kw.pop("home", "/tmp"))}
    base.update(env)
    return subprocess.run(args, env=base, capture_output=True, text=True, **kw)


def preparar(tmp_path, env):
    cred_d, bin_d = tmp_path / "c", tmp_path / "b"
    r = rodar(["sh", str(AGENTE / "preparar_credenciais.sh"), str(AGENTE / "bin"),
               str(cred_d), str(bin_d)], env)
    assert r.returncode == 0, r.stderr
    return cred_d, bin_d, r


def falso(tmp_path, nome, var):
    """Binario falso que imprime o valor da variavel que o wrapper deveria passar."""
    f = tmp_path / f"real_{nome}"
    f.write_text(f'#!/bin/sh\nprintf "%s" "${{{var}:-<vazio>}}"\n')
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return f


def test_preparar_grava_arquivos_600_e_nao_vaza_o_valor_no_log(tmp_path):
    cred_d, bin_d, r = preparar(tmp_path, {"GITHUB_TOKEN": SEGREDO_GH, "VERCEL_TOKEN": SEGREDO_VC})
    assert (cred_d / "github_token").read_text() == SEGREDO_GH
    assert (cred_d / "vercel_token").read_text() == SEGREDO_VC
    for n in ("github_token", "vercel_token"):
        assert stat.S_IMODE((cred_d / n).stat().st_mode) == 0o600
    assert stat.S_IMODE(cred_d.stat().st_mode) == 0o700
    assert SEGREDO_GH not in r.stdout + r.stderr and SEGREDO_VC not in r.stdout + r.stderr
    for f in ("gh", "vercel", "git-credential-worki"):
        assert os.access(bin_d / f, os.X_OK)


def test_gh_token_tambem_vale_e_token_ausente_remove_o_arquivo(tmp_path):
    cred_d, _, _ = preparar(tmp_path, {"GH_TOKEN": SEGREDO_GH})
    assert (cred_d / "github_token").read_text() == SEGREDO_GH
    assert not (cred_d / "vercel_token").exists()
    # novo boot sem o token: o arquivo antigo some, o estado fica explicito
    r = rodar(["sh", str(AGENTE / "preparar_credenciais.sh"), str(AGENTE / "bin"),
               str(cred_d), str(tmp_path / "b")], {})
    assert r.returncode == 0 and not (cred_d / "github_token").exists()


def test_wrapper_gh_passa_o_token_so_ao_processo_dele(tmp_path):
    cred_d, bin_d, _ = preparar(tmp_path, {"GITHUB_TOKEN": SEGREDO_GH})
    real = falso(tmp_path, "gh", "GH_TOKEN")
    r = rodar([str(bin_d / "gh")], {"WORKI_CREDENCIAIS_DIR": str(cred_d),
                                    "WORKI_REAL_GH": str(real)})
    assert r.stdout == SEGREDO_GH


def test_wrapper_vercel_passa_o_token(tmp_path):
    cred_d, bin_d, _ = preparar(tmp_path, {"VERCEL_TOKEN": SEGREDO_VC})
    real = falso(tmp_path, "vercel", "VERCEL_TOKEN")
    r = rodar([str(bin_d / "vercel")], {"WORKI_CREDENCIAIS_DIR": str(cred_d),
                                        "WORKI_REAL_VERCEL": str(real)})
    assert r.stdout == SEGREDO_VC


def test_wrapper_sem_arquivo_nao_inventa_token(tmp_path):
    cred_d, bin_d, _ = preparar(tmp_path, {})
    real = falso(tmp_path, "gh", "GH_TOKEN")
    r = rodar([str(bin_d / "gh")], {"WORKI_CREDENCIAIS_DIR": str(cred_d),
                                    "WORKI_REAL_GH": str(real)})
    assert r.stdout == "<vazio>"


def test_ajudante_do_git_so_responde_para_github(tmp_path):
    cred_d, bin_d, _ = preparar(tmp_path, {"GITHUB_TOKEN": SEGREDO_GH})
    h = str(bin_d / "git-credential-worki")
    env = {"WORKI_CREDENCIAIS_DIR": str(cred_d)}
    ok = rodar([h, "get"], env, input="protocol=https\nhost=github.com\n\n")
    assert f"password={SEGREDO_GH}" in ok.stdout and "username=x-access-token" in ok.stdout
    outro = rodar([h, "get"], env, input="protocol=https\nhost=gitlab.com\n\n")
    assert outro.stdout == ""
    store = rodar([h, "store"], env, input="host=github.com\n\n")
    assert store.stdout == ""
