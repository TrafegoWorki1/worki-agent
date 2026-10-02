-- Evolucao aditiva do schema existente em portugues. Nao aplicar 001_fila.sql.
alter table public.conversas add column if not exists proprietario_id text;
alter table public.conversas add column if not exists projeto_id text;
alter table public.conversas add column if not exists resumo text;
alter table public.mensagens add column if not exists conversa_id uuid references public.conversas(id);
alter table public.mensagens add column if not exists chat_jid text;
alter table public.entradas add column if not exists conversa_id uuid references public.conversas(id);
alter table public.entradas add column if not exists ordem bigint generated always as identity;
alter table public.entradas add column if not exists execucao_iniciada_em timestamptz;
create unique index if not exists uq_entradas_mensagem on public.entradas(mensagem_id);
create index if not exists idx_entradas_conversa_ordem on public.entradas(conversa_id,ordem);
create index if not exists idx_mensagens_conversa on public.mensagens(conversa_id,criado_em desc);
alter table public.saidas add column if not exists conversa_id uuid references public.conversas(id);
alter table public.saidas add column if not exists disponivel_em timestamptz not null default now();
alter table public.saidas add column if not exists lease_owner text;
alter table public.saidas add column if not exists lease_expires_at timestamptz;
alter table public.saidas drop constraint if exists saidas_status_check;
alter table public.saidas add constraint saidas_status_check
  check(status in ('pendente','enviando','enviada','falhou','incerto'));
create unique index if not exists uq_saidas_entrada on public.saidas(entrada_id) where entrada_id is not null;
create index if not exists idx_saidas_conversa on public.saidas(conversa_id,criado_em);

create table if not exists public.tarefas (
  id uuid primary key default gen_random_uuid(),
  conversa_id uuid not null references public.conversas(id),
  entrada_id uuid unique references public.entradas(id),
  projeto_id text,
  objetivo text not null,
  status text not null default 'ativa' check(status in ('ativa','bloqueada','concluida','falhou','cancelada')),
  etapa_atual text, proxima_acao text, checkpoint jsonb not null default '{}', resultado text,
  criado_em timestamptz not null default now(), atualizado_em timestamptz not null default now()
);
create index if not exists idx_tarefas_conversa on public.tarefas(conversa_id,atualizado_em desc);
create table if not exists public.etapas_tarefa (
  id uuid primary key default gen_random_uuid(),
  tarefa_id uuid not null references public.tarefas(id),
  chave_operacao text not null, input_hash text not null,
  status text not null default 'pendente' check(status in ('pendente','executando','concluida','falhou','incerto')),
  external_id text, evidencia jsonb, criado_em timestamptz not null default now(),
  atualizado_em timestamptz not null default now(), unique(tarefa_id,chave_operacao,input_hash)
);
create table if not exists public.memorias (
  id uuid primary key default gen_random_uuid(), proprietario_id text not null,
  projeto_id text, chave text not null, valor text not null,
  tipo text not null check(tipo in ('fato','preferencia','decisao','hipotese')),
  fonte text not null, confirmada boolean not null default false,
  status text not null default 'ativa' check(status in ('ativa','substituida','revogada')),
  substitui_id uuid references public.memorias(id), criado_em timestamptz not null default now()
);
create unique index if not exists uq_memoria_ativa on public.memorias(proprietario_id,projeto_id,chave)
  nulls not distinct where status='ativa';
alter table public.acoes_pendentes add column if not exists conversa_id uuid references public.conversas(id);
alter table public.acoes_pendentes add column if not exists tarefa_id uuid references public.tarefas(id);
alter table public.acoes_pendentes add column if not exists alvo text;
alter table public.acoes_pendentes drop constraint if exists acoes_pendentes_status_check;
alter table public.acoes_pendentes add constraint acoes_pendentes_status_check
  check(status in ('aguardando','confirmada','consumida','cancelada','executada','falhou','expirada'));
create index if not exists idx_acoes_conversa on public.acoes_pendentes(conversa_id,status,expira_em);

-- INVOKER: o backend ja usa service_role, nao precisamos elevar privilegios.
create or replace function public.worki_registrar_evento(
  p_instancia text,p_chat_jid text,p_remetente text,p_provider_message_id text,p_texto text,
  p_tipo text default 'conversation')
