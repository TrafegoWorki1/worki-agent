-- worki_concluir_entrada: so cria outbox em execucao bem-sucedida.
--
-- Sintoma observado em 2026-10-03: a tabela `saidas` continha
--   texto = 'hermes chat -q\n'
-- que e o `log_cmd` do adapter, nao uma resposta do agente. A entrada
-- estava em falha (o worker morreu por SIGTERM), e o relay chama
--
--   self.db.concluir(entrada.id, r.log or "hermes falhou")
--
-- passando o LOG como p_resposta. A RPC inseria em `saidas` sem olhar
-- p_sucesso, entao a outbox tentava enviar "hermes chat -q" para o
-- WhatsApp como se fosse resposta.
--
-- A correcao e o insert ficar dentro do caminho de sucesso. Em falha:
-- a entrada vai para 'falhou', a tarefa fica 'bloqueada', a auditoria
-- registra o erro, e NADA entra na outbox.
--
-- Idempotencia preservada: quem chama com sucesso=false recebe null e
-- trata como falha, que e o que ja acontecia com a excecao 'resposta
-- vazia' antes do texto existir. Entradas ja concluidas seguem
-- devolvendo a saida existente, sem criar duplicata.

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

  update tarefas set status=case when p_sucesso then 'concluida' else 'bloqueada' end,
    resultado=p_resposta,etapa_atual='Resultado salvo',
    proxima_acao=case when p_sucesso then 'Aguardar pedido' else 'Revisar execucao antes de retomar' end,
    atualizado_em=now() where entrada_id=e.id;

  if p_sucesso then
    select chat_jid into chat from conversas where id=e.conversa_id;
    update conversas set session_id=coalesce(p_session_id,session_id),atualizado_em=now() where id=e.conversa_id;
    -- Outbox SO no caminho de sucesso. Falha nao entra na fila de envio.
    insert into saidas(entrada_id,conversa_id,chat_jid,texto,idempotency_key)
      values(e.id,e.conversa_id,chat,p_resposta,'entrada:'||e.id) returning id into s;
  else
    -- Guarda o motivo em auditoria, nunca na outbox.
    insert into auditoria(canal,remetente,comando,ferramenta,resultado,correlation_id)
      values('whatsapp','proprietario',left(p_resposta,500),'Hermes','erro',e.id::text);
  end if;

  update entradas set status=case when p_sucesso then 'concluida' else 'falhou' end,
    lease_owner=null,lease_expires_at=null,atualizado_em=now() where id=e.id;

  if p_sucesso then
    insert into auditoria(canal,remetente,ferramenta,resultado,correlation_id)
      values('whatsapp','proprietario','Hermes','sucesso',e.id::text);
  end if;
  return s;
end $$;