-- Baseline inspecionado em wxqwtyotkkshdjzzwjsk em 2026-10-02.
-- Estrutura somente; sem dados, segredos ou recriacao de tabelas existentes.
create sequence if not exists public."acoes_pendentes_id_seq";
create sequence if not exists public."auditoria_id_seq";
create sequence if not exists public."concorrentes_id_seq";
create sequence if not exists public."contas_conectadas_id_seq";
create sequence if not exists public."conteudos_concorrentes_id_seq";
create sequence if not exists public."experimentos_id_seq";
create sequence if not exists public."falas_id_seq";
create sequence if not exists public."grupos_snapshots_id_seq";
create sequence if not exists public."paginas_eventos_id_seq";
create sequence if not exists public."paginas_metricas_id_seq";

create table if not exists public."acoes_pendentes" (
id bigint NOT NULL DEFAULT nextval('acoes_pendentes_id_seq'::regclass),
descricao text NOT NULL,
payload jsonb NOT NULL,
canal text DEFAULT 'whatsapp'::text,
status text DEFAULT 'aguardando'::text,
resultado text,
criado_em timestamp with time zone DEFAULT now(),
decidido_em timestamp with time zone,
acao text,
payload_hash text,
aprovador text,
expira_em timestamp with time zone,
consumida_em timestamp with time zone,
entrada_id uuid
);
alter table public."acoes_pendentes" enable row level security;

create table if not exists public."auditoria" (
id bigint NOT NULL DEFAULT nextval('auditoria_id_seq'::regclass),
canal text,
remetente text,
comando text,
skill text,
ferramenta text,
resultado text,
aguardou_aprovacao boolean DEFAULT false,
duracao_ms integer,
criado_em timestamp with time zone DEFAULT now(),
correlation_id text
);
alter table public."auditoria" enable row level security;

create table if not exists public."concorrentes" (
id bigint NOT NULL DEFAULT nextval('concorrentes_id_seq'::regclass),
nome text NOT NULL,
perfil_url text,
palavras_chave text[],
ativo boolean DEFAULT true,
criado_em timestamp with time zone DEFAULT now()
);
alter table public."concorrentes" enable row level security;

create table if not exists public."contas_conectadas" (
id bigint NOT NULL DEFAULT nextval('contas_conectadas_id_seq'::regclass),
provedor text NOT NULL,
conta_id text NOT NULL,
nome text,
escopo text DEFAULT 'leitura'::text,
ativo boolean DEFAULT true,
criado_em timestamp with time zone DEFAULT now()
);
alter table public."contas_conectadas" enable row level security;

create table if not exists public."conteudos_concorrentes" (
id bigint NOT NULL DEFAULT nextval('conteudos_concorrentes_id_seq'::regclass),
concorrente_id bigint,
semana date NOT NULL,
tipo text,
url text,
resumo text,
score numeric,
coletado_em timestamp with time zone DEFAULT now()
);
alter table public."conteudos_concorrentes" enable row level security;

create table if not exists public."conversas" (
id uuid NOT NULL DEFAULT gen_random_uuid(),
canal text NOT NULL DEFAULT 'whatsapp'::text,
instancia text NOT NULL DEFAULT 'agent-domintante'::text,
chat_jid text NOT NULL,
participante_jid text,
session_id text,
criado_em timestamp with time zone NOT NULL DEFAULT now(),
atualizado_em timestamp with time zone NOT NULL DEFAULT now()
);
alter table public."conversas" enable row level security;

create table if not exists public."entradas" (
id uuid NOT NULL DEFAULT gen_random_uuid(),
mensagem_id uuid NOT NULL,
status text NOT NULL DEFAULT 'aguardando'::text,
tentativas integer NOT NULL DEFAULT 0,
disponivel_em timestamp with time zone NOT NULL DEFAULT now(),
lease_owner text,
lease_expires_at timestamp with time zone,
ultimo_erro text,
criado_em timestamp with time zone NOT NULL DEFAULT now(),
atualizado_em timestamp with time zone NOT NULL DEFAULT now()
);
alter table public."entradas" enable row level security;

create table if not exists public."experimentos" (
id bigint NOT NULL DEFAULT nextval('experimentos_id_seq'::regclass),
pagina text NOT NULL,
hipotese text NOT NULL,
pr_url text,
pr_numero integer,
branch text,
taxa_antes numeric(6,4),
taxa_depois numeric(6,4),
status text DEFAULT 'proposto'::text,
criado_em timestamp with time zone DEFAULT now(),
concluido_em timestamp with time zone
);
alter table public."experimentos" enable row level security;

