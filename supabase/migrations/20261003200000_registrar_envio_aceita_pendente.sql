-- worki_registrar_envio precisa registrar o envio direto de uma saida 'pendente'.
--
-- Sintoma medido em producao em 2026-10-03: das 38 linhas de `saidas`,
-- NENHUMA estava 'enviada' e nenhuma tinha `enviado_em`: 19 'pendente',
-- 18 'falhou' e 1 'incerto', todas com tentativas = 0 (exceto o incerto).
--
-- Causa: o worker cria a saida ('pendente'), envia pela Evolution na mesma
-- execucao e chama worki_registrar_envio para marcar o resultado. Mas a
-- funcao so atualizava linhas 'enviando' com lease_owner = p_owner, e uma
-- saida recem-criada esta 'pendente' e sem dono. O UPDATE casava zero linhas,
-- a funcao devolvia false e o Python ignorava. A mensagem chegava ao usuario,
-- mas o banco nunca soube.
--
-- Por que isso e perigoso alem de sujar o registro: a saida segue 'pendente'
-- para sempre e, no dia em que a reserva de saidas deixar de estar bloqueada
-- (por exemplo ao aplicar 20261003160000), o laço de entrega reenvia todas.
--
-- Correcao: alem do caminho da reserva ('enviando' + dono + lease valido, que
-- continua exigido), aceita-se registrar o resultado de uma saida 'pendente'.
-- No caminho direto, o `tentativas` sobe aqui (a reserva e quem o incrementava),
-- para o limite de 5 tentativas da reserva continuar valendo.
--
-- Nao reabre saidas ja encerradas: 'enviada' e 'falhou'/'incerto' que nao
-- estejam sob lease continuam intocadas por esta funcao.

create or replace function public.worki_registrar_envio(p_saida_id uuid,p_owner text,
  p_estado text,p_provider_message_id text default null)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
begin
  if p_estado not in ('enviada','falhou','incerto') then raise exception 'estado invalido'; end if;
  update saidas set
    tentativas=case when status='pendente' then tentativas+1 else tentativas end,
    status=p_estado,
    provider_message_id=coalesce(p_provider_message_id,provider_message_id),
    enviado_em=case when p_estado='enviada' then now() else enviado_em end,
    disponivel_em=now()+make_interval(secs=>least(300,5*power(2,least(tentativas,6))::int)),
    ultimo_erro=case when p_estado='incerto' then 'Reconciliacao necessaria'
      when p_estado='falhou' then 'Envio recusado' end,
    lease_owner=null,lease_expires_at=null
    where id=p_saida_id and (
      status='pendente'
      or (status='enviando' and lease_owner=p_owner and lease_expires_at>clock_timestamp()));
  return found;
end $$;

revoke all on function public.worki_registrar_envio(uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.worki_registrar_envio(uuid,text,text,text) to service_role;
