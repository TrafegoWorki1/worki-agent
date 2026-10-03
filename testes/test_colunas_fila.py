"""Regressao: colunas que o worker le de `mensagens` e `conversas`.

Bug real de 2026-10-03: `reservar()` pedia `select=texto,remetente`, mas a
coluna do remetente chama-se `de`. O PostgREST respondia 400, o worker
falhava ANTES de chamar o Hermes e a entrada voltava para a fila — o
sintoma era "mandou mensagem e o agente nao respondeu", sem erro visivel.

Estes testes existem para o proximo erro de coluna nao custar uma sessao
de depuracao. Nao tocam no banco: leem o codigo-fonte.
"""

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
QUEUE = RAIZ / "integracoes" / "supabase" / "queue.py"

falhas: list[str] = []
total = 0


def checa(descricao: str, condicao: bool) -> None:
    global total
    total += 1
    if condicao:
        print(f"  [ok  ] {descricao}")
    else:
        print(f"  [FALHA] {descricao}")
        falhas.append(descricao)


def main() -> int:
    fonte = QUEUE.read_text(encoding="utf-8")
    # Comentario nao conta: o proprio arquivo cita `remetente` para
    # explicar o bug.
    codigo = "\n".join(
        l for l in fonte.splitlines() if not l.strip().startswith("#")
    )

    print("colunas usadas pelo worker")

    # 1. `de` e a coluna do remetente em `mensagens`.
    selects = re.findall(r"/rest/v1/mensagens\?[^\"']*select=([a-z_,]+)", codigo)
    checa(f"worker le mensagens com 'de' (viu: {selects})",
          bool(selects) and all("de" in s.split(",") for s in selects))

    checa("nenhum select pede 'remetente' em mensagens",
          "select=texto,remetente" not in codigo)

    # 2. A montagem da Entrada le a mesma chave que a consulta pediu.
    checa("Entrada usa m.get('de')", 'm.get("de")' in codigo or "m.get('de')" in codigo)
    checa("Entrada nao usa m.get('remetente')", 'm.get("remetente")' not in codigo
          and "m.get('remetente')" not in codigo)

    # 3. Consulta e montagem nao podem divergir de novo: se uma mudou
    # e a outra nao, o bug reaparece.
    chave_consulta = "de" if "de" in (selects[0].split(",") if selects else []) else "?"
    chave_leitura = "de" if 'm.get("de")' in codigo else "?"
    checa(f"consulta e montagem usam a mesma chave ({chave_consulta}/{chave_leitura})",
          chave_consulta == chave_leitura == "de")

    # 4. Colunas usadas contra o schema versionado.
    print("\ncolunas conferidas contra supabase/migrations/")
    mig = RAIZ / "supabase" / "migrations"
    ddl = "\n".join(f.read_text(encoding="utf-8") for f in mig.glob("*.sql"))
    for tabela, coluna in (("mensagens", "de"), ("conversas", "chat_jid"),
                           ("conversas", "session_id"), ("entradas", "mensagem_id")):
        bloco = re.search(
            rf'create table if not exists public\."{tabela}" \((.*?)\n\);',
            ddl, re.S,
        )
        achou = bool(bloco and re.search(rf"\b{coluna}\b", bloco.group(1)))
        checa(f"{tabela}.{coluna} existe no schema versionado", achou)

    print(f"\nPassou: {total - len(falhas)}   Falhou: {len(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