create table if not exists public."falas" (
id bigint NOT NULL DEFAULT nextval('falas_id_seq'::regclass),
de text NOT NULL,
texto text NOT NULL,
status text DEFAULT 'aguardando'::text,
resposta text,
criado_em timestamp with time zone DEFAULT now(),
respondido_em timestamp with time zone
);
alter table public."falas" enable row level security;

create table if not exists public."grupos_snapshots" (
id bigint NOT NULL DEFAULT nextval('grupos_snapshots_id_seq'::regclass),
grupo_jid text NOT NULL,
data date NOT NULL,
total_membros integer NOT NULL,
entradas integer DEFAULT 0,
saidas integer DEFAULT 0,
created_at timestamp with time zone DEFAULT now()
);
alter table public."grupos_snapshots" enable row level security;

create table if not exists public."mensagens" (
id uuid NOT NULL DEFAULT gen_random_uuid(),
instancia text NOT NULL DEFAULT 'agent-domintante'::text,
provider_message_id text,
de text NOT NULL,
grupo_jid text,
tipo_mensagem text,
texto text NOT NULL,
criado_em timestamp with time zone NOT NULL DEFAULT now()
);
alter table public."mensagens" enable row level security;

create table if not exists public."paginas_eventos" (
id bigint NOT NULL DEFAULT nextval('paginas_eventos_id_seq'::regclass),
pagina text NOT NULL,
tipo text NOT NULL,
origem text,
utm_source text,
utm_campaign text,
criado_em timestamp with time zone DEFAULT now()
);
alter table public."paginas_eventos" enable row level security;

create table if not exists public."paginas_metricas" (
id bigint NOT NULL DEFAULT nextval('paginas_metricas_id_seq'::regclass),
pagina text NOT NULL,
janela_inicio timestamp with time zone NOT NULL,
janela_fim timestamp with time zone NOT NULL,
visitas integer NOT NULL DEFAULT 0,
cliques integer NOT NULL DEFAULT 0,
taxa_conversao numeric(6,4) NOT NULL DEFAULT 0,
calculado_em timestamp with time zone DEFAULT now()
);
alter table public."paginas_metricas" enable row level security;

