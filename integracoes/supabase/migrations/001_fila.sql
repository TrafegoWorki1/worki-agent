-- =============================================================
-- worki-agent — migracao 001: fila duravel, sessoes, tarefas, outbox,
-- aprovacoes e memoria.
--
-- Aditiva. NAO apaga falas nem auditoria.
-- Aplicar:  psql "$SUPABASE_DB_URL" -f integracoes/supabase/migrations/001_fila.sql
--
-- Por que uma fila de verdade e nao a tabela `falas`:
-- `falas` tem 3 estados e nao tem lease. Duas workers pegando a mesma fala
-- rodavam o Hermes duas vezes. Aqui a reserva e atomica (RPC) e a lease
-- tem dono e validade.
-- =============================================================

-- -------------------------------------------------------------
-- 1. Falas: RLS + chave unica do evento
-- -------------------------------------------------------------
-- Faltava o RLS. A tabela guarda conteudo de mensagem do Herickson.
alter table falas enable row level security;
-- Deliberadamente sem policy: so o service_role (backend) toca.
-- service_role bypassa RLS, entao nao depende de policy.

-- O id do evento da Evolution nunca era persistido; a deduplicacao
-- depende dele. Colunas novas sao aditivas, entao linhas antigas
-- (id_provider null) continuam valendo.
alter table falas add column if not exists instance text;
alter table falas add column if not exists chat_jid text;
alter table falas add column if not exists sender_jid text;
alter table falas add column if not exists provider_message_id text;
alter table falas add column if not exists conversation_id bigint;
alter table falas add column if not exists instance_token text;

-- Chave unica do evento. UNIQUE sem entradas: em Postgres, null nunca
-- colide, entao as linhas antigas (id_provider null) nao sao barradas
-- por esse indice. Duas linhas novas com o mesmo evento colidem.
create unique index if not exists uq_falas_evento
  on falas (instance, provider_message_id)
  where provider_message_id is not null;

-- -------------------------------------------------------------
-- 2. Conversas — a sessao Hermes fica presa aqui, nao em `falas`
-- -------------------------------------------------------------
-- Resolver sessao por conversation_id, nunca por "latest" global.
create table if not exists conversations (
  id                bigserial primary key,
  owner_id          text not null,
  canal             text not null default 'whatsapp'
                    check (canal in ('whatsapp','telegram','terminal')),
  instance          text not null,
  chat_jid          text not null,
  project_id        text,
  hermes_session_id text,
  summary           text,
  -- Ordem por conversa. Uma tarefa por vez, nesta ordem.
  ultimo_mensagem_em timestamptz,
  updated_at        timestamptz default now(),
  unique (instance, chat_jid)
);

comment on table conversations is
  'Uma linha por conversa. hermes_session_id e o ID do state.db do Hermes.';

create index if not exists idx_conversations_owner
  on conversations (owner_id, updated_at desc);

-- -------------------------------------------------------------
-- 3. inbound_messages — o evento cru, uma vez so
-- -------------------------------------------------------------
-- `falas` guarda a conversa. Aqui fica o evento, com a chave que
-- permite dizer "esse evento ja foi visto" sem depender de estado de job.
create table if not exists inbound_messages (
  id                  bigserial primary key,
  conversation_id     bigint not null references conversations(id) on delete cascade,
  instance            text not null,
  chat_jid            text not null,
  sender_jid          text not null,
  provider_message_id text,
  texto               text,
  tipo_mensagem       text,
  instance_token      text,
  received_at         timestamptz default now(),
  -- Evenementos duplicados da Evolution caem aqui: o ON CONFLICT
  -- devolve o id existente e o receptor nao cria job novo.
  unique (instance, chat_jid, provider_message_id)
);

create index if not exists idx_inbound_conv_recebido
  on inbound_messages (conversation_id, received_at desc);

-- Liga a fala (legado) ao evento.
alter table falas
  drop constraint if exists falas_conversation_id_fkey;