returns table(entrada_id uuid,mensagem_id uuid,conversa_id uuid,duplicado boolean)
language plpgsql security invoker set search_path=public,pg_temp as $$
#variable_conflict use_column
declare c uuid; m uuid; e uuid; novo boolean;
begin
  if p_instancia is distinct from 'agent-domintante' or p_remetente is distinct from '558592494552@s.whatsapp.net'
    or coalesce(p_provider_message_id,'')='' or coalesce(btrim(p_texto),'')=''
    or coalesce(p_chat_jid,'')='' then raise exception 'evento invalido'; end if;
  insert into conversas(instancia,chat_jid,participante_jid,proprietario_id)
    values(p_instancia,p_chat_jid,p_remetente,split_part(p_remetente,'@',1))
    on conflict(canal,instancia,chat_jid) do update set atualizado_em=now()
    returning id into c;
  insert into mensagens(instancia,provider_message_id,de,grupo_jid,tipo_mensagem,texto,conversa_id,chat_jid)
    values(p_instancia,p_provider_message_id,p_remetente,
      case when p_chat_jid like '%@g.us' then p_chat_jid end,p_tipo,p_texto,c,p_chat_jid)
    on conflict(instancia,provider_message_id) where provider_message_id is not null
      do nothing returning id into m;
  novo := m is not null;
  if not novo then
    select id into m from mensagens where instancia=p_instancia and provider_message_id=p_provider_message_id
      and de=p_remetente and texto=p_texto and conversa_id=c;
    if m is null then raise exception 'evento duplicado com dados diferentes'; end if;
  end if;
  insert into entradas(mensagem_id,conversa_id) values(m,c)
    on conflict(mensagem_id) do nothing;
  select id into e from entradas where entradas.mensagem_id=m;
  return query select e,m,c,not novo;
end $$;

create or replace function public.worki_reservar_entrada(p_owner text,p_lease_s int default 120)
returns setof public.entradas language plpgsql security invoker set search_path=public,pg_temp as $$
declare c uuid; e uuid;
begin
  if coalesce(p_owner,'')='' or p_lease_s is null or p_lease_s<30 or p_lease_s>3600 then raise exception 'lease invalida'; end if;
  -- Trava a conversa, incluindo workers disputando mensagens diferentes dela.
  select cv.id into c from conversas cv where exists(
    select 1 from entradas x where x.conversa_id=cv.id and x.status='aguardando'
      and x.disponivel_em<=now() and not exists(select 1 from entradas anterior
        where anterior.conversa_id=cv.id and anterior.ordem<x.ordem
          and anterior.status not in ('concluida','cancelada')))
    and not exists(select 1 from entradas x where x.conversa_id=cv.id and x.status in ('processando','falhou'))
    order by (select min(ordem) from entradas where conversa_id=cv.id and status='aguardando')
    for update of cv skip locked limit 1;
  if c is null then return; end if;
  select id into e from entradas where conversa_id=c and status='aguardando' order by ordem limit 1;
  return query update entradas set status='processando',tentativas=tentativas+1,
    lease_owner=p_owner,lease_expires_at=now()+make_interval(secs=>p_lease_s),atualizado_em=now()
    where id=e returning *;
end $$;

create or replace function public.worki_renovar_lease(p_entrada_id uuid,p_owner text,p_lease_s int default 120)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
begin
  if p_lease_s is null or p_lease_s<30 or p_lease_s>3600 then raise exception 'lease invalida'; end if;
  update entradas set lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_s),atualizado_em=now()
    where id=p_entrada_id and status='processando' and lease_owner=p_owner
      and lease_expires_at>clock_timestamp();
  return found;
end $$;

create or replace function public.worki_iniciar_entrada(p_entrada_id uuid,p_owner text)
returns uuid language plpgsql security invoker set search_path=public,pg_temp as $$
declare t uuid;
begin
  update entradas set execucao_iniciada_em=clock_timestamp(),atualizado_em=now()
    where id=p_entrada_id and status='processando' and lease_owner=p_owner
      and lease_expires_at>clock_timestamp() and execucao_iniciada_em is null;
  if not found then raise exception 'lease perdida ou execucao ja iniciada'; end if;
  insert into tarefas(conversa_id,entrada_id,projeto_id,objetivo,etapa_atual,proxima_acao)
    select e.conversa_id,e.id,c.projeto_id,m.texto,'Hermes iniciado','Aguardar resultado'
    from entradas e join mensagens m on m.id=e.mensagem_id join conversas c on c.id=e.conversa_id
    where e.id=p_entrada_id returning id into t;
  return t;
