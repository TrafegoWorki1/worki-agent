-- -------------------------------------------------------------
-- Falas: conversa livre do Herickson com o agente
-- -------------------------------------------------------------
-- Diferente de acoes_pendentes: aqui nao ha aprovacao, e o agente
-- responde com texto livre. acoes_pendentes e para acoes com
-- confirmacao (deploy, merge, campanha).
--
-- O insert vem da Edge Function via service_role (bypassa RLS).
-- O select e' do relay, que roda na maquina do Herickson.
-- -------------------------------------------------------------
create table if not exists falas (
  id          bigserial primary key,
  de          text not null,              -- remetente, sem o @s.whatsapp.net
  texto       text not null,
  status      text default 'aguardando'
              check (status in ('aguardando','respondida','ignorada')),
  resposta    text,
  criado_em   timestamptz default now(),
  respondido_em timestamptz
);

create index if not exists idx_falas_status on falas (status, criado_em);

-- ---------------------------------------------------------------------------
-- Falas: politicas
-- ---------------------------------------------------------------------------
-- INSERT: so a Edge Function (service_role) escreve. Nao abrir para anon/authenticated.
--
-- SELECT/DELETE: o relay roda na maquina do Herickson com service_role,
-- que ja bypassa RLS. Nao criar policy aqui de proposito: menos uma
-- superficie aberta, e o service_role nao depende delas.
--
-- Deliberadamente NAO ha policy de SELECT para anon. Se alguem criar anon
-- no futuro, este arquivo deve continuar sem policy de leitura.

-- ---------------------------------------------------------------------------
-- Zernio / contas: manter fechado ate o Herickson pedir leitura
-- ---------------------------------------------------------------------------
alter table contas_conectadas enable row level security;
-- sem policy: service_role only.

-- ---------------------------------------------------------------------------
-- Acoes pendentes: o insert vem da Edge Function, a leitura do relay
-- ---------------------------------------------------------------------------
alter table acoes_pendentes enable row level security;