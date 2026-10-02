#!/usr/bin/env python
"""Seta as secrets da Edge Function pela API de management do Supabase.

Por que nao pelo CLI: `supabase secrets set` entra em panic neste plano
(secrets/set/set.go). A API de management funciona.

As chaves vem do .env porque o shell mascara env var que parece segredo.

Rodar:  python set_secrets_api.py
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import CFG, mascarar  # noqa: E402

PROJECT = "wxqwtyotkkshdjzzwjsk"
API = "https://api.supabase.com/v1"
TOKEN = CFG.get("SUPABASE_ACCESS_TOKEN", "")

# variavel no .env  ->  nome da secret
MAPA = {
    "EVOLUTION_API_URL": "EVOLUTION_API_URL",
    "EVOLUTION_API_KEY": "EVOLUTION_API_KEY",
    "EVOLUTION_INSTANCE": "EVOLUTION_INSTANCE",
    "WHATSAPP_NUMERO_AUTORIZADO": "WHATSAPP_NUMERO_AUTORIZADO",
    "WHATSAPP_GRUPO_COMANDO_JID": "WHATSAPP_GRUPO_COMANDO_JIDS",
    "WHATSAPP_AGENTE_NUMERO": "WHATSAPP_AGENTE_NUMERO",
    "SUPABASE_SERVICE_ROLE_KEY": "SUPABASE_SERVICE_ROLE_KEY",
}

OPCIONAIS = {"WHATSAPP_GRUPO_COMANDO_JID", "WHATSAPP_AGENTE_NUMERO"}

if not TOKEN:
    raise SystemExit("Falta SUPABASE_ACCESS_TOKEN no .env. Rode `supabase login` e atualize.")

faltando = [k for k in MAPA if not CFG.get(k) and k not in OPCIONAIS]
if faltando:
    raise SystemExit(f"FALTANDO no .env: {', '.join(faltando)}")


def call(metodo, path, body=None):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# Nao precisamos reenviar as secrets padrao do Supabase (SUPABASE_URL,
# SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY...). A API recusa com
# "Secret name must not start with the SUPABASE_ prefix", e a Edge Function
# recebe SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY automaticamente.
st, body = call("GET", f"/projects/{PROJECT}/secrets")
existentes = json.loads(body) if st == 200 else []
tem_padrao = {s["name"] for s in existentes}
print(f"Secrets padrao injetadas automaticamente: {len(tem_padrao)}")
print(f"  {', '.join(sorted(tem_padrao))}\n")

# Variaveis que o Supabase ja injeta na Edge Function — nao definimos.
JA_INJETADAS = {"SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"}

merged = []
aplicadas = []
puladas = []
for origem, destino in MAPA.items():
    if destino in JA_INJETADAS:
        print(f"  {destino}: ja injetada pelo Supabase, pulando")
        continue
    v = CFG.get(origem)
    if not v:
        puladas.append(destino)
        continue
    merged.append({"name": destino, "value": v})
    aplicadas.append((destino, v))

if not aplicadas:
    raise SystemExit("Nada a aplicar.")

print(f"\nAplicando {len(aplicadas)} secrets:")
for nome, v in aplicadas:
    print(f"  {nome} = {mascarar(v)}")
if puladas:
    print(f"\nSem valor, puladas: {', '.join(puladas)}")

st, body = call("POST", f"/projects/{PROJECT}/secrets", merged)
print(f"\n{'OK  ' if st in (200, 201) else 'FALHA'} POST secrets -> {st}")
if st not in (200, 201):
    print(body[:400])
else:
    st2, b2 = call("GET", f"/projects/{PROJECT}/secrets")
    print("\nConferindo:")
    for s in json.loads(b2) if st2 == 200 else []:
        print(f"  - {s['name']}")