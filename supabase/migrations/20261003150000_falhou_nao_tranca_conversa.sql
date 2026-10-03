-- Uma entrada 'falhou' nao pode trancar a conversa.
--
-- Sintoma em 2026-10-03: a mensagem "Cadê ?" (entrada de ordem 35) ficou
-- em 'aguardando' para sempre. A ordem 34 tinha falhado, e a RPC de
-- reserva recusava a conversa inteira:
--
--   and not exists(select 1 from entradas x where x.conversa_id=cv.id
--                 and x.status in ('processando','falhou'))
--
-- 'processando' precisa bloquear: significa execucao em curso, e duas
-- execucoes na mesma conversa brigariam pelo contexto da sessao.
--
-- 'falhou' nao. Falhou e TERMINAL: o trabalho acabou, com ou sem sucesso.
-- Bloquear por causa dele significa que uma falha unica impede o usuario
-- de continuar conversando na mesma conversa, para sempre. Aconteceu
-- duas vezes no mesmo dia.
--
-- A ordem ja e garantida pela clausula anterior (a entrada de menor
-- 'ordem' em aberto e a primeira), entao deixar 'falhou' de fora nao abre
-- brecha de reordenamento: a conversa continua sendo percorrida em ordem.
--
-- Uma entrada que esgotou as tentativas continua 'falhou' e continua
-- visivel no painel. O que muda e ela nao segurar as mensagens seguintes.

create or replace function public.worki_reservar_entrada(p_owner text,p_lease_s int default 120)
returns setof public.entradas language plpgsql security invoker set search_path=public,pg_temp as $$
declare c uuid; e uuid;
begin
  if coalesce(p_owner,'')='' or p_lease_s is null or p_lease_s<30 or p_lease_s>3600 then raise exception 'lease invalida'; end if;
  -- Trava a conversa, incluindo workers disputando mensagens diferentes dela.
  -- Bloqueia SO por 'processando': execucao em curso na mesma conversa
  -- competiria pela sessao. Entrada 'falhou' e terminal e nao segura as
  -- mensagens seguintes — ver o cabecalho desta migration.
  select cv.id into c from conversas cv where exists(
    select 1 from entradas x where x.conversa_id=cv.id and x.status='aguardando'
      and x.disponivel_em<=now() and not exists(select 1 from entradas anterior
        where anterior.conversa_id=cv.id and anterior.ordem<x.ordem
          and anterior.status not in ('concluida','cancelada','falhou')))
    and not exists(select 1 from entradas x where x.conversa_id=cv.id and x.status='processando')
    order by (select min(ordem) from entradas where conversa_id=cv.id and status='aguardando')
    for update of cv skip locked limit 1;
  if c is null then return; end if;
  select id into e from entradas where conversa_id=c and status='aguardando' order by ordem limit 1;
  return query update entradas set status='processando',tentativas=tentativas+1,
    lease_owner=p_owner,lease_expires_at=now()+make_interval(secs=>p_lease_s),atualizado_em=now()
    where id=e returning *;
end $$;