# Recomendações pendentes e achados em aberto

Estado: depois do merge `7cd20e7` (Hermes completo + caminho de falha) e deste
patch. Tudo abaixo é o que **ainda não foi feito**, em ordem de prioridade, com
o que foi verificado e o que é só leitura de código.

---

## 0. Migrações do repositório que podem não estar no Supabase

**Prioridade alta. Confirmar antes de qualquer outra coisa.**

A lista de migrações do projeto Supabase (consultada hoje) tinha só as três
primeiras (`20261002231514`, `...524`, `...823`). O repositório tem mais quatro:

| Migração | O que faz | Estado no Supabase |
|---|---|---|
| `20261003120000_outbox_so_no_sucesso` | Falha **não** cria saída: o log técnico não vai mais ao WhatsApp (já houve `hermes chat -q` chegando como resposta) | **Não consta na lista** |
| `20261003150000_falhou_nao_tranca_conversa` | `falhou` não trava a conversa | **Equivalente aplicado** pela `...190000` abaixo |
| `20261003160000_incerto_nao_tranca_envio` | Saída `incerto` não trava as saídas seguintes | **Não consta na lista** |
| `20261003190000_falha_nao_trava_conversa` | Mesma correção da `...150000` (escrita sem eu ter visto a outra) | Aplicada à mão no SQL Editor |

Observações:

- A `...190000` e a `...150000` têm o mesmo efeito. Aplicar as duas, em qualquer
  ordem, é inofensivo; a `...190000` acrescenta só um `revoke/grant` defensivo.
- "Não consta na lista" não prova que não foram aplicadas: podem ter sido
  coladas à mão no SQL Editor, que não registra a versão. Os dados de produção
  sugerem que **não**: a conversa travou por `falhou` de novo às 14:48 (espera de
  796 s), depois do commit da correção.

### Como confirmar (SQL Editor; só lê)

```sql
select
  pg_get_functiondef('public.worki_concluir_entrada(uuid,text,text,text,boolean)'::regprocedure)
    like '%Outbox SO no caminho de sucesso%'                           as tem_120000_outbox_so_sucesso,
  pg_get_functiondef('public.worki_reservar_saida(text,integer)'::regprocedure)
    not like '%in (''enviando'',''incerto'')%'                          as tem_160000_incerto_nao_tranca,
  pg_get_functiondef('public.worki_reservar_entrada(text,integer)'::regprocedure)
    not like '%in (''processando'',''falhou'')%'                        as falhou_nao_tranca;
```

`falhou_nao_tranca` deve dar `true` (a `...190000` foi aplicada). Se as duas
primeiras derem `false`, aplicar `20261003120000` e `20261003160000` na ordem.

### O código funciona nos dois estados

Foi o que este patch ajustou: `queue.concluir` devolve o id da saída criada (ou
`None`), e `relay._concluir_falha` avisa o usuário direto **só quando** o banco
não criou saída. Verificado em Postgres na cadeia completa:

| Banco | Falha do Hermes | O usuário recebe |
|---|---|---|
| Sem a `...120000` | RPC cria saída com o aviso neutro | O aviso, via outbox |
| Com a `...120000` | RPC não cria saída (texto vai para a auditoria) | O aviso, enviado direto pelo worker |

Em nenhum dos dois o log técnico chega ao WhatsApp. Custo: com a `...120000`, o
campo `auditoria.comando` guarda o aviso neutro e não o erro técnico; o erro
técnico fica no log do servidor (`worki.queue`).

---

## 1. Achado em aberto: a entrega de resposta não registra o envio

**Prioridade alta. NÃO corrigido neste patch, de propósito (ver "Por que não").**

### O que foi verificado (reproduzido em Postgres 16 local, com as 7 migrações)

Fluxo do worker em `integracoes/worker/relay.py::processar`:

1. `worki_concluir_entrada` cria a saída com `status='pendente'`.
2. O worker envia direto pela Evolution (`ev.enviar_texto`).
3. O worker chama `marcar_saida` → `worki_registrar_envio`.

