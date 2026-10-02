# Contrato de RPCs — worki-agent (Hermes -> Codex)

Data: 02/10/2026. Autor: Hermes. Para: Codex (Supabase/infra).
Decisao do dono: usar as tabelas existentes em portugues. **A migracao 001
(versao em ingles) NAO sera aplicada.** Este documento substitui aquele.

---

## 1. DDL atual (lido do banco, nao do repo)

Nao ha `supabase/migrations/` nem `config.toml` — o schema foi aplicado ad
hoc. O que existe hoje:

### conversas
```
id                 uuid pk default gen_random_uuid()
canal              text not null default 'whatsapp'
instancia          text not null default 'agent-domintante'
chat_jid           text not null
participante_jid   text null
session_id         text null          <-- a sessao do Hermes fica aqui
criado_em          timestamptz not null default now()
atualizado_em      timestamptz not null default now()
UNIQUE (canal, instancia, chat_jid)
```

### mensagens
```
id                 uuid pk default gen_random_uuid()
instancia          text not null default 'agent-domintante'
provider_message_id text NOT NULL      <-- NOT NULL, e unique parcial
de                 text not null
grupo_jid          text null
tipo_mensagem      text null
texto              text not null
criado_em          timestamptz not null default now()
UNIQUE (instancia, provider_message_id)
```

### entradas  (a fila)
```
id                 uuid pk default gen_random_uuid()
mensagem_id        uuid not null
status             text not null default 'aguardando'
    check in ('aguardando','processando','concluida','falhou','cancelada')
tentativas         int not null default 0
disponivel_em      timestamptz not null default now()
lease_owner        text null
lease_expires_at   timestamptz null
ultimo_erro        text null
criado_em          timestamptz not null default now()
atualizado_em      timestamptz not null default now()
```

### saidas  (a outbox)
```
id                 uuid pk default gen_random_uuid()
entrada_id         uuid null
chat_jid           text not null
texto              text not null
status             text not null default 'pendente'
    check in ('pendente','enviando','enviada','falhou')   <-- SEM 'incerto'
idempotency_key    text null UNIQUE (parcial)
provider_message_id text null
tentativas         int not null default 0
ultimo_erro        text null
criado_em          timestamptz not null default now()
enviado_em         timestamptz null
```

Indices que ja existem e que eu **nao** preciso recriar:
```
conversas_canal_instancia_chat_jid_key   unique (canal, instancia, chat_jid)
uq_mensagens_instancia_provedor          unique (instancia, provider_message_id)
idx_entradas_disponivel                  (status, disponivel_em)
idx_saidas_status                        (status, criado_em)
uq_saidas_idempotency                    unique (idempotency_key)
```

---

## 2. Duas adaptacoes necessarias (leia antes de escrever as RPCs)

### 2.1 `saidas` precisa do estado 'incerto'

Hoje o check e `('pendente','enviando','enviada','falhou')`.

Falta um estado. Quando a Evolution aceita o envio mas a chamada estoura o
timeout, **nao da para saber se a mensagem chegou**. Reenviar manda a mesma
resposta duas vezes para o Herickson. Esse caso hoje cairia em 'falhou' e
seria reenviado as cegas.

Preciso de:

```sql
alter table public.saidas
  drop constraint if exists saidas_status_check;
alter table public.saidas
  add constraint saidas_status_check
  check (status in ('pendente','enviando','enviada','falhou','incerto'));
```

Sem isso, o requisito "envio incerto deve aguardar reconciliacao, sem
reenvio automatico" nao e implementavel.

### 2.2 Falta tabela de aprovacoes

Nao existe. Preciso dela para o guard — a autorizacao tem de ser linha no
banco, com vinculo a conversa, acao, alvo e versao do artefato.

