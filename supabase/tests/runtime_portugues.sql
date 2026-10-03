-- Integracao real com rollback. Executar sem worker novo ativo.
-- Nao envia WhatsApp nem chama Hermes; dados de teste nao permanecem.
begin;
set local role service_role;
do $$
declare r record; dup record; e entradas%rowtype; t uuid; s uuid;
  a bigint; b bigint; mem uuid; nova uuid; negado boolean; n int;
  c uuid; chat text := '__worki_teste_'||gen_random_uuid()||'@g.us';
begin
  assert not exists(select 1 from entradas where status in ('aguardando','processando','falhou')),
    'nao testar reserva global com fila de producao ativa';
  select * into r from worki_registrar_evento('agent-domintante',chat,
    '558592494552@s.whatsapp.net','teste_'||gen_random_uuid(),'primeiro pedido');
  c := r.conversa_id;
  select * into dup from worki_registrar_evento('agent-domintante',chat,
    '558592494552@s.whatsapp.net',(select provider_message_id from mensagens where id=r.mensagem_id),'primeiro pedido');
  assert dup.duplicado and dup.entrada_id=r.entrada_id, 'dedupe';
  assert (select count(*) from entradas where conversa_id=c)=1, 'uma entrada por evento';
  negado:=false;
  begin
    perform worki_registrar_evento('agent-domintante',chat,null,'outro','invalido');
  exception when raise_exception then negado:=true; end;
  assert negado,'sender null deve ser rejeitado';
  negado:=false;
  begin
    perform worki_registrar_evento('agent-domintante',chat,'558592494552@s.whatsapp.net',
      (select provider_message_id from mensagens where id=r.mensagem_id),'conteudo alterado');
  exception when raise_exception then negado:=true; end;
  assert negado,'provider id nao pode mudar de conteudo';
  perform worki_registrar_evento('agent-domintante',chat,'558592494552@s.whatsapp.net',
    'teste_'||gen_random_uuid(),'segundo pedido');
  select * into e from worki_reservar_entrada('worker-a');
  assert e.id=r.entrada_id,'FIFO primeira mensagem';
  assert not exists(select 1 from worki_reservar_entrada('worker-b')),'conversa ocupada';
  assert not worki_renovar_lease(e.id,'worker-b'),'owner incorreto';
  assert worki_renovar_lease(e.id,'worker-a'),'owner correto';
  t:=worki_iniciar_entrada(e.id,'worker-a');
  assert worki_checkpoint_tarefa(t,'worker-a','etapa 1','etapa 2','{"arquivo":"teste.txt"}'), 'checkpoint';
  assert not worki_checkpoint_tarefa(t,'worker-b','indevido','indevido','{}'), 'checkpoint de outro owner';
  negado:=false;
  begin perform worki_iniciar_entrada(e.id,'worker-a');
  exception when raise_exception then negado:=true; end;
  assert negado,'iniciar uma vez';
  s:=worki_concluir_entrada(e.id,'worker-a','20261002_230000_abcdef','resultado');
  assert (select session_id from conversas where id=c)='20261002_230000_abcdef','sessao salva';
  assert (select status from tarefas where id=t)='concluida','tarefa concluida';
  assert worki_concluir_entrada(e.id,'worker-a','20261002_230000_abcdef','resultado')=s,'commit idempotente';
  assert (select count(*) from saidas where entrada_id=e.id)=1,'outbox unica';
  select * into e from worki_reservar_entrada('worker-b');
  assert e.id<>r.entrada_id,'segunda mensagem liberada';
  select count(*) into n from worki_reservar_saida('sender-a');
  assert n=1,'reserva de entrega';
  assert not exists(select 1 from worki_reservar_saida('sender-b')),'entrega concorrente bloqueada';
  assert not worki_registrar_envio(s,'sender-b','enviada'),'ack de outro owner';
  assert worki_registrar_envio(s,'sender-a','incerto'),'timeout incerto';
  assert not exists(select 1 from worki_reservar_saida('sender-a')),'incerto nao reenvia';
  -- Simular revisao que comprova falha, sem acao externa.
  update saidas set status='falhou',disponivel_em=now() where id=s;
  assert exists(select 1 from worki_reservar_saida('sender-a')),'falha clara permite retry';
  assert worki_registrar_envio(s,'sender-a','enviada','TESTE_PROVIDER'),'ack aceito';
  assert not exists(select 1 from worki_reservar_saida('sender-a')),'enviada nao repete';
  update entradas set lease_expires_at=now()-interval '1 second' where id=e.id;
  perform worki_recuperar_leases();
  assert (select status from entradas where id=e.id)='aguardando','reserva sem execucao recupera';
  select * into e from worki_reservar_entrada('worker-b');
  t:=worki_iniciar_entrada(e.id,'worker-b');
  update entradas set lease_expires_at=now()-interval '1 second' where id=e.id;
  perform worki_recuperar_leases();
  assert (select status from entradas where id=e.id)='falhou','execucao interrompida nao repete';
  assert (select status from tarefas where id=t)='bloqueada','tarefa exige revisao';
  assert not exists(select 1 from worki_reservar_entrada('worker-c')),'conversa bloqueada por falha';
  a:=worki_pedir_aprovacao(c,'merge','PR-TESTE',repeat('a',64),'Teste','{}');
  assert worki_pedir_aprovacao(c,'merge','PR-TESTE',repeat('a',64),'Teste','{}')=a,'pedido idempotente';
  b:=worki_pedir_aprovacao(c,'merge','PR-OUTRO',repeat('b',64),'Teste','{}');
  negado:=false;
  begin perform worki_aprovar_acao(c,'aprova','558592494552@s.whatsapp.net');
  exception when raise_exception then negado:=true; end;
  assert negado,'aprovacao ambigua';
  negado:=false;
  begin perform worki_aprovar_acao(c,null,'558592494552@s.whatsapp.net',a);
  exception when raise_exception then negado:=true; end;
  assert negado,'palavra null nao autoriza';
  assert worki_aprovar_acao(c,'aprova','558592494552@s.whatsapp.net',a)=a,'aprova alvo explicito';
  assert not worki_consumir_aprovacao(a,c,'merge','PR-TESTE',repeat('c',64)),'hash alterado';
  assert worki_consumir_aprovacao(a,c,'merge','PR-TESTE',repeat('a',64)),'consumo aprovado';
  assert not worki_consumir_aprovacao(a,c,'merge','PR-TESTE',repeat('a',64)),'consumo uma vez';
  update acoes_pendentes set expira_em=now()-interval '1 second' where id=b;
  negado:=false;
  begin perform worki_aprovar_acao(c,'aprova','558592494552@s.whatsapp.net',b);
  exception when raise_exception then negado:=true; end;
  assert negado,'aprovacao expirada';
  mem:=worki_salvar_memoria('558592494552','__teste__','preferencia','antiga','preferencia','teste',true);
  nova:=worki_salvar_memoria('558592494552','__teste__','preferencia','corrigida','preferencia','teste',true);
  assert (select status from memorias where id=mem)='substituida','memoria antiga substituida';
  assert (select substitui_id from memorias where id=nova)=mem,'fonte da correcao';
  assert (select count(*) from memorias where proprietario_id='558592494552' and projeto_id='__teste__'
    and chave='preferencia' and status='ativa')=1,'uma memoria ativa';
  assert not has_function_privilege('anon','worki_reservar_entrada(text,integer)','EXECUTE'),'RPC fechada anon';
  assert not has_function_privilege('authenticated','worki_registrar_evento(text,text,text,text,text,text)','EXECUTE'),
    'RPC fechada authenticated';
  assert has_function_privilege('service_role','worki_reservar_entrada(text,integer)','EXECUTE'),'RPC liberada backend';
end $$;
rollback;
