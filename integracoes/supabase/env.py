"""Configuracao do servidor: ambiente tem prioridade sobre .env opcional.

Nao imprime valores. No EasyPanel nao e necessario criar um arquivo .env.
"""

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
ARQUIVO = RAIZ / ".env"


def carregar() -> dict:
    dados = {}
    if ARQUIVO.exists():
        for linha in ARQUIVO.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            dados[k.strip()] = v.strip().strip("'\"")
    # env tem prioridade sobre o arquivo
    # Incluir nomes que so existem no ambiente. Um valor explicitamente vazio
    # tambem prevalece: nao reutilizar uma credencial antiga do arquivo.
    dados.update(os.environ)
    return dados


CFG = carregar()


def chave(nome: str) -> str:
    v = CFG.get(nome, "")
    if not v or v.strip() == "***":
        raise SystemExit(
            f"Configure {nome} nas variaveis do servico EasyPanel "
            "ou no .env local. Valor ausente ou mascarado."
        )
    return v


def mascarar(v: str, n: int = 8) -> str:
    if len(v) <= n * 2:
        return "***"
    return f"{v[:n]}...{v[-4:]}"