end $$;

create or replace function public.worki_checkpoint_tarefa(p_tarefa_id uuid,p_owner text,
  p_etapa text,p_proxima_acao text,p_checkpoint jsonb)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
begin
  perform 1 from entradas e join tarefas t on t.entrada_id=e.id
    where t.id=p_tarefa_id and e.status='processando' and e.lease_owner=p_owner
      and e.lease_expires_at>clock_timestamp() for update of e;
  if not found then return false; end if;
  update tarefas set etapa_atual=p_etapa,proxima_acao=p_proxima_acao,
    checkpoint=p_checkpoint,atualizado_em=now() where id=p_tarefa_id;
  return found;
end $$;

create or replace function public.worki_concluir_entrada(p_entrada_id uuid,p_owner text,
  p_session_id text,p_resposta text,p_sucesso boolean default true)
returns uuid language plpgsql security invoker set search_path=public,pg_temp as $$
declare e entradas%rowtype; s uuid; chat text;
begin
  if coalesce(btrim(p_resposta),'')='' then raise exception 'resposta vazia'; end if;
  select * into e from entradas where id=p_entrada_id for update;
  if not found then raise exception 'entrada inexistente'; end if;
  if e.status='concluida' then
    select id into s from saidas where entrada_id=e.id;
    return s; -- Repetir o commit depois de timeout nao repete o trabalho.
  end if;
  if e.status<>'processando' or e.lease_owner is distinct from p_owner
    or e.lease_expires_at<=clock_timestamp() or e.execucao_iniciada_em is null then
    raise exception 'lease perdida ou execucao nao iniciada'; end if;
  if p_sucesso and coalesce(p_session_id,'')='' then raise exception 'session_id obrigatorio'; end if;
  select chat_jid into chat from conversas where id=e.conversa_id;
  update conversas set session_id=coalesce(p_session_id,session_id),atualizado_em=now() where id=e.conversa_id;
  update tarefas set status=case when p_sucesso then 'concluida' else 'bloqueada' end,
    resultado=p_resposta,etapa_atual='Resultado salvo',
    proxima_acao=case when p_sucesso then 'Aguardar pedido' else 'Revisar execucao antes de retomar' end,
    atualizado_em=now() where entrada_id=e.id;
  insert into saidas(entrada_id,conversa_id,chat_jid,texto,idempotency_key)
    values(e.id,e.conversa_id,chat,p_resposta,'entrada:'||e.id) returning id into s;
  update entradas set status=case when p_sucesso then 'concluida' else 'falhou' end,
    lease_owner=null,lease_expires_at=null,atualizado_em=now() where id=e.id;
  insert into auditoria(canal,remetente,ferramenta,resultado,correlation_id)
    values('whatsapp','proprietario','Hermes',case when p_sucesso then 'sucesso' else 'erro' end,e.id::text);
  return s;
end $$;

create or replace function public.worki_recuperar_leases()
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare n int; s int;
begin
  with recuperadas as (update entradas set status=case when execucao_iniciada_em is null
      then 'aguardando' else 'falhou' end,lease_owner=null,lease_expires_at=null,
      ultimo_erro='Execucao interrompida; revisar se iniciada',atualizado_em=now()
    where status='processando' and lease_expires_at<=clock_timestamp() returning id)
  update tarefas set status='bloqueada',proxima_acao='Revisar execucao interrompida',atualizado_em=now()
    where entrada_id in(select id from recuperadas);
  get diagnostics n=row_count;
  update saidas set status='incerto',ultimo_erro='Envio interrompido; reconciliar'
    where status='enviando' and lease_expires_at<=clock_timestamp();
  get diagnostics s=row_count;
  return jsonb_build_object('tarefas_bloqueadas',n,'envios_incertos',s);
end $$;

