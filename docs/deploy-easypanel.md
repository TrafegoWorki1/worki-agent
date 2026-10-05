# Deploy no EasyPanel — worki-agent

Data: 02/10/2026. Servico: `worki-agent`, no projeto `n8n`.
Estado: **sem deploy**. Este documento e o procedimento, nao a prova de
que rodou.

---

## 1. O que este servico e

Um container, dois processos:

```
Evolution ──► receptor HTTP (:8080 /webhooks/evolution)
                    │
                    ▼
            Supabase (entradas)
                    │
                    ▼
            worker (reserva, Hermes, tarefas)
                    │
                    ▼
            Supabase (saidas) ──► Evolution ──► WhatsApp
```

O receptor responde assim que grava. O worker faz o trabalho. Separar
assim evita que uma tarefa de 30 minutos segure a conexao da Evolution.

O Hermes **nao** e daemon nesta versao. O worker chama
`hermes chat -q` por tarefa, usando o login que esta no volume `/opt/data`.

---

## 2. Variaveis obrigatorias

Todas no painel do EasyPanel, no servico `worki-agent`. Nenhuma tem valor
neste documento.

### Canal

| Variavel | Origem |
|---|---|
| `EVOLUTION_API_URL` | URL base que **ja** atende a Evolution. Nao e URL de webhook. |
| `EVOLUTION_API_KEY` | Chave da Evolution (header `apikey`). |
| `EVOLUTION_INSTANCE` | `agent-domintante` — grafia sem o "i". Confirmado no deploy atual. |
| `EVOLUTION_WEBHOOK_TOKEN` | Opcional. So se a versao instalada exigir token por instancia. |

### Banco

