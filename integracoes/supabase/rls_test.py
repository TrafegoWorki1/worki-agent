"""Testa se o RLS esta valendo de verdade. Rodar: python rls_test.py"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import chave, mascarar  # noqa: E402

SB = chave("SUPABASE_URL").rstrip("/")
SR = chave("SUPABASE_SERVICE_ROLE_KEY")
AN = chave("SUPABASE_ANON_KEY")

print(f"projeto: {SB}")
print(f"service_role: {mascarar(SR)}")
print(f"anon:         {mascarar(AN)}\n")

TAB = "auditoria"          # tabela sensivel, sem policy de leitura
EVENTOS = "paginas_eventos"  # so deve permitir INSERT via anon
METRICAS = "paginas_metricas" # tem policy de leitura publica


def get(tab, key, token):
    req = urllib.request.Request(
        f"{SB}/rest/v1/{tab}?select=*",
        headers={"apikey": token, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def post(tab, body, token):
    # Prefer: return=minimal — NAO usar return=representation. Com
    # representation o PostgREST exige SELECT junto, que o anon nao tem, e
    # devolve 401 "violates row-level security policy" — mensagem enganosa
    # que parece policy quebrada quando nao esta.
    req = urllib.request.Request(
        f"{SB}/rest/v1/{tab}",
        data=json.dumps(body).encode(),
        headers={"apikey": token, "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def ultimo_id(tab):
    """Pega o id do registro mais recente via service_role."""
    req = urllib.request.Request(
        f"{SB}/rest/v1/{tab}?select=id&order=id.desc&limit=1",
        headers={"apikey": SR, "Authorization": f"Bearer {SR}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())[0]["id"]
    except Exception:
        return None


def delete(tab, token, id_):
    req = urllib.request.Request(
        f"{SB}/rest/v1/{tab}?id=eq.{id_}",
        headers={"apikey": token, "Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:120]


print("=== 1. service_role escreve (setup do teste) ===")
st, body = post(TAB, {"canal": "teste", "comando": "probe rls",
                      "resultado": "inserido por service_role"}, SR)
print(f"   {st} {body[:90]}")
row_id = None
try:
    row_id = json.loads(body)[0]["id"]
except Exception:
    print("   nao consegui pegar o id")

print("\n=== 2. anon LE a tabela sensivel (deve FALHAR) ===")
st, body = get(TAB, AN, AN)
print(f"   {st} {body[:120]}")
veredito = "RLS OK (bloqueou)" if st == 401 or "[]" == body.strip() and st == 200 else "ATENCAO"
print(f"   -> {veredito}")

print("\n=== 3. anon LE metricas (deve PERMITIR, tabela vazia) ===")
st, body = get(METRICAS, AN, AN)
print(f"   {st} {body[:120]}")

print("\n=== 4. anon INSERE evento de pagina (deve PERMITIR) ===")
st, body = post(EVENTOS, {"pagina": "probe", "tipo": "visita",
                          "origem": "teste-rls"}, AN)
print(f"   {st} {body[:120]}")
evento_id = ultimo_id(EVENTOS)
print(f"   id criado: {evento_id}")

print("\n=== 5. anon INSERE tipo invalido (deve FALHAR, check constraint) ===")
st, body = post(EVENTOS, {"pagina": "probe", "tipo": "hackeado"}, AN)
print(f"   {st} {body[:140]}")

print("\n=== 6. anon DELETA o evento ===")
print("   ATENCAO: o PostgREST devolve 204 mesmo quando o RLS barra o DELETE")
print("   (0 linhas afetadas = 'sucesso' na API). Por isso o teste so nao e")
print("   confiavel. Verificar a sobrevivencia da linha logo abaixo.")
if evento_id:
    st, body = delete(EVENTOS, AN, evento_id)
    print(f"   status: {st} (nao prova nada sozinho)")
    req = urllib.request.Request(
        f"{SB}/rest/v1/{EVENTOS}?select=id&id=eq.{evento_id}",
        headers={"apikey": SR, "Authorization": f"Bearer {SR}"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        sobrou = json.loads(r.read().decode())
    if sobrou:
        print("   VEREDITO: linha SOBREVIVEU -> RLS bloqueou o DELETE (correto)")
        delete(EVENTOS, SR, evento_id)
    else:
        print("   VEREDITO: linha SUMIU -> RLS furado, DELETE passou (PROBLEMA)")
else:
    print("   pulado (evento nao foi criado)")

# limpeza
print("\n=== 7. limpeza (service_role) ===")
if evento_id:
    delete(EVENTOS, SR, evento_id)
    print("   evento de teste removido")
if row_id:
    delete(TAB, SR, row_id)
    print("   log de auditoria removido")
# ---- apendice: o DELETE retornou 204 (deveria falhar) ----
# Causa: o PostgREST devolve 204 tambem quando 0 linhas foram afetadas,
# porem isso NAO prova que a policy deixou passar. Precisa checar
# se a linha ainda existe depois do DELETE.
