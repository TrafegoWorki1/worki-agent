# Fechamento da integracao — revisao de e0783c6

Revisado em 2026-10-02. Escopo: Codex assume Supabase e EasyPanel;
Vibe Code assume receptor, worker, adaptador, guard, Dockerfile e testes.
Nenhum desses arquivos de aplicacao foi alterado nesta revisao.

## Entrega do Codex

Banco `wxqwtyotkkshdjzzwjsk`: tres migracoes aplicadas, historico remoto
verificado, 13 RPCs com permissoes restritas ao backend. Dados existentes
preservados. Teste SQL transacional rodou no Postgres real com rollback;
duas transacoes reais disputaram a mesma conversa e somente uma reservou.
Replay local das tres migracoes e das assercoes passou em PGlite.

Tabelas canonicas: conversas, mensagens, entradas, saidas, tarefas,
etapas_tarefa, memorias e **acoes_pendentes**. Nao criar aprovacao duplicada
nem tabelas em ingles. `acoes_pendentes.id` e bigint; os demais IDs sao UUID.

Contrato final aplicado: `docs/contrato-supabase.md` e
`docs/contrato-rpcs.json`. O contrato de nove RPCs em `contrato-rpcs.md`
foi proposto pela aplicacao e precisa ser atualizado para o contrato real.

EasyPanel: servico `n8n/worki-agent` criado e parado, com volumes do tipo
VOLUME `hermes-data:/opt/data` e `worki-workspace:/workspace`.
Driver, filesystem, permissao e persistencia WAL ainda exigem teste no
container. Nenhum merge, deploy, troca de webhook ou dominio foi feito.

## Adaptacoes obrigatorias do cliente e worker

| Caminho atual | Adaptacao para o banco aplicado |
|---|---|
| `registrar_evento` | Usar `p_remetente` como JID canonico e `p_tipo`; remover `p_de`, `p_grupo_jid` e `p_tipo_mensagem`. Ler `duplicado`, nao `ja_existia`. |
| `reservar` / `Entrada.de_row` | Enviar apenas `p_owner`, `p_lease_s`. Retorno tem `id`, nao `entrada_id`, e nao traz join. Recuperar texto/remetente em mensagens e sessao em conversas pelos IDs retornados. |
| `renovar` | Remover `p_agora`. Banco usa seu proprio relogio. |
| Criacao direta de tarefa | Chamar `worki_iniciar_entrada` ANTES de Hermes: grava inicio e cria tarefa atomicamente. Sem isso uma queda aparenta que a execucao nunca iniciou. |
| Progresso por PATCH | Usar `worki_checkpoint_tarefa` com checkpoint JSONB e ownership da lease. |
| Sessao, saida e conclusao separados | Substituir por `worki_concluir_entrada(p_entrada_id,p_owner,p_session_id,p_resposta,p_sucesso)`, que faz o commit atomico. Nao usar `p_erro`. |
| `recuperar_leases` | Chamada sem parametros; retorna JSON com contagens, nao inteiro. |
| Listagem livre de saidas | Sender usa `worki_reservar_saida`; reserva atomica ja marca enviando e controla lease, backoff e limite. |
| `marcar_saida` | Usar `worki_registrar_envio` com owner. Mapear `entregue` do cliente para `enviada` no banco. |
| Store de aprovacao | Usar acoes_pendentes, bigint, estados aguardando/confirmada/consumida e RPCs pedir/aprovar/consumir do contrato final. Consumo recebe ID, conversa, acao, alvo e hash; sem `p_agora`. |

Nao criar wrappers que restabelecam a conclusao em varias transacoes ou
reserva por GET/PATCH. As RPCs sao SECURITY INVOKER com grants de service_role;
nao precisam virar SECURITY DEFINER para funcionar.

Queda depois do inicio de Hermes bloqueia a tarefa para reconciliacao,
em vez de executar novamente sem saber se houve efeito externo. A tabela
`etapas_tarefa` existe; registrar chave de operacao, input_hash, evidencia
e ID externo nos pontos de efeito. Isso precisa ser integrado ao executor;
a existencia da tabela sozinha nao garante retomada de cada ferramenta.

## Bugs encontrados na aplicacao

### 1. Guard autoriza mesmo quando o banco recusa consumo

`integracoes/aprovacao/guard.py`, `Guard.exigir`: ignora o retorno de
`self.store.consumir(...)`, marca o objeto consumida e devolve autorizacao.
Reproduzido com store que lista aprovacao valida e retorna False ao consumir:
o guard devolveu a aprovacao. Em disputa entre executores, ambos poderiam
seguir. Exigir retorno exatamente True; qualquer recusa deve bloquear.

Adicionar caso com consumo False e teste de concorrencia. Integrar o guard
ao caminho real de cada ferramenta protegida; atualmente nao ha chamada
que imponha a regra nas operacoes do Hermes. Restringir ferramentas sem
interceptacao ate essa integracao existir. Prompt e AGENTS nao substituem
o controle de execucao.

### 2. Perda de lease nao interrompe Hermes