| Variavel | Observacao |
|---|---|
| `SUPABASE_URL` | `https://wxqwtyotkkshdjzzwjsk.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | **Somente no backend.** Nunca com prefixo `VITE_`/`NEXT_PUBLIC_`, nunca no bundle da pagina. |

### Allowlist (AGENTS.md secao 2)

| Variavel | Valor esperado |
|---|---|
| `WHATSAPP_NUMERO_AUTORIZADO` | `558592494552` (seu WhatsApp). O `5585999515154` e o Telegram e **nao** vai aqui. |
| `WHATSAPP_GRUPO_COMANDO_JIDS` | Lista de JIDs separados por virgula. **Vazia = nenhum grupo permitido.** |
| `WHATSAPP_AGENTE_NUMERO` | `558586661513`. Recebe e responde, nao comanda. |

Sobre os grupos: a configuracao atual tem zero grupos e a regra antiga
(`GRUPOS.length > 0 && ...`) fazia lista vazia **aceitar** qualquer
grupo. O receptor novo nega por padrao. Se voce nao configurar nenhum
grupo, nenhum grupo entra — que e o comportamento correto.

### Servico

| Variavel | Padrao | Para que |
|---|---|---|
| `PORT` | `8080` | Porta interna do receptor. |
| `WORKI_WEBHOOK_SECRET` | — | Segredo do webhook. Se vazio, o receptor aceita sem conferir. Configure. |
| `WORKI_WORKER_CONCURRENCY` | `1` | Uma tarefa por vez. |
| `WORKI_WORKER_LEASE_S` | `120` | Validade da lease. O worker renova a cada ~40s. |
| `WORKI_RECOVERY_POLL_SECONDS` | `3` | Intervalo entre passadas do worker. |
| `WORKI_TASK_TIMEOUT_SECONDS` | `1800` | Limite de uma execucao do Hermes. |
| `WORKI_WORKSPACE_DIR` | `/workspace` | Volume de trabalho. |
| `WORKI_HERMES_HOME` | `/opt/data` | Estado do Hermes. |
| `WORKI_WORKER_ID` | hostname-pid | So se quiser fixar a identidade da worker. |
| `LOG_LEVEL` | `INFO` | `DEBUG` so para diagnostico. |

### Credenciais dentro do container

O Hermes precisa de autenticar. **O login nao vem de variavel de
ambiente deste servico** — ele fica em `/opt/data/auth.json` (OAuth Nous)
ou nas chaves em `/opt/data/.env`.

Isso significa: o primeiro deploy **nao** vai funcionar sem esse arquivo.
Sao tres caminhos:

1. **`hermes setup --portal` dentro do container**, uma vez, com o volume
   ja montado. O refresh token persiste no volume.
2. Montar o `/opt/data` de uma maquina que ja tem o Hermes autenticado.
   So funciona se for **o mesmo** `/opt/data`; um Hermes instalado
   separado no computador nao compartilha sessao.
3. Configurar provedor via API key (sem OAuth). Verificar os nomes reais
   das credenciais na versao fixada.

Nao existe uma variavel "HERMES_API_KEY" generica. Verificar a versao
antes de inventar nome.

---

## 3. Porta e exposicao

- Porta interna: **8080**
- A URL publica nao existe ate a exposicao HTTP estar configurada. Ela
  tera a forma:

```
https://<host-fornecido-pelo-EasyPanel>/webhooks/evolution
```

**Nao inventar o hostname.** Pegar na configuracao de exposicao do
servico, depois de confirmar a porta interna. Se exigir DNS/domino
proprio, isso e mudanca de dominio e precisa de aprovacao do dono
(AGENTS.md secao 4).

Nao publicar API aberta do agente. O receptor so expoe
`/webhooks/evolution`, `/health` e `/ready`.

---

## 4. Volumes

| Volume | Caminho | Contem | Se nao montar |
|---|---|---|---|
| Estado do Hermes | `/opt/data` | `config.yaml`, `.env`, `auth.json`, `state.db`, `sessions/`, `skills/` | **Toda sessao se perde a cada deploy.** Sem sessao, sem `--resume`. |
| Workspace | `/workspace` | Checkout, arquivos gerados, trabalho inacabado | Arquivos de trabalho somem no update. |

Nao montar volume vazio sobre `/app` — o codigo esta empacotado ali.

### O risco do WAL (verificar antes do primeiro deploy)

O `state.db` do Hermes roda em **WAL**. A documentacao oficial avisa
explicitamente:

> Bind mounts que cruzam fronteira de VM (virtiofs, 9p, drvfs) nao
> fornecem shared memory coerente. Escritores concorrentes corrompem o
> banco WAL **em silencio**, enquanto o arquivo principal ainda passa no
> `PRAGMA integrity_check`.

Ou seja: se o volume do `/opt/data` for um bind mount NFS/9p, o resume
de sessao **quebra sem erro visivel**. O sintoma seria o agente esquecer
contexto aleatoriamente.

**Verificar o tipo de mount.** Se nao for nativo (ext4/xfs local):

- usar volume nomeado Docker (vive no ext4 da VM), ou
- rodar a conversao offline:
  ```sh
  docker exec worki-agent python3 -c "import sqlite3; print(sqlite3.connect('/opt/data/state.db').execute('PRAGMA journal_mode=DELETE').fetchone()[0])"
  ```
  e botar `database.journal_mode: delete` no `config.yaml` montado.

E o que a VPS relatada (4 cores, 15 GB RAM, disco 192 GB) sugere: 15 GB
com o Hermes e possivel, mas **medir antes** (`MemAvailable`, swap, OOM).
Build de pagina com browser pode estourar. Comecar com 1 vCPU e
1,5-2 GiB e validar com tarefa real.

---

## 5. ANTES do primeiro deploy

Tres coisas pendentes, em ordem:

### 5.1 Fixar a imagem do Hermes por digest

O `Dockerfile` hoje usa `nousresearch/hermes-agent:latest`. Isso **nao**
e reversivel: um deploy pode puxar imagem diferente da testada e um bug
aparece como mistério.

```sh
# escolher uma versao testada e pegar o digest
docker manifest inspect nousresearch/hermes-agent:X.Y.Z | grep digest
```

Depois, trocar no `Dockerfile`:
```dockerfile
ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:...
```

**Pendente.** Nao fixei digest sem rodar o smoke test — fixar sem testar
etrai a garantia que o digest deveria dar.

### 5.2 Smoke test com Hermes autenticado

Roda local, sem producao:

```sh
docker build -t worki-agent:teste .
docker run --rm -it \
  -e EVOLUTION_API_URL=... -e SUPABASE_URL=... \
  -v worki-data:/opt/data \
  worki-agent:teste /bin/sh -c "hermes setup --portal"
