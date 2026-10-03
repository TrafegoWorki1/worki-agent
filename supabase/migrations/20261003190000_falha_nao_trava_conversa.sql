-- Uma entrada que FALHOU nao pode travar a conversa para sempre.
--
-- Antes: worki_reservar_entrada tratava 'falhou' como estado nao-terminal.
-- Qualquer entrada 'falhou' na conversa bloqueava novas mensagens (o worker
-- nunca mais reservava nada dela). Como nao existe caminho que tire uma
-- entrada de 'falhou', a conversa ficava muda para sempre apos um unico erro
-- do Hermes -- exatamente a "falha silenciosa" que o AGENTS.md s.2 descreve.
--
-- Agora: 'falhou' e terminal, igual a 'concluida' e 'cancelada'. A mensagem
-- seguinte da mesma conversa volta a ser processada. O usuario recebe uma
-- resposta curta de erro (gravada pelo worker como saida) e pode reenviar.
-- A sessao do Hermes e preservada (o worker nao grava mais 'erro' nela), entao
-- o --resume da proxima mensagem continua o contexto.
--
-- Idempotente: create or replace preserva os grants existentes da funcao.

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
          and anterior.status not in ('concluida','cancelada','falhou')))
    and not exists(select 1 from entradas x where x.conversa_id=cv.id and x.status in ('processando'))
    order by (select min(ordem) from entradas where conversa_id=cv.id and status='aguardando')
    for update of cv skip locked limit 1;
  if c is null then return; end if;
  select id into e from entradas where conversa_id=c and status='aguardando' order by ordem limit 1;
  return query update entradas set status='processando',tentativas=tentativas+1,
    lease_owner=p_owner,lease_expires_at=now()+make_interval(secs=>p_lease_s),atualizado_em=now()
    where id=e returning *;
end $$;

-- Defensivo: garante o grant mesmo que o replace nao o preserve em algum ambiente.
revoke all on function public.worki_reservar_entrada(text,int) from public,anon,authenticated;
grant execute on function public.worki_reservar_entrada(text,int) to service_role;