create table if not exists public."saidas" (
id uuid NOT NULL DEFAULT gen_random_uuid(),
entrada_id uuid,
chat_jid text NOT NULL,
texto text NOT NULL,
status text NOT NULL DEFAULT 'pendente'::text,
idempotency_key text,
provider_message_id text,
tentativas integer NOT NULL DEFAULT 0,
ultimo_erro text,
criado_em timestamp with time zone NOT NULL DEFAULT now(),
enviado_em timestamp with time zone
);
alter table public."saidas" enable row level security;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.grupos_snapshots'::regclass and conname='grupos_snapshots_pkey') then
alter table public."grupos_snapshots" add constraint "grupos_snapshots_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.grupos_snapshots'::regclass and conname='grupos_snapshots_grupo_jid_data_key') then
alter table public."grupos_snapshots" add constraint "grupos_snapshots_grupo_jid_data_key" UNIQUE (grupo_jid, data);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.paginas_eventos'::regclass and conname='paginas_eventos_tipo_check') then
alter table public."paginas_eventos" add constraint "paginas_eventos_tipo_check" CHECK ((tipo = ANY (ARRAY['visita'::text, 'clique_cta'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.paginas_eventos'::regclass and conname='paginas_eventos_pkey') then
alter table public."paginas_eventos" add constraint "paginas_eventos_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.paginas_metricas'::regclass and conname='paginas_metricas_pkey') then
alter table public."paginas_metricas" add constraint "paginas_metricas_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.paginas_metricas'::regclass and conname='paginas_metricas_pagina_janela_inicio_key') then
alter table public."paginas_metricas" add constraint "paginas_metricas_pagina_janela_inicio_key" UNIQUE (pagina, janela_inicio);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.experimentos'::regclass and conname='experimentos_status_check') then
alter table public."experimentos" add constraint "experimentos_status_check" CHECK ((status = ANY (ARRAY['proposto'::text, 'aprovado'::text, 'em_teste'::text, 'concluido'::text, 'rejeitado'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.experimentos'::regclass and conname='experimentos_pkey') then
alter table public."experimentos" add constraint "experimentos_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.concorrentes'::regclass and conname='concorrentes_pkey') then
alter table public."concorrentes" add constraint "concorrentes_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.conteudos_concorrentes'::regclass and conname='conteudos_concorrentes_pkey') then
alter table public."conteudos_concorrentes" add constraint "conteudos_concorrentes_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.acoes_pendentes'::regclass and conname='acoes_pendentes_status_check') then
alter table public."acoes_pendentes" add constraint "acoes_pendentes_status_check" CHECK ((status = ANY (ARRAY['aguardando'::text, 'confirmada'::text, 'cancelada'::text, 'executada'::text, 'falhou'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.acoes_pendentes'::regclass and conname='acoes_pendentes_pkey') then
alter table public."acoes_pendentes" add constraint "acoes_pendentes_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.contas_conectadas'::regclass and conname='contas_conectadas_escopo_check') then
alter table public."contas_conectadas" add constraint "contas_conectadas_escopo_check" CHECK ((escopo = ANY (ARRAY['leitura'::text, 'escrita'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.contas_conectadas'::regclass and conname='contas_conectadas_pkey') then
alter table public."contas_conectadas" add constraint "contas_conectadas_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.contas_conectadas'::regclass and conname='contas_conectadas_provedor_conta_id_key') then
alter table public."contas_conectadas" add constraint "contas_conectadas_provedor_conta_id_key" UNIQUE (provedor, conta_id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.auditoria'::regclass and conname='auditoria_pkey') then
alter table public."auditoria" add constraint "auditoria_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.falas'::regclass and conname='falas_status_check') then
alter table public."falas" add constraint "falas_status_check" CHECK ((status = ANY (ARRAY['aguardando'::text, 'respondida'::text, 'ignorada'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.falas'::regclass and conname='falas_pkey') then
alter table public."falas" add constraint "falas_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.mensagens'::regclass and conname='mensagens_id_presente') then
alter table public."mensagens" add constraint "mensagens_id_presente" CHECK ((provider_message_id IS NOT NULL));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.mensagens'::regclass and conname='mensagens_pkey') then
alter table public."mensagens" add constraint "mensagens_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.conversas'::regclass and conname='conversas_pkey') then
alter table public."conversas" add constraint "conversas_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.conversas'::regclass and conname='conversas_canal_instancia_chat_jid_key') then
alter table public."conversas" add constraint "conversas_canal_instancia_chat_jid_key" UNIQUE (canal, instancia, chat_jid);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.entradas'::regclass and conname='entradas_status_check') then
alter table public."entradas" add constraint "entradas_status_check" CHECK ((status = ANY (ARRAY['aguardando'::text, 'processando'::text, 'concluida'::text, 'falhou'::text, 'cancelada'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.entradas'::regclass and conname='entradas_pkey') then
alter table public."entradas" add constraint "entradas_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.saidas'::regclass and conname='saidas_status_check') then
alter table public."saidas" add constraint "saidas_status_check" CHECK ((status = ANY (ARRAY['pendente'::text, 'enviando'::text, 'enviada'::text, 'falhou'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.saidas'::regclass and conname='saidas_pkey') then
alter table public."saidas" add constraint "saidas_pkey" PRIMARY KEY (id);
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.conteudos_concorrentes'::regclass and conname='conteudos_concorrentes_concorrente_id_fkey') then
alter table public."conteudos_concorrentes" add constraint "conteudos_concorrentes_concorrente_id_fkey" FOREIGN KEY (concorrente_id) REFERENCES concorrentes(id) ON DELETE CASCADE;
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.entradas'::regclass and conname='entradas_mensagem_id_fkey') then
alter table public."entradas" add constraint "entradas_mensagem_id_fkey" FOREIGN KEY (mensagem_id) REFERENCES mensagens(id) ON DELETE CASCADE;
end if; end $$;

do $$ begin
if not exists(select 1 from pg_constraint where conrelid='public.saidas'::regclass and conname='saidas_entrada_id_fkey') then
alter table public."saidas" add constraint "saidas_entrada_id_fkey" FOREIGN KEY (entrada_id) REFERENCES entradas(id) ON DELETE SET NULL;
end if; end $$;
CREATE UNIQUE INDEX IF NOT EXISTS falas_pkey ON public.falas USING btree (id);
CREATE INDEX IF NOT EXISTS idx_falas_status ON public.falas USING btree (status, criado_em);
CREATE UNIQUE INDEX IF NOT EXISTS contas_conectadas_pkey ON public.contas_conectadas USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS contas_conectadas_provedor_conta_id_key ON public.contas_conectadas USING btree (provedor, conta_id);
CREATE UNIQUE INDEX IF NOT EXISTS grupos_snapshots_pkey ON public.grupos_snapshots USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS grupos_snapshots_grupo_jid_data_key ON public.grupos_snapshots USING btree (grupo_jid, data);
CREATE INDEX IF NOT EXISTS idx_grupos_snapshots_jid_data ON public.grupos_snapshots USING btree (grupo_jid, data DESC);
CREATE UNIQUE INDEX IF NOT EXISTS paginas_eventos_pkey ON public.paginas_eventos USING btree (id);
CREATE INDEX IF NOT EXISTS idx_paginas_eventos_pagina_tipo_data ON public.paginas_eventos USING btree (pagina, tipo, criado_em DESC);
CREATE UNIQUE INDEX IF NOT EXISTS paginas_metricas_pkey ON public.paginas_metricas USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS paginas_metricas_pagina_janela_inicio_key ON public.paginas_metricas USING btree (pagina, janela_inicio);
CREATE INDEX IF NOT EXISTS idx_paginas_metricas_pagina_janela ON public.paginas_metricas USING btree (pagina, janela_inicio DESC);
CREATE UNIQUE INDEX IF NOT EXISTS experimentos_pkey ON public.experimentos USING btree (id);
CREATE INDEX IF NOT EXISTS idx_experimentos_pagina_status ON public.experimentos USING btree (pagina, status);
CREATE UNIQUE INDEX IF NOT EXISTS concorrentes_pkey ON public.concorrentes USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS conteudos_concorrentes_pkey ON public.conteudos_concorrentes USING btree (id);
CREATE INDEX IF NOT EXISTS idx_conteudos_conc_semana_score ON public.conteudos_concorrentes USING btree (semana DESC, score DESC NULLS LAST);
CREATE UNIQUE INDEX IF NOT EXISTS acoes_pendentes_pkey ON public.acoes_pendentes USING btree (id);
CREATE INDEX IF NOT EXISTS idx_acoes_pendentes_status ON public.acoes_pendentes USING btree (status, criado_em);
CREATE INDEX IF NOT EXISTS idx_acoes_pendentes_acao_status ON public.acoes_pendentes USING btree (acao, status);
CREATE UNIQUE INDEX IF NOT EXISTS auditoria_pkey ON public.auditoria USING btree (id);
CREATE INDEX IF NOT EXISTS idx_auditoria_criado ON public.auditoria USING btree (criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_auditoria_correlation ON public.auditoria USING btree (correlation_id);
CREATE UNIQUE INDEX IF NOT EXISTS mensagens_pkey ON public.mensagens USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mensagens_instancia_provedor ON public.mensagens USING btree (instancia, provider_message_id) WHERE (provider_message_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_mensagens_de_criado ON public.mensagens USING btree (de, criado_em DESC);
CREATE UNIQUE INDEX IF NOT EXISTS conversas_pkey ON public.conversas USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS conversas_canal_instancia_chat_jid_key ON public.conversas USING btree (canal, instancia, chat_jid);
CREATE INDEX IF NOT EXISTS idx_conversas_participante ON public.conversas USING btree (participante_jid);
CREATE UNIQUE INDEX IF NOT EXISTS entradas_pkey ON public.entradas USING btree (id);
CREATE INDEX IF NOT EXISTS idx_entradas_disponivel ON public.entradas USING btree (status, disponivel_em);
CREATE UNIQUE INDEX IF NOT EXISTS saidas_pkey ON public.saidas USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_saidas_idempotency ON public.saidas USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_saidas_status ON public.saidas USING btree (status, criado_em);

do $$ begin
if not exists(select 1 from pg_policies where schemaname='public' and tablename='paginas_eventos' and policyname='eventos insere anon') then
create policy "eventos insere anon" on public."paginas_eventos" for INSERT to anon  with check ((tipo = ANY (ARRAY['visita'::text, 'clique_cta'::text])));
end if; end $$;

do $$ begin
if not exists(select 1 from pg_policies where schemaname='public' and tablename='paginas_metricas' and policyname='metricas leitura publica') then
create policy "metricas leitura publica" on public."paginas_metricas" for SELECT to public using (true) ;
end if; end $$;

grant all on public."acoes_pendentes",public."auditoria",public."concorrentes",public."contas_conectadas",public."conteudos_concorrentes",public."conversas",public."entradas",public."experimentos",public."falas",public."grupos_snapshots",public."mensagens",public."paginas_eventos",public."paginas_metricas",public."saidas" to service_role;
grant usage,select on sequence public."acoes_pendentes_id_seq",public."auditoria_id_seq",public."concorrentes_id_seq",public."contas_conectadas_id_seq",public."conteudos_concorrentes_id_seq",public."experimentos_id_seq",public."falas_id_seq",public."grupos_snapshots_id_seq",public."paginas_eventos_id_seq",public."paginas_metricas_id_seq" to service_role;
grant insert on public.paginas_eventos to anon;
grant select on public.paginas_metricas to anon,authenticated;