```

Depois, um pedido que **nao** produza efeito externo:
"responda APENAS a palavra PING". Confirmar que:
- a resposta volta
- `state.db` foi criado em `/opt/data`
- `hermes sessions list` mostra a sessao com source `oneshot`

### 5.3 Banco

As RPCs do contrato (`docs/contrato-rpcs.md`) precisam estar aplicadas.
Ate la o receptor responde **200** mas devolve **500** em toda
persistencia, e o worker loga "fila indisponivel" a cada 30s.

---

## 6. Procedimento de deploy

1. Confirmar 5.1, 5.2 e 5.3.
2. No EasyPanel: apontar o repositorio `TrafegoWorki1/worki-agent`,
   branch `feat/fila-sessoes-outbox` (ou `master` apos o merge).
3. Configurar as variaveis da secao 2. Sem `WORKI_WEBHOOK_SECRET` o
   webhook fica aberto a quem souber a URL.
4. Montar os dois volumes.
5. Expor HTTP na porta 8080 e **anotar o host real**.
6. Deploy. Conferir:
   - `GET /health` → 200 (so prova que o processo responde)
   - `GET /ready` → 200 (prova que banco e worker estao de pe)
7. Testar o webhook com um evento de teste.
8. **So entao** repontar a Evolution.

---

## 7. Rollback

Ordem importa: o receptor antigo (Edge Function) e o receptor novo nao
podem ficar ativos ao mesmo tempo. Dois receptores para o mesmo evento
geram job duplicado — e job duplicado roda o Hermes duas vezes, que cria
branch, commit e preview duplicados.

**Rollback:**

1. Na Evolution, voltar o webhook para:
   `https://wxqwtyotkkshdjzzwjsk.supabase.co/functions/v1/evolution-webhook`
2. Parar o servico (ou continuar rodando — sem trafego nao processa nada).
3. Reverter o codigo para o commit anterior.

Verifiquei que a Edge Function responde 200 e reporta configuracao valida
em 02/10/2026. **Nao testei escrita nela** — o rollback e a reversao do
caminho, mas a primeira mensagem depois de migrar precisa ser observada.

**Rollback do banco:** as tabelas sao aditivas. O que o Codex aplicou
pode ficar — nada foi removido. Se precisar reverter RPC, sao `drop
function` e as tabelas novas (todas vazias no momento da analise).

---

## 8. O que ainda NAO esta implementado

Honesto, para ninguem descobrir em producao:

- **Memoria estruturada** e **RAG**: nao existem. O contexto vem so da
  sessao do Hermes. `docs/contrato-rpcs.md` pede a tabela, mas o
  recupero por query nao foi escrito.
- **`task_steps` / chave de operacao por etapa**: nao existe. **Esta e a
  janela residual conhecida:** se o processo cair entre o Hermes responder
  e a saida ser gravada, a entrada volta pela lease vencida e o Hermes
  roda de novo. O idempotency_key da saida fecha as janelas *depois* da
  gravacao; esta, antes.
- **CLI de continuidade no computador**: nao existe. Continuar pelo
  computador hoje e SSH + `hermes --resume <id>` na mesma VPS.
- **Skill `criar-publicar-pagina`**: nao existe. So o modelo em
  `skills/_modelo/`.
- **Guard integrado ao caminho de execucao**: o modulo existe e tem 23
  testes, e o store do Supabase esta escrito, mas **o skill ainda nao o
  chama**. Sem o skill ligando os dois, a regra existe mas nao barra nada.
- **Reconciliacao** depende de `fetchMessages` da Evolution, que **nao foi
  testado** nesta versao. Se o endpoint nao existir, o veredito fica
  `incerto` e a saida nao e reenviada — que e o comportamento seguro, mas
  significa resposta parada ate resolver.
- **Audio e documento**: o receptor aceita (so do dono), transcreve o audio em um
  servidor Whisper (`WHISPER_URL`) e salva o documento em `/workspace/entrada/`.
  Ver `integracoes/midia.py` e AGENTS.md secao 17. A Evolution precisa do
  endpoint `getBase64FromMediaMessage` ou do base64 no webhook; confirmar na
  versao instalada.

---

## 9. Health

| Rota | O que prova | Codigo |
|---|---|---|
| `/health` | O processo responde. Nao diz nada sobre dependencias. | 200 sempre |
| `/ready` | Banco e worker estao de pe. | 200 ou **503** |

O EasyPanel usa `/ready` para nao mandar trafego quando o banco esta
fora. `/health` e barato, para o HEALTHCHECK do Docker.

Diferenciar os dois e o que evita o EasyPanel reiniciar um container
saudavel que so esta esperando o banco.
