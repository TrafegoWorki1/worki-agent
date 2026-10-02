#!/usr/bin/env python
"""Relay de conversa: le as falas pendentes e devolve resposta no WhatsApp.

Roda na maquina do Herickson. Sem deploy, sem tunel, sem expor o Hermes.
A Edge Function so enfileira; este script responde.

Fluxo:
    WhatsApp -> Evolution -> Edge Function -> tabela falas
                                          (relay.py le aqui)
    WhatsApp <- Evolution <- responder <- grava resposta

Por que o Hermes nao fica exposto na internet: nao precisa. A Edge Function
so conhece o Supabase, e este script roda localmente com as credenciais do
proprio .env.

Rodar:  python relay.py            (uma vez, processa a fila e sai)
        python relay.py --loop     (fica em polling a cada --intervalo)
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import chave  # noqa: E402

SB = chave("SUPABASE_URL").rstrip("/")
SR = chave("SUPABASE_SERVICE_ROLE_KEY")
EV_URL = chave("EVOLUTION_API_URL").rstrip("/")
EV_KEY = chave("EVOLUTION_API_KEY")
INSTANCE = chave("EVOLUTION_INSTANCE") or "agent-domintante"

HEADERS = {"apikey": SR, "Authorization": f"Bearer {SR}", "Content-Type": "application/json"}


def agora():
    """Timestamp ISO-8601 em UTC.

    O PATCH do PostgREST nao avalia expressoes: mandar a string 'now()'
    gravaria literalmente isso num timestamptz. Calcular aqui.
    """
    return datetime.now(timezone.utc).isoformat()


def pedir(metodo, caminho, body=None, headers=None):
    req = urllib.request.Request(
        f"{SB}{caminho}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers or HEADERS,
        method=metodo,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        texto = r.read().decode()
        return json.loads(texto) if texto else []


def pendentes():
    """Falas aguardando resposta, mais antiga primeiro."""
    return pedir(
        "GET",
        "/rest/v1/falas?status=eq.aguardando&order=criado_em.asc&limit=20",
    )


def responder_no_whatsapp(numero, texto):
    """Manda a resposta. Devolve True se a Evolution aceitou."""
    req = urllib.request.Request(
        f"{EV_URL}/message/sendText/{INSTANCE}",
        data=json.dumps({"number": numero, "text": texto}).encode(),
        headers={"apikey": EV_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
            with urllib.request.urlopen(req, timeout=45) as r:
                # A Evolution devolve 201 Created em envio aceito, nao 200.
                # Aceitar so 200 faz todo envio parecer falha, e a fala fica
                # na fila para sempre mesmo tendo sido entregue.
                return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        print(f"  sendText falhou: {e.code} {e.read().decode()[:150]}")
        return False
    except Exception as e:
        print(f"  sendText erro: {e}")
        return False


def marcar_respondida(fala_id, resposta):
    """Marca como respondida. Falha aqui re-enfileira a fala na proxima rodada."""
    pedir(
            "PATCH",
            f"/rest/v1/falas?id=eq.{fala_id}",
            body={"status": "respondida", "resposta": resposta, "respondido_em": agora()},
            headers={**HEADERS, "Prefer": "return=minimal"},
        )


def marcar_ignorada(fala_id, motivo):
    pedir(
        "PATCH",
        f"/rest/v1/falas?id=eq.{fala_id}",
        body={"status": "ignorada", "resposta": motivo},
        headers={**HEADERS, "Prefer": "return=minimal"},
    )


def responder_a(fala):
    """
    Chama o Hermes em modo one-shot e devolve a resposta.

    `hermes -z` roda o agente completo: as mesmas ferramentas, memoria e
    skills desta sessao. O relay nao inventa um prompt proprio — o agente
    le as skills do repo e o AGENTS.md, entao o contexto e o mesmo.

    Por que -z e nao uma chamada HTTP na API do modelo: `hermes -z` usa o
    login que ja existe (OAuth Nous), sem chave nova no .env. E o agente
    vem completo — uma chamada HTTP traria so o texto, sem ferramenta.

    Arquivo em `respostas/<id>.txt` tem prioridade: e o override manual,
    para quando eu responder na mao.

    Timeout generoso: o agente pode rodar terminal, git e rede. O padrao
    do relay e 15s de polling, entao uma resposta lenta apenas ocupa
    uma rodada.
    """
    caminho = Path(__file__).resolve().parent / "respostas" / f"{fala['id']}.txt"
    if caminho.exists():
        resposta = caminho.read_text(encoding="utf-8").strip()
        if resposta:
            caminho.unlink()
            return resposta

    texto = fala["texto"]
    try:
        proc = subprocess.run(
            ["hermes", "-z", texto],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return "Demorei demais para responder. Tenta de novo?"
    except FileNotFoundError:
        return "Nao encontrei o hermes no PATH. O relay precisa rodar com ele disponivel."

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        print(f"  hermes -z falhou ({proc.returncode}): {err[:200]}")
        return "Deu erro aqui do meu lado. Tenta de novo em instantes?"

    resposta = proc.stdout.strip()
    if not resposta:
        return None
    return resposta


def processar():
    falas = pendentes()
    if not falas:
        return 0

    print(f"{len(falas)} fala(s) pendente(s)")
    processadas = 0

    for fala in falas:
        fid, de, texto = fala["id"], fala["de"], fala["texto"]
        print(f"\n[{fid}] de ...{de[-4:]}: {texto[:60]}")

        try:
            resposta = responder_a(fala)
        except Exception as e:
            # Nao marcar: a fala volta para a fila na proxima rodada.
            print(f"  erro ao responder: {e}")
            continue

        if not resposta:
            # Sem resposta do Hermes ainda. A fala segue aguardando e o
            # proximo --loop tenta de novo. Melhor esperar do que mandar
            # "nao sei" para o WhatsApp.
            print("  aguardando resposta do Hermes")
            continue

        if not responder_no_whatsapp(de, resposta):
            # Nao marcar: a Evolution nao aceitou, tenta de novo.
            print("  Evolution recusou - continua na fila")
            continue

        marcar_respondida(fid, resposta)
        processadas += 1
        print(f"  respondida: {resposta[:60]}")

    return processadas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="fica rodando")
    ap.add_argument("--intervalo", type=int, default=15, help="segundos entre rodadas")
    args = ap.parse_args()

    if not args.loop:
        processar()
        return

    print(f"Relay rodando a cada {args.intervalo}s. Ctrl+C para parar.")
    while True:
        try:
            processar()
        except Exception as e:
            print(f"erro no relay: {e}")
        time.sleep(args.intervalo)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nparado")