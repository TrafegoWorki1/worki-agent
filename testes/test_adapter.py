"""Testes do adaptador Hermes.

O que precisa ser provado aqui, em ordem de risco:

1. Duas falas da MESMA conversa: a segunda enxerga a primeira.
2. Conversas A e B: nao se misturam.
3. Sessao nova quando nao ha id: o contexto anterior nao vaza.
4. stdout sujo (banner, 'restored workspace dir') nao vira resposta.
5. Prompt vazio e binario inexistente falham em vez de inventar texto.

Rodar:  python testes/test_adapter.py
Custo: cada teste que chama o Hermes gasta tokens de verdade.
"""

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from integracoes.hermes.adapter import Adaptador, Resultado, _limpar  # noqa: E402

FALHOU = []
PASSOU = []


def checar(nome: str, condicao: bool, detalhe: str = ""):
    (PASSOU if condicao else FALHOU).append(nome)
    marca = "ok  " if condicao else "FALHA"
    print(f"  [{marca}] {nome}{(' — ' + detalhe) if detalhe else ''}")


def so_puro():
    """Testes que nao chamam o Hermes. Rodam primeiro e de graca."""
    print("\n1. limpeza de stdout (nao chama Hermes)")
    sujo = (
        "↪ restored workspace dir: C:\\projeto\n"
        "│ banner\n"
        "\n"
        "A resposta real do agente.\n"
        "Segunda linha dela.\n"
    )
    limpo = _limpar(sujo)
    checar("banner removido", "restored workspace" not in limpo, repr(limpo[:40]))
    checar("resposta preservada", "A resposta real do agente." in limpo)
    checar("linha interna preservada", "Segunda linha dela." in limpo)

    print("\n2. falha sem chamar Hermes")
    ad = Adaptador(timeout=5)
    r = ad.responder("   ")
    checar("prompt vazio rejeitado", not r.ok and r.texto is None)

    ad2 = Adaptador(binario="hermes-que-nao-existe-xyz", timeout=5)
    r2 = ad2.responder("oi")
    checar("binario ausente nao quebra", not r2.ok and r2.texto is None,
           r2.log[:50])


def main():
    print("=" * 60)
    print("Testes do adaptador — parte 1: sem custo de token")
    print("=" * 60)
    so_puro()

    if "--rapido" in sys.argv:
        print("\n(pulado: testes com o Hermes por causa de --rapido)")
        return 1 if FALHOU else 0

    print("\n" + "=" * 60)
    print("Testes do adaptador — parte 2: sessao (custa token)")
    print("=" * 60)
    ad = Adaptador(timeout=300)

    print("\n3. sessao nova guarda contexto")
    t0 = time.time()
    r1 = ad.responder("Responda APENAS a palavra: ABACAXI")
    checar("primeira chamada ok", r1.ok, r1.log[:120] if not r1.ok else "")
    checar("devolveu session_id", bool(r1.session_id), str(r1.session_id))
    checar("resposta correta", "ABACAXI" in (r1.texto or ""), repr(r1.texto))
    print(f"     ({time.time() - t0:.0f}s)")

    if not (r1.ok and r1.session_id):
        print("\nSem sessao valida: os testes 4 e 5 nao tem como passar.")
        print("Nao marque como verde — isso e falha, nao pular.")
        FALHOU.append("sessao base")

    else:
        print("\n4. segunda fala da MESMA conversa enxerga a primeira")
        r2 = ad.responder(
            "Qual foi a palavra que eu pedi antes? Responda APENAS a palavra.",
            session_id=r1.session_id,
        )
        checar("retomou ok", r2.ok, r2.log[:120] if not r2.ok else "")
        checar("mesma sessao mantida", r2.session_id == r1.session_id,
               f"{r1.session_id} -> {r2.session_id}")
        checar("lembrou do contexto", "ABACAXI" in (r2.texto or "").upper(),
               repr(r2.texto))

        print("\n5. conversa nova NAO enxerga a antiga")
        r3 = ad.responder(
            "Qual foi a palavra que eu pedi antes? Se nao houver contexto, "
            "responda APENAS: NADA",
        )
        checar("abriu sessao nova", r3.session_id != r1.session_id,
               f"{r1.session_id} vs {r3.session_id}")
        checar("sem vazamento de contexto",
               "ABACAXI" not in (r3.texto or "").upper(), repr(r3.texto))

    print("\n" + "=" * 60)
    print(f"Passou: {len(PASSOU)}   Falhou: {len(FALHOU)}")
    if FALHOU:
        for f in FALHOU:
            print(f"  FALHOU: {f}")
    print("=" * 60)
    return 1 if FALHOU else 0


if __name__ == "__main__":
    sys.exit(main())