`integracoes/worker/relay.py`, `_renovar_em_loop`: seta um Event quando a
renovacao retorna False, mas `processar` so verifica depois de
`hermes.responder` terminar. O adaptador usa subprocess.run, sem receber
esse Event. Erro de renovacao somente gera log e permite continuar.

Implementar cancelamento real do subprocesso e seus filhos, incluindo
SIGTERM; tratar falha de renovacao de acordo com o prazo ainda valido.
Nao iniciar nova acao externa sem ownership valido. Testar com subprocesso
que permanece executando quando a lease e perdida e verificar interrupcao.

### 3. Primeiro envio e reenviador ainda podem duplicar

`processar` envia diretamente para `entrada.de`, embora uma mensagem de
grupo deva receber resposta em `chat_jid`. Esse envio nao reserva saida
nem marca enviando antes da chamada; depois passa estado entregue ao banco,
que aceita enviada. Deixar todo envio para o sender com reserva atomica.

`entregar_saidas` continua enviando se marcar enviando falhar. Tambem
reenvia quando reconciliar levanta excecao. Esses caminhos precisam
interromper o envio e manter incerto se nao houver evidencia conclusiva.
Dois senders nao podem processar a mesma saida listada por GET.

`evolution/cliente.py` reduz o destino a digitos; validar formato de grupo
na versao real da Evolution, preservando a identidade da conversa.

### 4. Reconciliacao aceita evidencia de outra conversa

`reconciliar` busca 20 mensagens, compara trecho de 40 caracteres e aceita
mensagem recebida ou enviada sem filtrar chat nem janela temporal. Um
match pode ser de outra conversa; ausencia na amostra nao prova falha.

Exigir evidencia suficiente vinculada ao chat e ao envio: ID quando
disponivel, fromMe, horario e conteudo/operacao. Inconclusivo ou erro deve
continuar incerto, sem reenviar automaticamente. Verificar endpoint,
metodo, payload e paginacao de fetchMessages na Evolution instalada.

### 5. Dockerfile precisa de build real

Depois de USER hermes, COPY cria o entrypoint com ownership root e RUN
chmod tenta modifica-lo como hermes: erro de permissao esperado. Usar
COPY --chmod ou ajustar durante etapa root. Validar ownership de volumes
novos e existentes antes de rodar o usuario sem privilegios.

ENTRYPOINT customizado substitui o dispatcher oficial; nao preserva s6
ou stage2-hook automaticamente, apesar do comentario. Preservar o
bootstrap oficial com CMD ou implementar e testar supervisao, bootstrap,
permissoes e encerramento equivalentes. Conferir isso na imagem escolhida.

Fixar digest depois do smoke test. Garantir checkout, AGENTS e skills
disponiveis no diretorio de trabalho real: a imagem copia AGENTS apenas
para /app e /workspace vazio nao recebe o repo automaticamente.

### 6. Autenticacao, sessao e memoria ainda exigem fechamento

- Webhook sem WORKI_WEBHOOK_SECRET retorna autorizado. Para receptor
  exposto, exigir segredo na inicializacao e conferir transporte do header
  X-Webhook-Secret pela Evolution; teste com segredo configurado nao cobre
  inicializacao sem segredo.
- Adaptador ainda usa _sessao_nova como fallback da sessao mais recente
  do state.db. Nao escolher sessao criada por outro processo; usar somente
  ID associado a esta execucao e conversa.
- montar_contexto recupera sessao e tarefas, mas ainda nao integra resumo,
  mensagens recentes e memorias confirmadas por dono/projeto. Implementar
  recuperacao e gravação com fonte via worki_salvar_memoria.
- Config.carregar ignora variaveis de ambiente vazias e aceita mascara ***.
  Variavel explicitamente vazia deve sobrescrever arquivo e falhar quando
  obrigatoria; mascara nao pode virar credencial valida.

RAG e consulta de documentos ainda nao foram implementados. Memoria
estruturada e continuidade nao significam que o projeto inteiro terminou.

## Validacao necessaria antes do deploy

Reexecutados nesta revisao: guard 23 checks, fechamento 35 checks com
URLs/chaves ficticias, adaptador --rapido 5 checks. Todos passaram; os
fakes nao exercitam os defeitos descritos acima. Os 13 checks completos
de Hermes real informados pela outra ferramenta nao foram repetidos aqui.

1. Importar a entrega do banco no branch e alinhar a aplicacao ao contrato.
2. Adicionar casos de regressao que falhem antes das correcoes acima.
3. Testar chamadas REST reais como service_role, sem imprimir credenciais.
4. Buildar imagem, smoke test, fixar digest e testar volumes com WAL.
5. Simular quedas antes/depois de iniciar Hermes, gravar saida e enviar;
   dois workers/senders concorrentes; perda de lease e SIGTERM real.
6. Testar duas mensagens relacionadas com restart entre elas, isolamento
   entre conversas, grupo autorizado e correcao de memoria.
7. Devolver commit, resultados, digest e pendencias. Codex prepara a
   configuracao final do EasyPanel e cutover; merge e producao seguem
   autorizacao prevista no AGENTS.md. Nao alterar dominios/DNS.
