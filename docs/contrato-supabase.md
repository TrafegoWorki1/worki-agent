# Contrato de integracao — banco em portugues

Projeto: `wxqwtyotkkshdjzzwjsk` / Agente Dominante / PostgreSQL 17.
Verificado e aplicado em 2026-10-02. Schema canonico: `public`.

O Codex assume banco e EasyPanel. A outra ferramenta assume receptor HTTP,
worker, adaptador Hermes, Dockerfile e testes da aplicacao. Esses arquivos
nao foram alterados nesta entrega do banco. A correcao de ambiente em
`integracoes/supabase/env.py` e os testes dela acompanham o patch.

## Estado do banco

Preservados `falas`, auditoria e demais registros existentes. As novas RPCs
usam `mensagens`, `conversas`, `entradas`, `saidas`, `acoes_pendentes`.
Criadas apenas as estruturas faltantes: `tarefas`, `etapas_tarefa`, `memorias`.
Nenhuma tabela equivalente em ingles foi criada.

**Nao aplicar `integracoes/supabase/migrations/001_fila.sql`.** Os clientes
`queue.py` e `outbox.py` do PR #1 ainda usam o contrato em ingles: precisam
ser adaptados. Os contratos reais completos estao em `contrato-rpcs.json`.
Os IDs de conversa, mensagem, entrada, saida, tarefa e memoria sao UUIDs.
Somente `acoes_pendentes.id` continua bigint. Nao converter UUID para int.

Todas as 13 RPCs executam como `SECURITY INVOKER`, com `search_path` fixo.
`anon` e `authenticated` nao executam essas RPCs e nao acessam as tabelas
de runtime. `service_role` tem os grants necessarios e bypassa RLS.
RLS sem policies nessas tabelas e intencional: acesso somente pelo backend.
As policies existentes de eventos e metricas de paginas foram preservadas.

## Sequencia obrigatoria do worker

1. Receptor autentica webhook, valida instancia, remetente e grupos.
2. `worki_registrar_evento` salva conversa + mensagem + entrada atomicamente.
3. `worki_reservar_entrada` reserva uma entrada. Array vazio significa sem trabalho.
4. Ler `mensagens` por `mensagem_id` e `conversas` por `conversa_id`.
5. `worki_iniciar_entrada` grava inicio e cria tarefa ANTES de chamar Hermes.
6. Renovar lease a cada 20–30 segundos. Se retornar false, interromper o
   subprocesso e exigir revisao; nao continuar produzindo efeitos externos.
7. Executar Hermes com `conversas.session_id`; salvar checkpoints materiais.
8. `worki_concluir_entrada` salva resultado, sessao, saida e conclusao numa
   transacao. Repetir esse commit depois de timeout e seguro para sucesso.
9. Sender separado reserva saida, envia a Evolution e registra o resultado.

Nao usar GET seguido de PATCH para reservar nem concluir fora das RPCs.
Nao enviar resposta antes de `worki_concluir_entrada` confirmar o commit.
Nao usar `--resume latest` nem fallback que escolha sessao de outro processo.

## RPCs de recepcao e processamento

| RPC | Parametros | Retorno |
|---|---|---|
| `worki_registrar_evento` | `p_instancia text`, `p_chat_jid text`, `p_remetente text`, `p_provider_message_id text`, `p_texto text`, `p_tipo text='conversation'` | Array com `entrada_id`, `mensagem_id`, `conversa_id`, `duplicado` |
| `worki_reservar_entrada` | `p_owner text`, `p_lease_s integer=120` | Array de zero ou uma linha de `entradas` |
| `worki_renovar_lease` | `p_entrada_id uuid`, `p_owner text`, `p_lease_s integer=120` | Boolean; false exige interrupcao |
| `worki_iniciar_entrada` | `p_entrada_id uuid`, `p_owner text` | UUID da tarefa |
| `worki_checkpoint_tarefa` | `p_tarefa_id uuid`, `p_owner text`, `p_etapa text`, `p_proxima_acao text`, `p_checkpoint jsonb` | Boolean |
| `worki_concluir_entrada` | `p_entrada_id uuid`, `p_owner text`, `p_session_id text`, `p_resposta text`, `p_sucesso boolean=true` | UUID da saida |
| `worki_recuperar_leases` | Sem parametros | JSON com contagens de tarefas bloqueadas e envios incertos |

`p_remetente` e o JID canonico do participante autorizado, nao o nome de
exibicao. Instancia permitida: `agent-domintante`. O receptor deve resolver
`participant` nos grupos e aplicar allowlist de grupos antes da RPC. Banco
valida o numero autorizado do AGENTS.md, mas nao armazena a lista de grupos.
Mensagens com identidade `@lid` precisam de mapeamento comprovado; nao
inferir numero nem aceitar nome de exibicao.

Deduplicacao por `(instancia, provider_message_id)`. Repeticao com remetente,
conversa ou texto diferentes e erro, nao um novo comando. Nunca persistir
token da instancia junto da mensagem. RPC pode devolver erro de dados:
receptor nao deve confirmar processamento que nao foi salvo.

Ordem por `entradas.ordem`, nao pelo UUID. Reserva trava a conversa; apenas
uma entrada dela pode executar por vez. Entrada `falhou` bloqueia a conversa
ate revisao. Lease vencida sem inicio volta a `aguardando`; depois de inicio
vira `falhou` e bloqueia a tarefa. Reinicio nao prova que uma acao externa
falhou. Esse caso requer reconciliacao, nao nova execucao automatica.

## Entrega

| RPC | Parametros | Retorno |
|---|---|---|
| `worki_reservar_saida` | `p_owner text`, `p_lease_s integer=120` | Array de zero ou uma linha de `saidas` |
| `worki_registrar_envio` | `p_saida_id uuid`, `p_owner text`, `p_estado text`, `p_provider_message_id text=null` | Boolean |

