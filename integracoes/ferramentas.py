"""O que o agente tem de acesso NESTA execucao, dito no prompt.

Por que existe: o agente respondeu que nao sabia se tinha GitHub e Vercel e
publicou uma pagina por deploy ANONIMO da Vercel (`temporary-*.vercel.app`),
que expira em 60 minutos e fica publico. O AGENTS.md diz o que ele pode, mas
nao diz o que esta presente de fato.

O que conta e o que o AGENTS consegue USAR, nao o que o container tem: o
Hermes tira GITHUB_TOKEN, GH_TOKEN e VERCEL_TOKEN do ambiente dos comandos do
agente. Por isso a checagem e o arquivo de credencial que os wrappers `gh` e
`vercel` leem (integracoes/agente/preparar_credenciais.sh). Nunca imprime o
valor de nenhum token.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

CREDENCIAIS_PADRAO = "/opt/data/.credenciais"


def _tem(pasta: Path, nome: str) -> bool:
    try:
        return (pasta / nome).read_text().strip() != ""
    except OSError:
        return False


def linha_de_ferramentas(env: Mapping[str, str] | None = None,
                         pasta: str | Path | None = None) -> str:
    env = os.environ if env is None else env
    base = Path(pasta or env.get("WORKI_CREDENCIAIS_DIR") or CREDENCIAIS_PADRAO)
    gh = _tem(base, "github_token")
    vc = _tem(base, "vercel_token")

    partes = [
        "Acessos desta execucao: "
        f"GitHub (gh e git) {'DISPONIVEL' if gh else 'SEM TOKEN'}; "
        f"Vercel (vercel) {'DISPONIVEL' if vc else 'SEM TOKEN'}."
    ]
    if gh or vc:
        partes.append("Use-os direto, sem perguntar se tem acesso.")
    if vc:
        partes.append(
            "Publique sempre na conta autenticada. NUNCA use deploy anonimo "
            "ou temporario da Vercel: expira em 60 minutos e fica publico."
        )
    faltando = [n for n, ok in (("GitHub", gh), ("Vercel", vc)) if not ok]
    if faltando:
        partes.append(
            f"Sem token de {' e '.join(faltando)}: nao contorne nem publique "
            "de forma anonima; avise o Herickson."
        )
    return " ".join(partes)