create or replace function public.worki_reservar_saida(p_owner text,p_lease_s int default 120)
returns setof public.saidas language plpgsql security invoker set search_path=public,pg_temp as $$
declare c uuid; s uuid;
begin
  if coalesce(p_owner,'')='' or p_lease_s is null or p_lease_s<60 or p_lease_s>3600 then raise exception 'lease invalida'; end if;
  select cv.id into c from conversas cv where exists(select 1 from saidas x
    where x.conversa_id=cv.id and x.status in ('pendente','falhou') and x.tentativas<5
      and x.disponivel_em<=now() and not exists(select 1 from saidas anterior
        join entradas ae on ae.id=anterior.entrada_id join entradas xe on xe.id=x.entrada_id
        where anterior.conversa_id=cv.id and ae.ordem<xe.ordem and anterior.status<>'enviada'))
    and not exists(select 1 from saidas x where x.conversa_id=cv.id and x.status in ('enviando','incerto'))
    order by cv.criado_em for update of cv skip locked limit 1;
  if c is null then return; end if;
  select x.id into s from saidas x join entradas e on e.id=x.entrada_id
    where x.conversa_id=c and x.status in ('pendente','falhou') order by e.ordem limit 1;
  return query update saidas set status='enviando',tentativas=tentativas+1,lease_owner=p_owner,
    lease_expires_at=now()+make_interval(secs=>p_lease_s) where id=s returning *;
end $$;

create or replace function public.worki_registrar_envio(p_saida_id uuid,p_owner text,
  p_estado text,p_provider_message_id text default null)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
begin
  if p_estado not in ('enviada','falhou','incerto') then raise exception 'estado invalido'; end if;
  update saidas set status=p_estado,provider_message_id=p_provider_message_id,
    enviado_em=case when p_estado='enviada' then now() end,
    disponivel_em=now()+make_interval(secs=>least(300,5*power(2,least(tentativas,6))::int)),
    ultimo_erro=case when p_estado='incerto' then 'Reconciliacao necessaria'
      when p_estado='falhou' then 'Envio recusado' end,lease_owner=null,lease_expires_at=null
    where id=p_saida_id and status='enviando' and lease_owner=p_owner
      and lease_expires_at>clock_timestamp();
  return found;
end $$;

create or replace function public.worki_pedir_aprovacao(p_conversa_id uuid,p_acao text,p_alvo text,
  p_payload_hash text,p_descricao text,p_payload jsonb,p_tarefa_id uuid default null)
returns bigint language plpgsql security invoker set search_path=public,pg_temp as $$
declare a bigint; validade interval;
begin
  validade:=case p_acao when 'merge' then interval '24 hours' when 'deploy_producao' then interval '30 minutes'
    when 'campanha_anuncio' then interval '2 hours' when 'alterar_orcamento' then interval '2 hours'
    when 'pausar_anuncio' then interval '30 minutes' when 'gasto' then interval '1 hour' end;
  if validade is null or coalesce(p_payload_hash,'') !~ '^[0-9a-f]{64}$' or coalesce(p_alvo,'')='' then
    raise exception 'acao ou artefato invalido'; end if;
  perform 1 from conversas where id=p_conversa_id for update;
  if not found then raise exception 'conversa inexistente'; end if;
  if p_tarefa_id is not null and not exists(select 1 from tarefas where id=p_tarefa_id and conversa_id=p_conversa_id)
    then raise exception 'tarefa pertence a outra conversa'; end if;
  select id into a from acoes_pendentes where conversa_id=p_conversa_id and acao=p_acao
    and alvo=p_alvo and payload_hash=p_payload_hash and status in ('aguardando','confirmada')
    and expira_em>clock_timestamp() order by id desc limit 1;
  if a is not null then return a; end if;
  update acoes_pendentes set status='cancelada' where conversa_id=p_conversa_id and acao=p_acao
    and alvo=p_alvo and status in ('aguardando','confirmada') and payload_hash is distinct from p_payload_hash;
  insert into acoes_pendentes(descricao,payload,acao,payload_hash,expira_em,conversa_id,tarefa_id,alvo)
    values(p_descricao,p_payload,p_acao,p_payload_hash,clock_timestamp()+validade,p_conversa_id,p_tarefa_id,p_alvo)
    returning id into a;
  return a;
end $$;

create or replace function public.worki_aprovar_acao(p_conversa_id uuid,p_palavra text,p_aprovador text,
  p_acao_id bigint default null)