Estados exatos: `pendente`, `enviando`, `enviada`, `falhou`, `incerto`.
Mapear `ResultadoEnvio.ENTREGUE` da aplicacao para `enviada` no banco.
Reserva incrementa tentativas, coloca `enviando` e atribui lease. So o dono
da lease registra o resultado. `falhou` recebe backoff, com ate 5 tentativas.
Timeout, conexao interrompida ou queda depois do envio devem virar `incerto`.
`incerto` nao e reservado novamente e bloqueia entregas seguintes daquela
conversa. O reconciliador da aplicacao ainda precisa consultar evidencias
na Evolution antes de decidir se pode marcar enviada ou liberar retry.

Enviar para `saidas.chat_jid`, incluindo grupo autorizado. Nao substituir
o destino pelo numero do participante. A saida tem chave unica por entrada;
repetir um envio nao exige rodar Hermes novamente.

## Aprovacoes

| RPC | Parametros | Retorno |
|---|---|---|
| `worki_pedir_aprovacao` | `p_conversa_id uuid`, `p_acao text`, `p_alvo text`, `p_payload_hash text`, `p_descricao text`, `p_payload jsonb`, `p_tarefa_id uuid=null` | Bigint da acao |
| `worki_aprovar_acao` | `p_conversa_id uuid`, `p_palavra text`, `p_aprovador text`, `p_acao_id bigint=null` | Bigint confirmado |
| `worki_consumir_aprovacao` | `p_acao_id bigint`, `p_conversa_id uuid`, `p_acao text`, `p_alvo text`, `p_payload_hash text` | Boolean, true uma vez |

Hash SHA-256 de 64 caracteres hexadecimais minusculos, calculado a partir
do payload e da versao concreta do artefato. Payload alterado para mesmo
alvo cancela autorizacao anterior. Guard deve chamar consumo imediatamente
antes da ferramenta protegida e abortar se false; texto no prompt nao basta.
Consumida significa autorizacao usada, nao prova de sucesso externo.
Guardar evidencia e ID externo em `etapas_tarefa`, auditar ferramenta e
resultado. Queda depois de consumo exige revisao antes de repetir a acao.

| Acao | Palavra exata | Validade |
|---|---|---|
| `merge` | `aprova` | 24 h |
| `deploy_producao` | `sobe` | 30 min |
| `campanha_anuncio`, `alterar_orcamento` | `confirma` | 2 h |
| `pausar_anuncio` | `confirma` | 30 min |
| `gasto` | `confirma` | 1 h |

O backend valida identidade antes de encaminhar a palavra. Com duas
pendencias, exigir ID explicito; sem pendencia, palavra nao autoriza nada.
Teste cobriu palavra null, prazo vencido, hash alterado e consumo duplicado.
O teto de gasto continua exigindo verificacao na ferramenta de negocios.

## Memoria e contexto

`worki_salvar_memoria(p_proprietario text,p_projeto text,p_chave text,
p_valor text,p_tipo text,p_fonte text,p_confirmada boolean=false)` retorna UUID.
Tipos: `fato`, `preferencia`, `decisao`, `hipotese`. Sem confirmacao, grava
`hipotese`. A confirmacao deve vir de evidencia do proprietario, nao de
inferencia do modelo. Fonte deve apontar mensagem, documento ou decisao.
Correcao substitui versao anterior, preservando historico e `substitui_id`.

Antes de responder, recuperar conversa e resumo, mensagens recentes,
tarefas ativas/bloqueadas e memorias ativas do proprietario, filtrando
`projeto_id` por projeto atual ou null. Nao incluir memorias de outro projeto.
Memorias do Supabase precisam ser recuperadas pelo worker; habilitar a
memoria nativa do Hermes sozinho nao faz essa integracao.

Terminal e WhatsApp devem consultar os mesmos IDs de tarefa/projeto. Usar
o mesmo repo nao une sessoes locais automaticamente. O `state.db` local
no Windows nao foi copiado para a VPS; banco ja suporta nova sessao persistida
no container, com contexto recuperado antes da primeira resposta.

RAG, ingestao de documentos, embeddings e busca hibrida nao foram implementados
nesta etapa. `memorias` guarda fatos estruturados; nao e um indice de documentos.

## Validacao e infraestrutura

Migracoes aplicadas e historico remoto iniciado. Teste
`supabase/tests/runtime_portugues.sql` executado no Postgres real como
`service_role`, com rollback. Cobertura: dedupe, FIFO, lease, checkpoint,
commit atomico, sessao, outbox, reinicio simulado, aprovacoes e correcao de memoria.
Teste concorrente real: duas transacoes disputaram duas entradas da mesma
conversa; apenas uma reservou. Fixtures internas foram removidas.

Antes de declarar aplicacao pronta, testar REST com credenciais do servico,
Hermes real e envio Evolution; esses testes ainda dependem do container.
O teste SQL nao equivale a um teste ponta a ponta do WhatsApp.

EasyPanel: `n8n/worki-agent`, parado. Volumes do tipo VOLUME verificados:
`hermes-data` em `/opt/data`; `worki-workspace` em `/workspace`. Nao sao bind
mounts na configuracao do painel. O driver e o filesystem efetivo devem
ser verificados no container, junto de `journal_mode=WAL`, gravacao e retomada
apos reinicio. Limites configurados: 1 CPU / 1536 MB.

Nao mexer em dominios nem trocar webhook antes de validar o novo receptor.
Cutover deve parar o relay antigo antes de habilitar o novo caminho.
Merge e deploy em producao seguem a aprovacao do AGENTS.md.