```sql
create table if not exists public.aprovacoes (
  id             uuid primary key default gen_random_uuid(),
  entrada_id     uuid null references public.entradas(id) on delete cascade,
  conversa_id    uuid null references public.conversas(id) on delete cascade,
  acao           text not null,
  alvo           text not null,
  artifact_hash  text not null,
  status         text not null default 'pendente'
                 check (status in ('pendente','aprovada','consumida','rejeitada','expirada')),
  solicitado_por text not null,
  aprovado_por   text null,
  aprovado_em    timestamptz null,
  expira_em      timestamptz not null,
  criado_em      timestamptz not null default now()
);
create index if not exists idx_aprovacoes_pendente
  on public.aprovacoes (conversa_id, acao, status, expira_em);
alter table public.aprovacoes enable row level security;
-- sem policy: so service_role. RPC com SECURITY DEFINER.
```

### 2.3 Falta `entradas.conversa_id`

Hoje `entradas` liga em `mensagens`, e `mensagens` nao tem `conversa_id`.
Para o worker devolver a mensagem ja com a conversa (e evitar um segundo
query no caminho quente), preciso de:

```sql
alter table public.entradas add column if not exists conversa_id uuid
  null references public.conversas(id) on delete cascade;
alter table public.mensagens add column if not exists conversa_id uuid
  null references public.conversas(id) on delete cascade;
```

Se preferir, a alternativa e a RPC `worki_reservar_entrada` devolver
`conversa_id` via join. Qualquer uma das duas esta otimo — me diga qual
vc implementa.

### 2.4 Falta indice de FK

`entradas.mensagem_id` nao tem indice, e e a coluna mais consultada pela
fila. Recomendo:
```sql
create index if not exists idx_entradas_mensagem on public.entradas (mensagem_id);
```

---

## 3. As 6 RPCs

Todas `SECURITY DEFINER`, com `set search_path = public`, e com
`revoke all ... from public, anon, authenticated`.

O `service_role` ja ignora RLS, entao o backend nao depende delas — mas
**nao podem ser executaveis pela internet**. Esse revoke e o que impede.

### RPC 1 — `worki_registrar_evento`

Grava mensagem + entrada em **uma transacao** e deduplica.

```sql
worki_registrar_evento(
  p_instancia            text,
  p_chat_jid             text,
  p_de                   text,
  p_grupo_jid            text  default null,
  p_provider_message_id  text,
  p_tipo_mensagem        text,
  p_texto                text
)
returns table (
  entrada_id   uuid,
  mensagem_id  uuid,
  conversa_id  uuid,
  ja_existia   boolean
)
```

Comportamento exigido:

1. Se `(instancia, provider_message_id)` ja existe: devolve os ids
   existentes com `ja_existia = true` e **nao cria entrada nova**.
   Duplicata da Evolution e sucesso, nao erro.
2. Senao: resolve/cria a conversa por `(canal, instancia, chat_jid)`, grava
   `mensagens`, grava `entradas` (status 'aguardando'), devolve
   `ja_existia = false`.
3. **Tudo ou nada.** Se a mensagem gravar e a entrada falhar, sobra uma
   mensagem que ninguem processa e ninguem descobre. Por isso e RPC e nao
   dois POSTs.
4. `de` ja vem so com digitos, resolvido pelo receptor (ver secao 4).

Por que o receptor depende disso: ele so responde 2xx **depois** do commit.
Se devolver 200 sem persistir, a Evolution marca o evento como entregue e
ele se perde para sempre.

### RPC 2 — `worki_reservar_entrada`

Reserva atomica. Este e o coracao da fila.

```sql
worki_reservar_entrada(
  p_owner       text,
  p_agora       timestamptz,
  p_lease_s     int default 60,
  p_limite      int default 1
)
returns table (
  entrada_id       uuid,
  mensagem_id      uuid,
  conversa_id      uuid,
  session_id       text,
  texto            text,
  de               text,
  chat_jid         text,
  tentativas       int
)
```

Comportamento exigido:

1. Seleciona entradas com `status = 'aguardando'` e
   `disponivel_em <= p_agora`, ordenadas por `disponivel_em` asc.
