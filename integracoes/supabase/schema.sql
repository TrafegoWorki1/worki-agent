-- =============================================================
-- worki-agent — schema base
-- Agente Dominante: snapshots de grupo, metricas de pagina,
-- experimentos, concorrentes e acoes pendentes de aprovacao.
-- =============================================================
-- Aplicar:  supabase db push
-- Ou:       psql "$SUPABASE_DB_URL" -f integracoes/supabase/schema.sql
-- =============================================================

-- -------------------------------------------------------------
-- Grupo de lancamento
-- -------------------------------------------------------------
create table if not exists grupos_snapshots (
  id            bigserial primary key,
  grupo_jid     text not null,
  data          date not null,
  total_membros int  not null,
  entradas      int  default 0,
  saidas        int  default 0,
  created_at    timestamptz default now(),
  unique (grupo_jid, data)
);

comment on table grupos_snapshots is
  'Um registro por grupo por dia. A variação vem da diferenca entre linhas consecutivas.';

create index if not exists idx_grupos_snapshots_jid_data
  on grupos_snapshots (grupo_jid, data desc);

-- -------------------------------------------------------------
-- Eventos de pagina (visita, clique no CTA)
-- Gravados pelo frontend. Usa a ANON key, nunca a service role.
-- -------------------------------------------------------------
create table if not exists paginas_eventos (
  id         bigserial primary key,
  pagina     text not null,
  tipo       text not null check (tipo in ('visita', 'clique_cta')),
  origem     text,
  utm_source text,
  utm_campaign text,
  criado_em  timestamptz default now()
);

create index if not exists idx_paginas_eventos_pagina_tipo_data
  on paginas_eventos (pagina, tipo, criado_em desc);

-- -------------------------------------------------------------
-- Metricas agregadas. Uma linha por pagina por janela.
-- -------------------------------------------------------------
create table if not exists paginas_metricas (
  id             bigserial primary key,
  pagina         text not null,
  janela_inicio  timestamptz not null,
  janela_fim     timestamptz not null,
  visitas        int not null default 0,
  cliques        int not null default 0,
  taxa_conversao numeric(6,4) not null default 0,
  calculado_em   timestamptz default now(),
  unique (pagina, janela_inicio)
);

create index if not exists idx_paginas_metricas_pagina_janela
  on paginas_metricas (pagina, janela_inicio desc);

-- -------------------------------------------------------------
-- Experimentos A/B da pagina. Fecha o ciclo do antes/depois.
-- A Fase 4 grava 'taxa_antes' no PR; o job semanal preenche 'taxa_depois'.
-- -------------------------------------------------------------
create table if not exists experimentos (
  id           bigserial primary key,
  pagina       text not null,
  hipotese     text not null,
  pr_url       text,
  pr_numero    int,
  branch       text,
  taxa_antes   numeric(6,4),
  taxa_depois  numeric(6,4),
  status       text default 'proposto'
               check (status in ('proposto','aprovado','em_teste','concluido','rejeitado')),
  criado_em    timestamptz default now(),
  concluido_em timestamptz
);

create index if not exists idx_experimentos_pagina_status
  on experimentos (pagina, status);

-- -------------------------------------------------------------
-- Concorrentes (lista de vigia)
-- -------------------------------------------------------------
create table if not exists concorrentes (
  id            bigserial primary key,
  nome          text not null,
  perfil_url    text,
  palavras_chave text[],
  ativo         boolean default true,
  criado_em     timestamptz default now()
);

-- -------------------------------------------------------------
-- Conteudos dos concorrentes, coletados pelo cron semanal
-- -------------------------------------------------------------
create table if not exists conteudos_concorrentes (
  id             bigserial primary key,
  concorrente_id bigint references concorrentes(id) on delete cascade,
  semana         date not null,
  tipo           text,
  url            text,
  resumo         text,
  score          numeric,
  coletado_em    timestamptz default now()
);

create index if not exists idx_conteudos_conc_semana_score
  on conteudos_concorrentes (semana desc, score desc nulls last);

-- -------------------------------------------------------------
-- Acoes pendentes de aprovacao (deploy, anuncio, merge)
-- -------------------------------------------------------------
create table if not exists acoes_pendentes (
  id          bigserial primary key,
  descricao   text not null,
  payload     jsonb not null,
  canal       text default 'whatsapp',
  status      text default 'aguardando'
              check (status in ('aguardando','confirmada','cancelada','executada','falhou')),
  resultado   text,
  criado_em   timestamptz default now(),
  decidido_em timestamptz
);

create index if not exists idx_acoes_pendentes_status
  on acoes_pendentes (status, criado_em);

-- -------------------------------------------------------------
-- Contas conectadas (Zernio / Meta / Instagram)
-- -------------------------------------------------------------
create table if not exists contas_conectadas (
  id         bigserial primary key,
  provedor   text not null,
  conta_id   text not null,
  nome       text,
  escopo     text default 'leitura' check (escopo in ('leitura','escrita')),
  ativo      boolean default true,
  criado_em  timestamptz default now(),
  unique (provedor, conta_id)
);

-- -------------------------------------------------------------
-- Log de auditoria (AGENTS.md secao 7)
-- Falha tambem loga.
-- -------------------------------------------------------------
create table if not exists auditoria (
  id                  bigserial primary key,
  canal               text,
  remetente           text,
  comando             text,
  skill               text,
  ferramenta          text,
  resultado           text,
  aguardou_aprovacao  boolean default false,
  duracao_ms          int,
  criado_em           timestamptz default now()
);

create index if not exists idx_auditoria_criado
  on auditoria (criado_em desc);

-- =============================================================
-- RLS
-- O frontend so escreve em paginas_eventos com a ANON key.
-- Todo o resto e leitura do agente via service role (server-side).
-- =============================================================

alter table grupos_snapshots     enable row level security;
alter table paginas_eventos      enable row level security;
alter table paginas_metricas     enable row level security;
alter table experimentos        enable row level security;
alter table concorrentes         enable row level security;
alter table conteudos_concorrentes enable row level security;
alter table acoes_pendentes     enable row level security;
alter table contas_conectadas   enable row level security;
alter table auditoria           enable row level security;

-- Leitura publica das metricas agregadas (a pagina pode mostrar taxa).
create policy "metricas leitura publica" on paginas_metricas
  for select using (true);

-- Frontend insere evento de visita/clique.
create policy "eventos insere anon" on paginas_eventos
  for insert to anon with check (tipo in ('visita','clique_cta'));

-- Nao existe policy de update nem delete no anon: o evento e imutavel.
-- Sem policy, RLS nega. Isso e o que queremos.