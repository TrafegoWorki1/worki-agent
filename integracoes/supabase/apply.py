#!/usr/bin/env python
"""Aplica o schema do worki-agent no Supabase via a API de SQL.

Usa SUPABASE_SERVICE_ROLE_KEY. Nao escreve nada no repo.
Rodar:  python supabase_apply.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from env import chave, mascarar  # noqa: E402

SB = chave("SUPABASE_URL").rstrip("/")
KEY = chave("SUPABASE_SERVICE_ROLE_KEY")
DDL_URL = f"{SB}/pg/query"

print(f"projeto: {SB}")
print(f"service_role: {mascarar(KEY)}")

DDL = """
create table if not exists public.grupos_snapshots (
  id            bigserial primary key,
  grupo_jid     text not null,
  data          date not null,
  total_membros int  not null,
  entradas      int  default 0,
  saidas        int  default 0,
  created_at    timestamptz default now(),
  unique (grupo_jid, data)
);
create index if not exists idx_grupos_snapshots_jid_data
  on public.grupos_snapshots (grupo_jid, data desc);

create table if not exists public.paginas_eventos (
  id           bigserial primary key,
  pagina       text not null,
  tipo         text not null check (tipo in ('visita','clique_cta')),
  origem       text,
  utm_source   text,
  utm_campaign text,
  criado_em    timestamptz default now()
);
create index if not exists idx_paginas_eventos_pagina_tipo_data
  on public.paginas_eventos (pagina, tipo, criado_em desc);

create table if not exists public.paginas_metricas (
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
  on public.paginas_metricas (pagina, janela_inicio desc);

create table if not exists public.experimentos (
  id            bigserial primary key,
  pagina        text not null,
  hipotese      text not null,
  pr_url        text,
  pr_numero     int,
  branch        text,
  taxa_antes    numeric(6,4),
  taxa_depois   numeric(6,4),
  status        text default 'proposto'
                check (status in ('proposto','aprovado','em_teste','concluido','rejeitado')),
  criado_em     timestamptz default now(),
  concluido_em timestamptz
);
create index if not exists idx_experimentos_pagina_status
  on public.experimentos (pagina, status);

create table if not exists public.concorrentes (
  id             bigserial primary key,
  nome           text not null,
  perfil_url     text,
  palavras_chave text[],
  ativo          boolean default true,
  criado_em      timestamptz default now()
);

create table if not exists public.conteudos_concorrentes (
  id             bigserial primary key,
  concorrente_id bigint references public.concorrentes(id) on delete cascade,
  semana         date not null,
  tipo           text,
  url            text,
  resumo         text,
  score          numeric,
  coletado_em    timestamptz default now()
);
create index if not exists idx_conteudos_conc_semana_score
  on public.conteudos_concorrentes (semana desc, score desc nulls last);

create table if not exists public.acoes_pendentes (
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
  on public.acoes_pendentes (status, criado_em);

create table if not exists public.contas_conectadas (
  id        bigserial primary key,
  provedor  text not null,
  conta_id  text not null,
  nome      text,
  escopo    text default 'leitura' check (escopo in ('leitura','escrita')),
  ativo     boolean default true,
  criado_em timestamptz default now(),
  unique (provedor, conta_id)
);

create table if not exists public.auditoria (
  id                 bigserial primary key,
  canal              text,
  remetente          text,
  comando            text,
  skill              text,
  ferramenta         text,
  resultado          text,
  aguardou_aprovacao boolean default false,
  duracao_ms         int,
  criado_em          timestamptz default now()
);
create index if not exists idx_auditoria_criado on public.auditoria (criado_em desc);
"""

RLS = """
alter table public.grupos_snapshots enable row level security;
alter table public.paginas_eventos enable row level security;
alter table public.paginas_metricas enable row level security;
alter table public.experimentos enable row level security;
alter table public.concorrentes enable row level security;
alter table public.conteudos_concorrentes enable row level security;
alter table public.acoes_pendentes enable row level security;
alter table public.contas_conectadas enable row level security;
alter table public.auditoria enable row level security;

drop policy if exists "metricas leitura publica" on public.paginas_metricas;
create policy "metricas leitura publica" on public.paginas_metricas
  for select using (true);

drop policy if exists "eventos insere anon" on public.paginas_eventos;
create policy "eventos insere anon" on public.paginas_eventos
  for insert to anon with check (tipo in ('visita','clique_cta'));
"""


def run(sql: str, label: str) -> None:
    req = urllib.request.Request(
        DDL_URL,
        data=json.dumps({"query": sql}).encode(),
        headers={
            "apikey": KEY,
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read().decode()[:400]
            print(f"OK   {label}: {body}")
    except urllib.error.HTTPError as e:
        print(f"FALHA {label}: {e.code} {e.read().decode()[:400]}")
        sys.exit(1)


if __name__ == "__main__":
    run(DDL, "tabelas + indices")
    run(RLS, "RLS + policies")
    print("\nSchema aplicado.")