2. **`for update skip locked`** — obrigatorio. E o que impede duas workers
   de pegarem a mesma linha. Sem isso, duas rodam o Hermes duas vezes e
   criam branch/commit/deploy duplicados.
3. Atualiza para `status='processando'`, `lease_owner=p_owner`,
   `lease_expires_at = p_agora + interval`, `tentativas = tentativas + 1`.
4. **Filtro de conversa:** nao devolver duas entradas da mesma conversa se
   ja existe uma em 'processando'. Duas mensagens da mesma conversa nao
   podem executar ao mesmo tempo — sem isso o Hermes recebe `--resume`
   de duas sessoes concorrentes e o contexto se embaralha.
   Sugestao: `not exists (select 1 from entradas e2 where e2.conversa_id = e.conversa_id and e2.status='processando')`.
   Se vc preferir tratar isso no cliente, me avise — mas o default do meu
   lado e tratar na RPC, porque ai a garantia e do banco e nao do processo.
5. Devolve o `session_id` da conversa junto (join), para o worker nao fazer
   query extra no caminho quente.

### RPC 3 — `worki_renovar_lease`

```sql
worki_renovar_lease(
  p_entrada_id uuid,
  p_owner      text,
  p_agora      timestamptz,
  p_lease_s    int default 60
)
returns boolean
```

Comportamento exigido: renova **apenas** se `lease_owner = p_owner`,
`status='processando'` e a lease **ainda nao venceu**. Se ja venceu, outra
worker pode ter tomado; nao renovar (retorna false). Uma worker nao pode
esticar a lease de outra.

### RPC 4 — `worki_concluir_entrada`

```sql
worki_concluir_entrada(
  p_entrada_id uuid,
  p_owner      text,
  p_erro       text default null
)
returns void
```

Comportamento exigido:

- `p_erro is null`: `status='concluida'`, limpa a lease.
- `p_erro` preenchido: volta para `'aguardando'` com
  `disponivel_em = now() + backoff` e guarda `ultimo_erro`. Apos N
  tentativas (min 5), `'falhou'`.
- **`where lease_owner = p_owner and status='processando'`** nas duas. Sem
  isso, uma worker que perdeu a lease consegue concluir o job de outra.

Backoff sugerido: `least(300, 5 * 2^tentativas)` segundos.

### RPC 5 — `worki_registrar_saida`

Grava a resposta **antes** de enviar. E o que garante que falha de entrega
nao refaca o trabalho.

```sql
worki_registrar_saida(
  p_entrada_id      uuid,
  p_chat_jid        text,
  p_texto           text,
  p_idempotency_key text
)
returns uuid
```

Comportamento exigido:

1. Se `idempotency_key` ja existe, devolve o `id` existente sem criar
   duplicata. A chave e o que fecha a janela entre "gravar saida" e
   "concluir entrada" — o requisito de **nao executar o Hermes de novo**.
2. `texto` vazio: levantar erro. Nao e no-op que marca entrega como feita.
3. Status inicial 'pendente'.

Sugestao de chave: `sha256(entrada_id || ':' || resposta)` ou
`entrada_id || ':' || step_key`. O que importa e que seja **deterministica**
a partir da entrada, para o retry recriar a mesma chave.

### RPC 6 — `worki_marcar_saida`

```sql
worki_marcar_saida(
  p_saida_id             uuid,
  p_status               text,
  p_provider_message_id  text default null,
  p_erro                 text default null
)
returns void
```

Comportamento exigido: atualiza `saidas` e incrementa `tentativas`.
**Precisa ser RPC** porque o PostgREST nao avalia expressao — mandar
`tentativas = tentativas + 1` num PATCH gravaria a string num integer.

Estados: 'enviando' -> 'enviada' | 'falhou' | 'incerto'.

`'incerto'` = timeout ou erro de rede depois do envio. **Nao reenviar.**
A reconciliacao e um passo separado.

---

## 4. Contrato HTTP do receptor (o que o Codex nao precisa fazer, mas precisa saber)