returns bigint language plpgsql security invoker set search_path=public,pg_temp as $$
declare a acoes_pendentes%rowtype; n int;
begin
  if p_aprovador is distinct from '558592494552@s.whatsapp.net' then raise exception 'aprovador nao autorizado'; end if;
  perform 1 from conversas where id=p_conversa_id for update;
  select count(*) into n from acoes_pendentes where conversa_id=p_conversa_id and status='aguardando'
    and expira_em>clock_timestamp() and (p_acao_id is null or id=p_acao_id);
  if n<>1 then raise exception 'aprovacao ausente ou ambigua'; end if;
  select * into a from acoes_pendentes where conversa_id=p_conversa_id and status='aguardando'
    and expira_em>clock_timestamp() and (p_acao_id is null or id=p_acao_id) for update;
  if ((p_palavra='aprova' and a.acao='merge') or (p_palavra='sobe' and a.acao='deploy_producao')
    or (p_palavra='confirma' and a.acao in ('campanha_anuncio','alterar_orcamento','pausar_anuncio','gasto'))) is not true
    then raise exception 'palavra nao autoriza esta acao'; end if;
  update acoes_pendentes set status='confirmada',aprovador=p_aprovador,decidido_em=now() where id=a.id;
  return a.id;
end $$;

create or replace function public.worki_consumir_aprovacao(p_acao_id bigint,p_conversa_id uuid,
  p_acao text,p_alvo text,p_payload_hash text)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
begin
  update acoes_pendentes set status='consumida',consumida_em=now() where id=p_acao_id
    and conversa_id=p_conversa_id and acao=p_acao and alvo=p_alvo and payload_hash=p_payload_hash
    and status='confirmada' and aprovador='558592494552@s.whatsapp.net'
    and expira_em>clock_timestamp() and consumida_em is null;
  return found;
end $$;

create or replace function public.worki_salvar_memoria(p_proprietario text,p_projeto text,p_chave text,
  p_valor text,p_tipo text,p_fonte text,p_confirmada boolean default false)
returns uuid language plpgsql security invoker set search_path=public,pg_temp as $$
declare anterior uuid; nova uuid;
begin
  if p_proprietario is distinct from '558592494552' or coalesce(p_chave,'')='' or coalesce(p_fonte,'')=''
    then raise exception 'memoria sem proprietario, chave ou fonte'; end if;
  -- Lock transacional por chave para impedir duas versoes ativas concorrentes.
  perform pg_advisory_xact_lock(hashtextextended(p_proprietario||':'||coalesce(p_projeto,'')||':'||p_chave,0));
  select id into anterior from memorias where proprietario_id=p_proprietario
    and projeto_id is not distinct from p_projeto and chave=p_chave and status='ativa';
  update memorias set status='substituida' where id=anterior;
  insert into memorias(proprietario_id,projeto_id,chave,valor,tipo,fonte,confirmada,substitui_id)
    values(p_proprietario,p_projeto,p_chave,p_valor,case when p_confirmada then p_tipo else 'hipotese' end,
      p_fonte,p_confirmada,anterior) returning id into nova;
  return nova;
end $$;

alter table public.tarefas enable row level security;
alter table public.etapas_tarefa enable row level security;
alter table public.memorias enable row level security;
revoke all on public.conversas,public.mensagens,public.entradas,public.saidas,
  public.acoes_pendentes,public.tarefas,public.etapas_tarefa,public.memorias from public,anon,authenticated;
grant select,insert,update,delete on public.conversas,public.mensagens,public.entradas,public.saidas,
  public.acoes_pendentes,public.tarefas,public.etapas_tarefa,public.memorias to service_role;
grant usage,select on sequence public.entradas_ordem_seq,public.acoes_pendentes_id_seq,
  public.auditoria_id_seq to service_role;
do $$ declare r record; begin
  for r in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and left(p.proname,6)='worki_'
  loop
    execute format('revoke all on function %s from public,anon,authenticated',r.signature);
    execute format('grant execute on function %s to service_role',r.signature);
  end loop;
end $$;
-- O trigger automatico de RLS e administrativo, nao uma RPC publica.
do $$ begin
  if to_regprocedure('public.rls_auto_enable()') is not null then
    revoke execute on function public.rls_auto_enable() from public,anon,authenticated;
  end if;
end $$;
notify pgrst,'reload schema';
