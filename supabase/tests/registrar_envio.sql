-- worki_registrar_envio: envio direto de saida 'pendente' e caminho da reserva.
-- Integracao real com rollback: nao deixa dado. NAO rodar com fila ativa; o
-- teste recusa sozinho (a reserva e global e poderia pegar uma saida real).
\set ON_ERROR_STOP on
begin;
do $$ begin
  assert not exists(select 1 from saidas where status in ('pendente','enviando','falhou'))
    and not exists(select 1 from entradas where status in ('aguardando','processando')),
    'nao testar com fila de producao ativa';
end $$;

insert into conversas(id,canal,instancia,chat_jid,session_id) values('66666666-6666-6666-6666-666666666666','whatsapp','agent-domintante','__teste@s.whatsapp.net','s');
insert into mensagens(id,instancia,provider_message_id,de,texto,conversa_id,chat_jid) values
 ('a6666666-0000-0000-0000-000000000001','agent-domintante','__G1','x','um','66666666-6666-6666-6666-666666666666','__teste@s.whatsapp.net');
insert into entradas(id,mensagem_id,conversa_id,status) values
 ('b6666666-0000-0000-0000-000000000001','a6666666-0000-0000-0000-000000000001','66666666-6666-6666-6666-666666666666','aguardando');

do $$
declare s uuid; ok boolean; r record; n int;
begin
  perform worki_reservar_entrada('w',120);
  perform worki_iniciar_entrada('b6666666-0000-0000-0000-000000000001','w');
  s := worki_concluir_entrada('b6666666-0000-0000-0000-000000000001','w','s','ola',true);
  -- 1) envio direto bem-sucedido (o caminho do worker)
  ok := worki_registrar_envio(s,'w','enviada','PMID-1');
  select * into r from saidas where id=s;
  assert ok, '1: registrar_envio deveria devolver true';
  assert r.status='enviada' and r.enviado_em is not null and r.provider_message_id='PMID-1' and r.tentativas=1,
    '1: estado errado: '||r.status||' tentativas='||r.tentativas;
  -- 2) a reserva nao reentrega o que ja foi enviado
  select count(*) into n from worki_reservar_saida('w',120);
  assert n=0, '2: reserva reentregou saida ja enviada';
  -- 3) nao reabre saida encerrada
  ok := worki_registrar_envio(s,'w','falhou');
  select * into r from saidas where id=s;
  assert not ok and r.status='enviada', '3: reabriu saida enviada';
end $$;

-- 4 e 5) falha e timeout no envio direto
insert into mensagens(id,instancia,provider_message_id,de,texto,conversa_id,chat_jid) values
 ('a6666666-0000-0000-0000-000000000002','agent-domintante','__G2','x','dois','66666666-6666-6666-6666-666666666666','__teste@s.whatsapp.net');
insert into entradas(id,mensagem_id,conversa_id,status) values
 ('b6666666-0000-0000-0000-000000000002','a6666666-0000-0000-0000-000000000002','66666666-6666-6666-6666-666666666666','aguardando');
do $$
declare s uuid; ok boolean; r record;
begin
  perform worki_reservar_entrada('w',120);
  perform worki_iniciar_entrada('b6666666-0000-0000-0000-000000000002','w');
  s := worki_concluir_entrada('b6666666-0000-0000-0000-000000000002','w','s','resposta dois',true);
  ok := worki_registrar_envio(s,'w','falhou');
  select * into r from saidas where id=s;
  assert ok and r.status='falhou' and r.tentativas=1 and r.enviado_em is null and r.ultimo_erro='Envio recusado',
    '4: falha mal registrada: '||r.status;
  update saidas set status='pendente', tentativas=0 where id=s;
  ok := worki_registrar_envio(s,'w','incerto');
  select * into r from saidas where id=s;
  assert ok and r.status='incerto' and r.enviado_em is null, '5: incerto mal registrado';
end $$;

-- 6) caminho da reserva continua exigindo o dono certo
insert into conversas(id,canal,instancia,chat_jid,session_id) values('77777777-7777-7777-7777-777777777777','whatsapp','agent-domintante','__teste2@s.whatsapp.net','s');
insert into mensagens(id,instancia,provider_message_id,de,texto,conversa_id,chat_jid) values
 ('a7777777-0000-0000-0000-000000000001','agent-domintante','__H1','x','x','77777777-7777-7777-7777-777777777777','__teste2@s.whatsapp.net');
insert into entradas(id,mensagem_id,conversa_id,status) values
 ('b7777777-0000-0000-0000-000000000001','a7777777-0000-0000-0000-000000000001','77777777-7777-7777-7777-777777777777','aguardando');
do $$
declare s uuid; ok boolean; st text; n int;
begin
  perform worki_reservar_entrada('w',120);
  perform worki_iniciar_entrada('b7777777-0000-0000-0000-000000000001','w');
  s := worki_concluir_entrada('b7777777-0000-0000-0000-000000000001','w','s','resp',true);
  select count(*) into n from worki_reservar_saida('dono-A',120);
  select status into st from saidas where id=s;
  assert n=1 and st='enviando', '6: a reserva deveria ter pego a saida; status='||st;
  ok := worki_registrar_envio(s,'dono-B','enviada');
  assert not ok, '6: aceitou dono errado em saida enviando';
  ok := worki_registrar_envio(s,'dono-A','enviada');
  select status into st from saidas where id=s;
  assert ok and st='enviada', '6: recusou o dono certo';
end $$;
rollback;
