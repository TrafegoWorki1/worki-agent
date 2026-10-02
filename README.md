# worki-agent

Agente Dominante. Um repositorio, dois canais: terminal e WhatsApp.

## O que e

Skills (o que o agente sabe fazer), automacoes (o que roda sozinho) e
integracoes (com quem o agente conversa). Tudo que voce melhorar aqui vale
nos dois canais.

## Estrutura

```
worki-agent/
├── AGENTS.md          # regras que valem para todas as skills
├── skills/            # uma pasta por skill, cada uma com SKILL.md
│   └── _modelo/       # modelo pra copiar
├── automacoes/        # crons: conversao, relatorio, snapshot
├── integracoes/       # evolution, zernio, supabase
├── paginas/           # codigo das paginas (ou repo por cliente)
└── .env.example       # nomes das variaveis, sem valores
```

## Regras

Todas em `AGENTS.md`. As que mais importam:

- **Allowlist**: so `WHATSAPP_NUMERO_AUTORIZADO` comanda o agente
- **Aprovacao**: merge, deploy, anuncio e gasto exigem confirmacao
- **Dominio**: nunca mexer sem avisar
- **Segredo**: nunca no repo, mascarar em log

## Como usar

### Terminal

O Hermes le `AGENTS.md` automaticamente quando voce trabalha neste repo.

### WhatsApp

Instancia `agent-domintante` na Evolution API. Webhook a ser configurado
(ver `integracoes/evolution/README.md`).

## Criando uma skill

1. Copie `skills/_modelo/` para `skills/minha-skill/`
2. Preencha o `SKILL.md`
3. Escreva o comando de teste e rode
4. Commite

## Estado

| Fase | Status |
|---|---|
| 0 — repo e skills | **pronto** |
| 1 — canal WhatsApp | **funcionando** — webhook ativo, "ping"→"pong" testado |
| 2 — grupo de lancamento | pendente (falta criar o grupo e passar o JID) |
| 3 — metricas de pagina | pendente |
| 4 — criar/editar paginas | GitHub e Vercel prontos, pagina nunca foi deployada |
| 5 — Meta Ads (Zernio) | pendente, comeca em leitura |
| 6 — relatorio de concorrentes | pendente |

## Canal WhatsApp

```
WhatsApp (chip 558586661513)
    │ Evolution envia MESSAGES_UPSERT
    ▼
Supabase Edge Function: evolution-webhook
    │ 1. valida allowlist (so 5585999515154 comanda)
    │ 2. grava em auditoria
    │ 3. se autorizado, responde via sendText
    ▼
Evolution API ──► WhatsApp
```

Endpoint: `https://wxqwtyotkkshdjzzwjsk.supabase.co/functions/v1/evolution-webhook`

Health check (GET): mostra o que esta configurado.

Comandos uteis:

```bash
# deploy da funcao
supabase functions deploy evolution-webhook --no-verify-jwt

# aplicar secrets (o CLI quebra, usa a API)
python integracoes/supabase/set_secrets_api.py

# testar o RLS
python integracoes/supabase/rls_test.py

# ver o webhook na Evolution
curl -H "apikey: $EVOLUTION_API_KEY" \
  "$EVOLUTION_API_URL/webhook/find/agent-domintante"
```

Armadilhas conhecidas: `docs/armadilhas.md`. Leia antes de mexer.