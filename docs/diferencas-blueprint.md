# Diferencas entre o blueprint e o HEAD real (02/10/2026)

Base: TrafegoWorki1/worki-agent @ 756c8fe (master, privado, 5 commits, 20 arquivos).
Verificado: git clone + leitura + 2 execucoes reais do Hermes + GET no webhook atual.

## Confirmado do blueprint (achados conferidos no HEAD)

| Achado | Onde | Estado |
|---|---|---|
| relay chama `hermes -z` sem sessao | integracoes/supabase/relay.py:140 | CONFIRMADO |
| polling 15s | relay.py:212 | CONFIRMADO (`--intervalo` default 15) |
| entrega antes de marcar a fala | relay.py:187-192 | CONFIRMADO |
| env.py so sobrepoe nomes do .env | env.py:28-30 | CONFIRMADO |
| grupo sem participant | index.ts:34 (`msg.from ?? remoteJid`) | CONFIRMADO |
| chave do evento nao persistida | index.ts:48 (`id` extraido, nunca gravado) | CONFIRMADO |
| 200 sempre | index.ts:243 | CONFIRMADO |
| lista de grupo vazia libera grupo | index.ts:63 | CONFIRMADO |
| .env.example singular vs plural | .env.example:26 vs index.ts:16 | CONFIRMADO |
| falas sem RLS no arquivo | schema_falas.sql:11-22 | CONFIRMADO |
| sem Dockerfile | (ausente) | CONFIRMADO |

## Novidade do HEAD que o blueprint NAO viu (commit 756c8fe)

O HEAD e mais recente que o ZIP analisado. Ele JA.moveu a conversa para
"conversa livre" e o relay agora roda na maquina do Herickson. O blueprint
desenha um servico na VPS; o HEAD ainda e local. As duas coisas coexistem:
o receptor na VPS substitui a Edge Function, mas o relay continua util como
fallback/rollback.

## Correcoes de fato ao blueprint

1. **`--pass-session-id` NAO serve.** Ele coloca o id no system prompt do agente
   (texto), nao no stdout. Nao da para parsear. O relay atual nao tem esse bug
   porque nem tenta. Metodo deterministico correto: snapshot de
   `max(started_at)` em `state.db` antes do run, depois localizar a sessao
   criado depois — validado nesta maquina.

2. **`-z` + `--resume ID` FUNCIONA e devolve so a resposta final.** Testado:
   sessoes `20261002_192323_6dc950` retomada respondeu "SESSAO" (lembrou do
   pedido anterior); run novo respondeu "NOVA". `state.db` fica em
   `$HERMES_HOME/state.db`. A combinacao one-shot + resume por ID e a base do
   adaptador.

3. **Numero de autorizacao.** O README (linha 71) diz "so 5585999515154 comanda"
   e esse e o numero do TELEGRAM. O AGENTS.md e explicito: a allowlist do
   WhatsApp e 558592494552. O README esta errado. Blueprint mandou usar AGENTS.md
   como referencia — confirmado, e o README precisa de correcao.

4. **Estado do webhook antigo.** `GET` na URL do README responde 200 e reporta
   `{allowlist:true, grupos:0, evolution:true, instance:"agent-domintante"}`.
   Esta VIVO hoje. A grafia `agent-domintante` (sem o "i") e a real do
   deploy — o blueprint mandou nao corrigir, e a Instances API confirma.

5. **`grupos: 0`** na configuracao atual. Com lista vazia e a regra atual
   (`GRUPOS.length > 0 && ...`), grupo nao autorizado e silenciosamente aceito.
   Isso e um furo real hoje, nao hipotetico.

6. **RLS de `falas`.** Nao esta habilitada. Nao ha policy, entao o anon nao le
   nada — mas o `falas` e a tabela mais sensivel do sistema (conteudo de
   mensagem do Herickson). Falta o `enable row level security`.

7. **WAL em bind mount.** A doc do Docker e explicita: `state.db` roda em WAL e
   bind mount que cruza fronteira de VM (9p/drvfs/virtiofs) corrompe em
   silencio. EasyPanel monta volume — se for bind mount de NFS/9p, o adapter
   precisa de volume nativo nomeado ou `journal_mode: delete`.

8. **s6-overlay.** A imagem oficial ja supervisiona `gateway run`. O blueprint
   pede supervisar receptor+worker. O caminho suportado pela imagem e s6, nao
   `&` solto — e o worker/receptor entram como servicos s6 adicionais.

9. **`instances` para conferir nome de instancia.** A doc do Docker mostra
   `docker exec hermes instances list`. Serve para auditar `/opt/data/instances/`
   e o `auth.json` de cada instancia no volume.

## O que o blueprint pede e ainda nao existe em lugar nenhum

- Nenhuma tabela de fila (jobs/leases/outbox/tasks/approvals). O HEAD tem
  `falas` (3 estados) e `acoes_pendentes` (nao-guardado, so log).
- Nenhum guard de aprovacao. `acoes_pendentes` registra intencao; nada consulta
  antes de executar. Um prompt com "aprova" nao passa por lugar nenhum.
- Nenhum `skills/criar-publicar-pagina/`. So `skills/_modelo/`.
- Nenhum Dockerfile, entrypoint, docker-compose, .dockerignore.
- Nenhum `integracoes/hermes/`. Nenhum `integracoes/evolution/webhook.py`.
- Nenhuma memoria estruturada (tabela `memories`) nem RAG.
- `docs/armadilhas.md` e `docs/laya-bugs.md` existem e valem como entrada.
