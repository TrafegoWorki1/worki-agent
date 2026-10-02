# Integracao Evolution API

Canal WhatsApp do agente.

## Estado atual (2026-10-02)

| Item | Valor | Status |
|---|---|---|
| URL | ver `.env` | responde 200, v2.3.7 |
| Instancia | `agent-domintante` | **open** |
| Chip | 558586661513 (novo, dedicado ao agente) | conectado |
| Webhook | — | **NAO CONFIGURADO** |

Nenhum evento registrado. Sem webhook, o agente nao recebe nada.

## Endpoints usados

Nome varies por versao. Confirmar na doc antes de codar:
<https://doc.evolution-api.com>

| Operacao | Endpoint |
|---|---|
| Listar instancias | `GET /instance/fetchInstances` |
| Status da instancia | `GET /instance/connectionState/{nome}` |
| QR code / conectar | `GET /instance/connect/{nome}` |
| Enviar texto | `POST /message/sendText/{nome}` |
| Webhook config | `POST /webhook/set/{nome}` |

Autenticacao: header `apikey: <EVOLUTION_API_KEY>`

## Passo a passo para configurar o webhook

1. Ter uma URL publica que receba POST. Candidatos:
   - `WorkiDigital/servidor-api` (backend existente)
   - Vercel (mas a pagina ainda nao tem projeto)

2. Registrar o webhook na Evolution:

```bash
curl -X POST "https://<EVOLUTION_URL>/webhook/set/agent-domintante" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "url": "<URL_PUBLICA>/webhook/evolution",
    "webhookByEvents": false,
    "events": [
      "MESSAGES_UPSERT",
      "GROUP_PARTICIPANTS_ADD",
      "GROUP_PARTICIPANTS_REMOVE"
    ]
  }'
```

3. **Conferir** (a API responde 200 mesmo quando o webhook nao funciona):

```bash
curl "https://<EVOLUTION_URL>/webhook/find/agent-domintante" \
  -H "apikey: $EVOLUTION_API_KEY"
```

4. Teste: mandar mensagem no grupo e ver se o endpoint recebe.

## Allowlist

O endpoint **precisa** validar antes de processar:

```
remoteJid da mensagem ∈ {WHATSAPP_GRUPO_COMANDO_JID}?
numero do remetente === WHATSAPP_NUMERO_AUTORIZADO?
```

Se qualquer uma falhar: responder 200 e descartar. Nunca processar.

Nao confiar em `pushName` — e controlado pelo remetente.
Comparar sempre o `remoteJid` / numero, nunca o nome.

## O chip do agente nao comanda

O chip 558586661513 **recebe e responde**. Ele nao esta na allowlist.
Se uma skill permitir que o agente se auto-comande, e um bug.

## Criterio de pronto (Fase 1)

Mensagem "ping" do numero autorizado → resposta "pong".
Mensagem de outro numero → silencio.