Só que `worki_registrar_envio` só atualiza linhas com `status='enviando'` **e**
`lease_owner = p_owner`. A saída recém-criada está `pendente`, sem dono.
Resultado, reproduzido:

```
status saida apos concluir:  pendente
registrar_envio retornou:    false          <- falha em silêncio, sem erro
status saida apos marcar:    pendente, enviado_em=NULL
proxima rodada reserva a MESMA saida? sim
```

O Python ignora o `false` (o RPC não levanta exceção), então ninguém percebe.

### Evidência em produção (consistente, não conclusiva)

Na consulta de tempos, `enviado_em` veio `NULL` nas **20 últimas** respostas.
Isso bate com a reprodução, mas não prova o efeito final (ver abaixo).

### O que pode acontecer depois (NÃO verificado em produção)

Na rodada seguinte, `entregar_saidas` reserva essa mesma saída. A reserva devolve
a linha já como `enviando` (verificado: `status=enviando | tentativas=1`), então
**toda** entrega por esse caminho entra na ramificação de reconciliação
(`if saida.get("status") in ("enviando","incerto")`), inclusive a primeira.
`ev.reconciliar` lista as mensagens da conversa e decide:

| Veredito | Efeito |
|---|---|
| `entregue` (achou o texto) | marca `enviada`, sem reenviar. É o caso bom |
| `falhou` (não achou o texto, por exemplo a Evolution ainda não indexou) | **reenvia: mensagem duplicada no WhatsApp** |
| `incerto` (a listagem veio vazia ou deu erro) | essa saída fica `incerto` para sempre: nada reconcilia `incerto` (`saidas_incertainas` não é chamada por ninguém e a reserva só pega `pendente`/`falhou`). Com a `...160000` aplicada ela não trava mais as saídas seguintes; sem ela, trava a conversa |

### Para saber qual caso acontece (rodar no SQL Editor; só lê)

```sql
-- 1) distribuição de estados das saídas
select status, count(*) as qtd,
       sum((enviado_em is null)::int) as sem_enviado_em,
       max(tentativas) as max_tentativas
from public.saidas group by status order by qtd desc;

-- 2) as últimas 20, com o erro registrado
select status, tentativas, ultimo_erro, enviado_em is not null as tem_envio,
       left(texto, 40) as texto
from public.saidas order by criado_em desc limit 20;
```

Como ler: muitas `incerto` indicam o terceiro caso. `enviada` com `tentativas >= 2`
indica reconciliação resolvendo. `tentativas >= 2` com `ultimo_erro` vazio e o
mesmo texto repetido no WhatsApp indica o segundo caso (duplicata).

### Por que não foi corrigido aqui

A correção depende do que a consulta acima mostrar, e envolve o caminho que
entrega mensagens ao usuário. Duas opções, com custo diferente:

- **A. Enviar sempre pela reserva** (recomendada): em `processar`, trocar o envio
  direto por `self.entregar_saidas()`, e distinguir primeira tentativa de retomada
  por `tentativas == 1` (primeira: envia direto; maior: reconcilia antes). Mantém
  a invariante "só vira `enviada` quem foi reservada". Exige atualizar os testes.
- **B. Aceitar `pendente` em `worki_registrar_envio`**: uma linha de SQL, mas
  afrouxa a invariante e, num timeout, deixaria saídas `incerto` presas (nada as
  reconcilia). Só faz sentido junto de um laço que reconcilie `incerto`.

### Lacuna de teste que escondeu isso

O `DBFake` de `testes/test_fechamento.py` implementa `marcar_saida` atualizando o
estado **sempre**. O banco real recusa em silêncio. Por isso os 15 testes passam
com o bug presente. Ao corrigir, o fake precisa reproduzir a regra do SQL
(`pendente` não vira `enviada` sem reserva).

---

## 2. Segurança