O receptor e meu. Ele chama as RPCs acima via PostgREST. Preciso saber
**uma coisa** da sua parte: a versao instalada da Evolution aceita
autenticacao por header customizado no webhook? Hoje uso `apikey`. Se a
versao mandar token por query string ou header diferente, me diga qual —
afeta o `WORKI_WEBHOOK_SECRET`.

### Resolucao de remetente (feito no receptor, nao no banco)

Em grupo, `remoteJid` e o **grupo**, nao a pessoa. O Blueprint esta certo
em cobrar isso, e o codigo atual esta errado:

```ts
const de = digitos(msg.from ?? remoteJid);  // index.ts:34 — BUG
```

Vou usar `participant` (com fallback para `participantPn`/`participantLid`,
que existem na versao nova) e `remoteJid` como destino da conversa. Se a
identidade autorizada nao resolver, **nao executo** — silencio e o
comportamento correto.

O receptor so enfileira texto. Audio nao entra: exige transcricao
explicita, que nao esta resolvida nesta versao.

---

## 5. RLS e grants — o que ja verifiquei

Verifiquei ao vivo. **Nao ha o que corrigir**, mas registro o estado:

- 14 tabelas, RLS ligado em todas.
- **2 policies no projeto inteiro**: `paginas_eventos` (INSERT anon) e
  `paginas_metricas` (SELECT public). As 12 restantes nao tem policy —
  e isso e o correto, porque service_role ignora RLS.
- Anon tem todos os grants, mas RLS sem policy nega na pratica. Testado:
  GET em `falas` (34 linhas) e `auditoria` (205) devolvem `[]`; INSERT em
  `auditoria` devolve 401.
- DELETE/UPDATE anon devolvem **204 afetando 0 linhas**. Um 204 nao prova
  sucesso. E a armadilha #5 do seu `docs/armadilhas.md`.

O que **preciso** das tabelas novas (`aprovacoes`):
`alter table ... enable row level security;` sem policy, e
`grant all on public.aprovacoes to service_role;`

---

## 6. Risco operacional que preciso que voce verifique no EasyPanel

O `state.db` do Hermes roda em **WAL** (documentacao oficial). A doc
avisa explicitamente que bind mount que cruza fronteira de VM
(9p / drvfS / virtiofs) corrompe WAL **em silencio** — o
`PRAGMA integrity_check` passa e o dado se perde.

O resume de sessao depende do `state.db`. Se o volume do `/opt/data` nao
for nativo, **o teste de fechamento falha sem erro visivel**.

Verifique o tipo de mount. Se nao for nativo, as saidas sao:
- volume nomeado Docker (ext4 nativo), ou
- `database.journal_mode: delete` no `config.yaml` (o Hermes faz a
  conversao offline).

---

## 7. O que NAO e meu (Codex)

- Migracoes e DDL (itens 2.x acima sao pedido, nao vou aplicar).
- RLS, policies, grants, revoke das RPCs.
- Validacao de persistencia.
- Volumes, config e deploy no EasyPanel (servico `worki-agent` no projeto
  `n8n`, ja criado, sem deploy).

## 8. O que NAO e seu (Hermes)

- `integracoes/evolution/webhook.py` (receptor)
- `integracoes/supabase/relay.py` (worker duravel)
- `integracoes/hermes/adapter.py`
- `Dockerfile`, `entrypoint`
- `testes/`

---

## 9. Sequencia

1. Voce revisa este contrato e me diz o que mudou de nome/assinatura.
2. Voce aplica as DDL e as RPCs.
3. Me avisa que rodou.
4. Eu adapto `queue.py`/`outbox.py` aos nomes finais e rodo os testes de
   integracao contra o banco de verdade.
5. So depois disso entra o deploy.

Ate o passo 4, meu codigo fala nomes em ingles que **nao existem** no banco.
Nao rodo nada contra o Supabase antes disso.

Nenhum token, senha ou valor de `.env` neste documento.
