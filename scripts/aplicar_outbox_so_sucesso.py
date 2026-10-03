"""Aplica a correcao da RPC worki_concluir_entrada no Supabase.

Uso: py -3.12 scripts/aplicar_outbox_so_sucesso.py [--simular]
"""
import pathlib
import sys
import urllib.parse

import psycopg2

REPO = pathlib.Path(__file__).resolve().parents[1]
MIGRACAO = REPO / "supabase" / "migrations" / "20261003120000_outbox_so_no_sucesso.sql"


def dsn(senha):
    """Monta os DSNs de tentativa.

    A senha pode conterponto, que no pooler (.`usuario.senha@host`) seria
    lido como separador. Por isso ela vai percent-encoded — o psycopg2
    decodifica sozinho. Sem isso o erro e "tenant/user nao encontrado",
    que parece problema de senha e nao e.
    """
    senha_url = urllib.parse.quote(senha, safe="")
    return [
        f"postgresql://postgres:{senha_url}@db.wxqwtyotkkshdjzzwjsk.supabase.co:5432/postgres",
        f"postgresql://postgres.{senha_url}@aws-0-sa-east-1.pooler.supabase.com:5432/postgres",
        f"postgresql://postgres.{senha_url}@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
    ]


def conectar(senha):
    for url in dsn(senha):
        try:
            return psycopg2.connect(url, connect_timeout=15), url.split("@")[1].split(":")[0]
        except Exception as e:
            print(f"  falhou {url.split('@')[1].split(':')[0]}: {str(e)[:70]}")
    return None, None


def main():
    env = {}
    for linha in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            k, v = linha.split("=", 1)
            env[k.strip()] = v.strip()

    print("conectando...")
    con, host = conectar(env["SUPABASE_DB_PASSWORD"])
    if con is None:
        print("FALHOU: sem conexao com o banco")
        return 1
    print(f"conectado em {host}")
    con.autocommit = True
    cur = con.cursor()

    # Estado antes
    cur.execute("select prosrc from pg_proc where proname='worki_concluir_entrada'")
    linha = cur.fetchone()
    antes = linha[0] if linha else ""
    guarda = antes.split("if p_sucesso then")[1].split("update entradas")[0] if "if p_sucesso then" in antes else None
    print("\nANTES: insert em saidas dentro de if p_sucesso?",
          "SIM" if guarda else "NAO (vazava log para a outbox)")

    sql = MIGRACAO.read_text(encoding="utf-8")
    corpo = sql[sql.index("create or replace function"):]
    cur.execute(corpo)
    print("\nmigration aplicada")

    # Estado depois
    cur.execute("select prosrc from pg_proc where proname='worki_concluir_entrada'")
    depois = cur.fetchone()[0]
    tem_guard = "if p_sucesso then" in depois and depois.find("if p_supless") == -1
    pos_guard = depois.find("if p_sucesso then")
    pos_insert = depois.find("insert into saidas")
    print("DEPOIS: insert dentro do guarda de sucesso?",
          "SIM" if 0 <= pos_guard < pos_insert else "NAO")

    cur.execute("select count(*) from saidas where texto like 'hermes chat%'")
    print("\nsaidas com log vazado:", cur.fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())