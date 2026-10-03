"""Regressao do nome do evento da Evolution 2.3.7.

Capturado ao vivo com a Evolution 2.3.7 real disparando webhook: o
evento chega como "messages.upsert" (minusculo, com ponto). O codigo
comparava apenas com "MESSAGES_UPSERT" e devolvia None, entao TODA
mensagem era ignorada: a Evolution entregava, o receptor aceitava com
200 e o backend nao processava nada.

Os payloads abaixo sao os que a Evolution realmente enviou, gravados
num coletor publico durante a investigacao.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.evolution.webhook import (  # noqa: E402
    TIPOS_ACEITOS, checar_autorizacao, normalizar,
)

CFG = type("C", (), {
    "EVOLUTION_INSTANCE": "agent-domintante",
    "WHATSAPP_NUMERO_AUTORIZADO": "558592494552",
    "WHATSAPP_GRUPO_COMANDO_JIDS": "",
})()


def normaliza(evento):
    return normalizar(evento, "agent-domintante")


def dados_que_casam():
    """Nomes que a Evolution usa, por versao. Todos precisam entrar."""
    return [
        ("2.3.x minusculo com ponto", "messages.upsert"),
        ("antigo caixa alta", "MESSAGES_UPSERT"),
        ("caixa alta com prefixo", "evolution_api.MESSAGES_UPSERT"),
    ]


def principal():
    falhas = []

    for descricao, nome in dados_que_casam():
        ev = {
            "event": nome,
            "instance": "agent-domintante",
            "data": {
                "key": {"id": "MSG1", "remoteJid": "558592494552@s.whatsapp.net",
                        "fromMe": False},
                "messageType": "conversation",
                "message": {"conversation": "oi agente"},
            },
        }
        r = normaliza(ev)
        if r is None:
            falhas.append(f"{descricao} ({nome}): descartado antes de ler o texto")
            continue
        if r.texto != "oi agente":
            falhas.append(f"{descricao}: texto errado {r.texto!r}")
        ok, motivo = checar_autorizacao(r, CFG)
        if not ok:
            falhas.append(f"{descricao}: numero autorizado recusado ({motivo})")

    # A allowlist precisa continuar barrando quem nao e o Herickson.
    ev = {
        "event": "messages.upsert", "instance": "agent-domintante",
        "data": {
            "key": {"id": "X1", "remoteJid": "5511999999999@s.whatsapp.net",
                    "fromMe": False},
            "message": {"conversation": "oi"},
        },
    }
    r = normaliza(ev)
    if r is not None:
        ok, _ = checar_autorizacao(r, CFG)
        if ok:
            falhas.append("numero NAO autorizado foi aceito")

    # DELIVERY_ACK nao tem conteudo e nao pode virar job.
    ack = {
        "event": "messages.upsert", "instance": "agent-domintante",
        "data": {
            "key": {"id": "ACK1", "remoteJid": "558592494552@s.whatsapp.net",
                    "fromMe": False},
            "status": "DELIVERY_ACK",
            "message": {"messageContextInfo": {"threadId": []}},
        },
    }
    if normaliza(ack) is not None:
        falhas.append("DELIVERY_ACK virou evento processavel")

    # Evento que nao e de mensagem continua ignorado.
    chat = {
        "event": "chats.upsert", "instance": "agent-domintante",
        "data": [{"remoteJid": "97307047555229@lid", "unreadMessages": 0}],
    }
    if normaliza(chat) is not None:
        falhas.append("chats.upsert virou evento processavel")

    # Mensagem enviada pelo proprio agente nao entra.
    from_me = {
        "event": "messages.upsert", "instance": "agent-domintante",
        "data": {
            "key": {"id": "M1", "remoteJid": "558592494552@s.whatsapp.net",
                    "fromMe": True},
            "message": {"conversation": "eco"},
        },
    }
    if normaliza(from_me) is not None:
        falhas.append("fromMe=True foi aceito (loop de eco)")

    if falhas:
        print("FALHOU:")
        for f in falhas:
            print(f"  - {f}")
        return 1

    print(f"Passou ({len(dados_que_casam())} grafias de evento + allowlist)")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