alter table falas
  add constraint falas_conversation_id_fkey
  foreign key (conversation_id) references conversations(id) on delete set null;

-- -------------------------------------------------------------
-- 4. Jobs — fila com reserva atomica e lease
-- -------------------------------------------------------------
create table if not exists jobs (
  id           bigserial primary key,
  message_id   bigint not null references inbound_messages(id) on delete cascade,
  status       text not null default 'pendente'
               check (status in ('pendente','reservado','concluido','falhou','descartado')),
  attempts     int not null default 0,
  available_at timestamptz not null default now(),
  -- Lease: dono e validade. O worker renova; a validade vencida devolve
  -- o job para a fila sem ninguem precisar limpar.
  lease_owner  text,
  lease_until  timestamptz,
  last_error   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  -- Um job por mensagem. Dois inserts concorrentes nao duplicam trabalho.
  unique (message_id)
);

-- Indice do caminho quente: pendente, disponivel, mais antigo primeiro.
create index if not exists idx_jobs_disponivel
  on jobs (status, available_at)
  where status in ('pendente','reservado');

-- -------------------------------------------------------------
-- 5. tasks — objetivo e proxima acao, independentes do resumo de chat
-- -------------------------------------------------------------
create table if not exists tasks (
  id           bigserial primary key,
  project_id   text,
  conversation_id bigint references conversations(id) on delete set null,
  objective    text not null,
  status       text not null default 'ativa'
               check (status in ('ativa','bloqueada','concluida','falhou','cancelada')),
  current_step text,
  next_action  text,
  -- Checkpoint serializado: o executor grava aqui depois de cada etapa
  -- material e o resume le daqui. Texto, nao jsonb, porque o conteudo
  -- evolui por fases e o snapshot e reescrito inteiro.
  checkpoint   text,
  result       text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index if not exists idx_tasks_status on tasks (status, updated_at desc);
create index if not exists idx_tasks_conv    on tasks (conversation_id, status);

-- -------------------------------------------------------------
-- 6. task_steps — chave de operacao unica (idempotencia)
-- -------------------------------------------------------------
-- Um step ja concluido nao roda de novo. input_hash detects que o
-- pedido mudou: mesmo step_key com hash diferente = nova operacao.
create table if not exists task_steps (
  id           bigserial primary key,
  task_id      bigint not null references tasks(id) on delete cascade,
  step_key     text not null,
  input_hash   text,
  status       text not null default 'pendente'
               check (status in ('pendente','rodando','concluido','falhou','pulado')),
  external_id  text,
  evidence     text,
  completed_at timestamptz,
  unique (task_id, step_key)
);

-- -------------------------------------------------------------
-- 7. outbox — entrega separada do raciocinio
-- -------------------------------------------------------------
-- O worker grava aqui e so depois envia. Se o envio falhar, a entrega
-- repete sem o Hermes rodar de novo.
--
-- `status` distingue entregue de 'incerto': a Evolution aceitou e a
-- resposta morreu antes de guardar o id. 'incerto' reconcilia em vez de
-- reenviar as cegas.
create table if not exists outbox (
  id                 bigserial primary key,
  conversation_id    bigint not null references conversations(id) on delete cascade,
  task_id            bigint references tasks(id) on delete set null,
  destino            text not null,
  response_text      text not null,
  status             text not null default 'pendente'
                     check (status in ('pendente','enviando','entregue','falhou','incerto')),
  attempts           int not null default 0,
  available_at       timestamptz not null default now(),
  provider_message_id text,
  last_error         text,
  sent_at            timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists idx_outbox_pendente
  on outbox (status, available_at)
  where status in ('pendente','enviando','incerto');

-- -------------------------------------------------------------
-- 8. approvals — guard de acao protegida (AGENTS.md secao 3)
-- -------------------------------------------------------------
-- O guard consulta ESTA tabela antes de executar. Texto no prompt
-- nao e controle. Vinculada a alvo, artefato, acao e validade, e
-- consumida uma vez (status vira 'consumida').
--
-- artifact_hash e o que invalida: se o artefato muda depois da
-- aprovacao, o hash deixa de bater e a aprovacao vale para nada.
create table if not exists approvals (
  id           bigserial primary key,
  task_id      bigint references tasks(id) on delete cascade,
  conversation_id bigint references conversations(id) on delete set null,
  action       text not null,
  target       text not null,
  input_hash   text,
  artifact_hash text,
  status       text not null default 'pendente'
               check (status in ('pendente','aprovada','consumida','rejeitada','expirada')),
  requested_by text not null,
  approved_by  text,
  approved_at  timestamptz,
  expires_at   timestamptz not null,
  created_at   timestamptz not null default now()
);

create index if not exists idx_approvals_pendente
  on approvals (task_id, action, status, expires_at);

-- -------------------------------------------------------------
-- 9. RPC: reserva atomica
-- -------------------------------------------------------------
-- Por que RPC e nao GET seguido de PATCH: entre o GET e o PATCH outra
-- worker le a mesma linha e as duas executam. Aqui o UPDATE ... WHERE
-- status='pendente' decide o vencedor dentro da transacao — so uma
-- linha muda, a outra recebe zero rows e sai.
--
-- SECURITY DEFINER: o worker nao precisa de grant de tabela. Revogar
-- de public/anon e o que impede executar pela internet.
create or replace function worki_reservar_job(
  p_owner     text,
  p_agora     timestamptz,
  p_lease_s   int default 60,
  p_limite    int default 1
)
returns setof jobs
language plpgsql
security definer
set search_path = public
as $$
begin
  return query
  with candidatos as (
    select j.id
      from jobs j
     where j.status = 'pendente'
       and j.available_at <= p_agora
     order by j.available_at asc
     -- Trava as linhas candidatas no instante da selecao, dentro da
     -- mesma transacao. A segunda worker espera aqui, e quando o
     -- lock libera ja ve status='reservado' e pula.
     for update skip locked
     limit p_limite
  )
  update jobs j
     set status      = 'reservado',
         lease_owner = p_owner,
         lease_until = p_agora + make_interval(secs => p_lease_s),
         attempts    = j.attempts + 1,
         updated_at  = p_agora
   from candidatos c
   where j.id = c.id
  returning j.*;
end;
$$;

revoke all on function worki_reservar_job(text, timestamptz, int, int) from public;
revoke all on function worki_reservar_job(text, timestamptz, int, int) from anon;
revoke all on function worki_reservar_job(text, timestamptz, int, int) from authenticated;

-- -------------------------------------------------------------
-- 10. RPC: concluir / falhar
-- -------------------------------------------------------------
create or replace function worki_concluir_job(
  p_job_id  bigint,
  p_owner   text,
  p_erro    text default null
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_erro is null then
    update jobs
       set status = 'concluido', lease_owner = null, lease_until = null,
           updated_at = now()
     where id = p_job_id and lease_owner = p_owner and status = 'reservado';
  else
    -- Falha devolve para a fila com backoff. Tentativas esgotadas
    -- viram 'falhou' em vez de reprocessar para sempre.
    update jobs
       set status = case when attempts >= 5 then 'falhou' else 'pendente' end,
           available_at = now() + make_interval(secs => least(300, power(2, attempts)::int * 5)),
           last_error = left(p_erro, 500),
           lease_owner = null, lease_until = null,
           updated_at = now()
     where id = p_job_id and lease_owner = p_owner and status = 'reservado';
  end if;
end;
$$;

revoke all on function worki_concluir_job(bigint, text, text) from public;
revoke all on function worki_concluir_job(bigint, text, text) from anon;
revoke all on function worki_concluir_job(bigint, text, text) from authenticated;

-- Recupera leases vencidos: worker morreu no meio, job volta para a fila.
create or replace function worki_recuperar_leases(p_agora timestamptz default now())
returns int
language plpgsql
security definer
set search_path = public
as $$
declare n int;
begin
  update jobs
     set status = 'pendente', lease_owner = null, lease_until = null,
         last_error = 'lease vencido', updated_at = p_agora
   where status = 'reservado' and lease_until < p_agora;
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function worki_recuperar_leases(timestamptz) from public;
revoke all on function worki_recuperar_leases(timestamptz) from anon;
revoke all on function worki_recuperar_leases(timestamptz) from authenticated;

-- Estica a lease de uma tarefa longa. Sem isto, uma tarefa que leva mais
-- que o prazo perde o job para outra worker enquanto a primeira ainda
-- esta trabalhando — e as duas rodam o Hermes. O `lease_owner = p_owner`
-- impede que uma worker estique a lease de outra.
create or replace function worki_renovar_lease(
  p_job_id   bigint,
  p_owner    text,
  p_agora    timestamptz default now(),
  p_lease_s  int default 60
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare ok boolean;
begin
  update jobs
     set lease_until = p_agora + make_interval(secs => p_lease_s),
         updated_at = p_agora
   where id = p_job_id
     and lease_owner = p_owner
     and status = 'reservado'
     and lease_until > p_agora;   -- ja vencida: nao renova, deixa recuperar
  get diagnostics ok = row_count;
  return ok > 0;
end;
$$;

revoke all on function worki_renovar_lease(bigint, text, timestamptz, int) from public;
revoke all on function worki_renovar_lease(bigint, text, timestamptz, int) from anon;
revoke all on function worki_renovar_lease(bigint, text, timestamptz, int) from authenticated;

-- -------------------------------------------------------------
-- 10b. RPC: registrar evento (mensagem + job em UMA transacao)
-- -------------------------------------------------------------
-- Por que uma RPC e nao dois POST do receptor: dois POST sao duas
-- transacoes. Se a mensagem gravar e o job falhar, sobra uma linha em
-- inbound_messages que ninguem processa — o pedido existe mas nunca
-- chega ao worker, e ninguem descobre. Aqui e tudo ou nada.
--
-- Retorna job_id e ja_existia. Duplicata da Evolution devolve a linha
-- existente com ja_existia=true e NAO cria job: o receptor responde 200 e
-- nao ha trabalho duplicado.
create or replace function worki_registrar_evento(
  p_conversation_id    bigint,
  p_instance           text,
  p_chat_jid           text,
  p_sender_jid         text,
  p_provider_message_id text,
  p_texto              text,
  p_tipo               text default 'conversation',
  p_instance_token     text default null
)
returns table (job_id bigint, message_id bigint, ja_existia boolean)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_msg    bigint;
  v_job    bigint;
  v_existe boolean := false;
begin
  if p_provider_message_id is not null then
    select id into v_msg
      from inbound_messages
     where instance = p_instance
       and chat_jid = p_chat_jid
       and provider_message_id = p_provider_message_id;
    if v_msg is not null then
      v_existe := true;
      select j.id into v_job
        from jobs j where j.message_id = v_msg;
      -- Devolve o job existente (ou null se ele foi concluido/descartado).
      -- Chamar de novo o mesmo evento nunca abre trabalho novo.
      return query select v_job, v_msg, true;
      return;
    end if;
  end if;

  insert into inbound_messages
    (conversation_id, instance, chat_jid, sender_jid, provider_message_id,
     texto, tipo_mensagem, instance_token)
  values
    (p_conversation_id, p_instance, p_chat_jid, p_sender_jid,
     p_provider_message_id, p_texto, p_tipo, p_instance_token)
  returning id into v_msg;

  insert into jobs (message_id) values (v_msg) returning id into v_job;

  return query select v_job, v_msg, false;
end;
$$;

revoke all on function worki_registrar_evento(bigint, text, text, text, text, text, text, text) from public;
revoke all on function worki_registrar_evento(bigint, text, text, text, text, text, text, text) from anon;
revoke all on function worki_registrar_evento(bigint, text, text, text, text, text, text, text) from authenticated;

-- Soma a tentativa de entrega da outbox e aplica backoff.
-- PATCH direto nao serve: o PostgREST nao avalia expressao, entao mandar
-- attempts = attempts + 1 gravaria a string num integer.
--
-- O backoff em available_at e o que segura a retentativa: a proxima
-- leitura da outbox filtra por available_at <= now(), entao uma linha
-- recusada espera antes de tentar de novo, em vez de martelar a
-- Evolution a cada rodada do worker.
create or replace function worki_outbox_tentar(p_outbox_id bigint)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update outbox o
     set attempts = o.attempts + 1,
         -- 5s, 10s, 20s, 40s, 80s, teto de 5min. Cedeado.
         available_at = now() + make_interval(
           secs => least(300, (5 * power(2, o.attempts))::int)
         ),
         updated_at = now()
   where o.id = p_outbox_id
     -- Só conta tentativa de quem ainda nao entregou. Recontar depois
     -- de 'entregue' faria o numero crescer sem motivo.
     and o.status in ('pendente','incerto','falhou');
end;
$$;

revoke all on function worki_outbox_tentar(bigint) from public;
revoke all on function worki_outbox_tentar(bigint) from anon;
revoke all on function worki_outbox_tentar(bigint) from authenticated;

-- -------------------------------------------------------------
-- 11. Memoria estruturada (curto e longo prazo)
-- -------------------------------------------------------------
-- `supersedes_id` e o que faz correcao substituir em vez de acumular:
-- o fato novo aponta para o velho, o velho vira 'substituido'. Os dois
-- nao coexistem como verdade.
--
-- Fases: 'fato' (confirmado pelo dono), 'decisao' (com fonte), 'hipotese'
-- (do modelo, nunca promovido a fato sem confirmacao).
create table if not exists memories (
  id           bigserial primary key,
  owner_id     text not null,
  project_id   text,
  type         text not null default 'fato'
               check (type in ('fato','decisao','preferencia','hipotese')),
  key          text not null,
  value        text not null,
  source_id    text,
  valid_from   timestamptz not null default now(),
  supersedes_id bigint references memories(id) on delete set null,
  status       text not null default 'ativo'
               check (status in ('ativo','substituido','revogado')),
  confidence   numeric(3,2) not null default 1.00,
  created_at   timestamptz not null default now(),
  -- Uma chave ativa por owner/projeto. A correcao e um UPDATE de key,
  -- nao uma segunda linha com a mesma chave.
  unique (owner_id, project_id, key, valid_from)
);

create index if not exists idx_memories_ativo
  on memories (owner_id, project_id, valid_from desc)
  where status = 'ativo';

-- -------------------------------------------------------------
-- 12. RLS das tabelas novas
-- -------------------------------------------------------------
-- service_role apenas. O frontend (anon) nao enxerga mensagem, tarefa,
-- aprovacao nem memoria. Sem policy = RLS nega = fechado.
alter table conversations    enable row level security;
alter table inbound_messages enable row level security;
alter table jobs             enable row level security;
alter table tasks            enable row level security;
alter table task_steps      enable row level security;
alter table outbox           enable row level security;
alter table approvals        enable row level security;
alter table memories         enable row level security;

-- Grants explicitos: com RLS ligado e sem grant, so o dono da tabela
-- (postgres) alcanca. service_role precisa do grant para passar pelo
-- RLS sem policy.
grant all on conversations, inbound_messages, jobs, tasks, task_steps,
             outbox, approvals, memories to service_role;

-- A tabela legacy tambem precisa de grant explicito.
grant all on falas to service_role;
