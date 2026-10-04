"""O que o agente tem de acesso NESTA execucao, dito no prompt.

Por que existe: o agente respondeu que nao sabia se tinha GitHub e Vercel e
publicou uma pagina por deploy ANONIMO da Vercel (`temporary-*.vercel.app`),
que expira em 60 minutos e fica publico. O AGENTS.md diz o que ele pode, mas
nao diz o que esta presente de fato. Esta linha diz, lendo o ambiente, e nunca
imprime o valor de nenhuma variavel.
"""

from __future__ import annotations

import os
from typing import Mapping


def _tem(env: Mapping[str, str], *nomes: str) -> bool:
    return any((env.get(n) or "").strip() for n in nomes)


def linha_de_ferramentas(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    gh = _tem(env, "GITHUB_TOKEN", "GH_TOKEN")
    vc = _tem(env, "VERCEL_TOKEN")

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
