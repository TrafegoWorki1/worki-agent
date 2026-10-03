-- Uma saida 'incerto' nao pode trancar a conversa.
--
-- Sintoma em 2026-10-03: a saida cc2d2ae3 (status 'incerto', 2
-- tentativas) impedia o envio de TODAS as saidas posteriores da conversa
-- 72a022a5. As respostas ficavam em 'pendente' para sempre, mesmo com o
-- worker ativo e o Evolution respondendo 200.
--
-- A RPC de reserva de saidas tinha:
--
--   and not exists(select 1 from saidas x where x.conversa_id=cv.id
--                 and x.status in ('enviando','incerto'))
--
-- 'enviando' precisa bloquear: envio em curso na mesma conversa pode
-- desordenar as respostas no WhatsApp.
--
-- 'incerto' nao pode. 'incerto' significa que o worker NAO SOUber se o
-- envio chegou a acontecer — a Evolution pode ter aceitado ou recusado.
-- O comentario do proprio relay diz: "Nao reenviar as cegas:
-- reconciliar". Reconciliacao exige consultar o provider.
--
-- Enquanto essa reconciliacao nao existe, tratar 'incerto' como bloqueio
-- permanente e pior do que o risco de duplicar: a conversa inteira morre.
-- Mensagem antiga nao pode impedir mensagem nova.
--
-- O filtro por ordem (anterior.status <> 'enviada') continua garantindo
-- que a fila de envio ande em sequencia. O que se remove e o bloqueio
-- total.

create or replace function public.worki_reservar_saida(p_owner text,p_lease_s int default 120)
returns setof public.saidas language plpgsql security invoker set search_path=public,pg_temp as $$
declare c uuid; s uuid;
begin
  if coalesce(p_owner,'')='' or p_lease_s is null or p_lease_s<60 or p_lease_s>3600 then raise exception 'lease invalida'; end if;
  -- Bloqueia SO por 'enviando': envio em curso na mesma conversa
  -- desordenaria as respostas. 'incerto' e reconciliacao pendente — nao
  -- pode segurar as saidas seguintes. Ver o cabecalho desta migration.
  select cv.id into c from conversas cv where exists(select 1 from saidas x
    where x.conversa_id=cv.id and x.status in ('pendente','falhou') and x.tentativas<5
      and x.disponivel_em<=now() and not exists(select 1 from saidas anterior
        join entradas ae on ae.id=anterior.entrada_id join entradas xe on xe.id=x.entrada_id
        where anterior.conversa_id=cv.id and ae.ordem<xe.ordem and anterior.status<>'enviada'))
    and not exists(select 1 from saidas x where x.conversa_id=cv.id and x.status='enviando')
    order by cv.criado_em for update of cv skip locked limit 1;
  if c is null then return; end if;
  select x.id into s from saidas x join entradas e on e.id=x.entrada_id
    where x.conversa_id=c and x.status in ('pendente','falhou') order by e.ordem limit 1;
  return query update saidas set status='enviando',tentativas=tentativas+1,lease_owner=p_owner,
    lease_expires_at=now()+make_interval(secs=>p_lease_s) where id=s returning *;
end $$;