| Item | Estado | O que fazer |
|---|---|---|
| Edge Function `evolution-webhook` sem autenticação | **Corrigida neste patch, mas só vale depois de publicar a função** | `supabase functions deploy evolution-webhook --no-verify-jwt`, definir o secret `WORKI_WEBHOOK_SECRET` no Supabase e configurar o cabeçalho `X-Webhook-Secret` no webhook da Evolution. Sem o secret, mantém o comportamento antigo e o GET mostra `segredo:false` |
| Grupo liberado com lista vazia na Edge Function | Corrigido neste patch | — |
| `integracoes/supabase/relay.py` roda `hermes -z` na máquina do dono, lendo a tabela `falas` | Não alterado | Se a Edge Function ainda está em uso, ela é a porta de entrada desse relay. Decidir: desligar de vez (a Evolution aponta só para o receptor Python) ou manter protegida |
| Segredos de todos os serviços do EasyPanel apareceram em texto puro numa sessão | Fora do repositório | Girar a service-role do Supabase, os tokens do Meta Ads, a chave da Evolution e as senhas de admin |
| URL de deploy do EasyPanel (com token) em HTTP | Fora do repositório | Girar o token do serviço (`refreshAppDeployToken`) depois que tudo estiver estável |
| `guard.py` (aprovação ligada ao alvo e ao hash do artefato) não é chamado | Não alterado | Ligar às ferramentas perigosas, ou usar `approvals.mode: manual` do Hermes (item 3) |

---

## 3. Hermes na VPS

- **Fixar a imagem por digest.** `ARG HERMES_IMAGE=...:latest` ainda muda a cada
  rebuild: `docker manifest inspect nousresearch/hermes-agent:latest`.
- **Aprovações de verdade.** No `/opt/data/config.yaml`:
  `approvals.mode: manual` e `approvals.unattended_mode: deny`. A seção 3 do
  `AGENTS.md` continua sendo só texto para o modelo.
- **Skills próprias.** O Hermes agora sincroniza as skills que vêm na imagem.
  As skills de marketing do dono não existem lá; `skills/` só tem o `_modelo`.
- **MCPs** (Supabase leitura, Meta Ads leitura, GitHub): não configurados.
- **Timeout x política.** `WORKI_TASK_TIMEOUT_SECONDS=1500` é maior que o limite
  de esforço da seção 13 do `AGENTS.md` (cerca de 15 minutos), de propósito: o
  agente para sozinho e explica, e o timeout é só a rede de segurança.

---

## 4. Tempo de resposta

Medido nas últimas 20 mensagens (antes de `WORKI_RECOVERY_POLL_SECONDS` ir de
30 para 3):

| Componente | Observado |
|---|---|
| Espera na fila | 2 a 30 s (polling de 30 s); 338 s e 796 s em duas conversas travadas por `falhou` |
| Hermes, mensagem simples | 11 a 38 s |
| Hermes, tarefa (imagem, instalação) | 100 a 400 s |

- Feito: polling em 3 s; timeout em 1500 s; aviso de andamento opt-in
  (`WORKI_ACK_AFTER_SECONDS`, padrão 0).
- Pendente: o Hermes é iniciado do zero a cada mensagem (`hermes chat -q`). Reduzir
  os 11 a 25 s de uma mensagem simples exige manter um processo vivo, o que é uma
  mudança de arquitetura. Não foi tentada.
- `enviado_em` nulo impede medir a última perna (Evolution): ver o item 1.

---

## 5. Limpeza do repositório (baixo risco, sem pressa)

- `testes/` e `tests/` coexistem. Unificar.
- Arquivos legados fora do caminho de execução: `integracoes/supabase/queue_legacy_en.py`,
  `outbox_legacy_en.py`, `schema.sql`, `schema_falas.sql`, `schema_para_colar.sql`.
  Confirmar que nada os usa e remover.
- `README.md` ainda descreve o canal antigo (Edge Function) como o ativo.
- O `HEALTHCHECK` do Dockerfile só confere `/health` (processo vivo); `/ready`
  (Supabase e worker) é o que diz se o agente está pronto.
