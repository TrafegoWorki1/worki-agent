"""Carrega as variaveis do .env do repo. Nao imprime valores.

Por que existe: o shell deste ambiente mascara variaveis de ambiente que
parecem segredo, substituindo por `***`. Ler do arquivo evita isso.
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
    for k in list(dados):
        if os.environ.get(k):
            dados[k] = os.environ[k]
    return dados


CFG = carregar()


def chave(nome: str) -> str:
    v = CFG.get(nome, "")
    if not v:
        raise SystemExit(
            f"Falta {nome} no {ARQUIVO}\n"
            f"Copie .env.example para .env e preencha."
        )
    return v


def mascarar(v: str, n: int = 8) -> str:
    if len(v) <= n * 2:
        return "***"
    return f"{v[:n]}...{v[-4:]}"