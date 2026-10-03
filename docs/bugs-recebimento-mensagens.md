# Bugs no recebimento de mensagens — 2026-10-03

Diagnóstico feito depois de o agente de WhatsApp ficar mudo. São quatro
problemas independentes. Cada um escondia o seguinte: enquanto um estava
ativo, o próximo não aparecia.

O agente acabou funcionando no fim do dia. Todos os quatro estão
corrigidos e com teste de regressão.

---

## 1. Nome do evento da Evolution não casava — toda mensagem era descartada

**Sintoma:** nada chegava, nunca. Nenhum erro, nenhum log.

**Causa:** a Evolution 2.3.7 entrega o evento como `messages.upsert`
(minúsculo, com ponto). O receptor fazia:

```python
nome = (evento.get("event") or "").split(".")[-1]   # -> "upsert"
```

e comparava com `TIPOS_ACEITOS = ("MESSAGES_UPSERT",)`. `"upsert"` não
bate, então `normalizar()` devolvia `None` **antes de ler o texto**.

A Evolution entregava no webhook, o receptor respondia 200, e nada era
processado. Falha silenciosa: o único sintoma era a ausência de resposta.

As três grafias que a Evolution usa:

```
MESSAGES_UPSERT                 versão antiga
messages.upsert                 2.3.x
evolution_api.messages.upsert   com namespace
```

**Correção** (`d8620d0`): normaliza ponto e hífen para `_`, sobe para
caixa alta, corta o namespace. As três chegam a `MESSAGES_UPSERT`.

```python
bruto = str(evento.get("event") or evento.get("eventType") or "").strip()
nome = bruto.replace("-", "_").replace(".", "_").upper()
if "_" in nome and not nome.startswith("MESSAGES_"):
    nome = "MESSAGES_UPSERT" if nome.endswith("MESSAGES_UPSERT") else nome
if nome and nome not in TIPOS_ACEITOS:
    return None
```

**Teste:** `testes/test_eventos_evolution.py`, com os payloads reais
capturados da Evolution.

**Como achar de novo:** se a Evolution está `open`, o webhook responde
200 e mesmo assim nada entra, o nome do evento é a primeira coisa a
comparar. Versão da Evolution é a pista principal — 2.3.x usa minúsculo.

---

## 2. Coluna `remetente` não existe — o worker morria antes do Hermes

**Sintoma:** entrada ia para `processando`, lease vencia, voltava para a
fila, ciclo se repetia.

**Causa:** `reservar()` consultava `select=texto,remetente`. A coluna em
`mensagens` chama-se `de`. O PostgREST respondia 400 e a exceção subia
antes de chamar o Hermes.

Confirmado contra o banco:

```
select=texto,de        -> HTTP 200
select=texto,remetente -> HTTP 400
```

**Correção** (`3ba9ccb`): pedir `de` na consulta e ler `m.get("de")` na
montagem da entrada. **Nenhuma coluna nova no banco** — o schema
versionado em `supabase/migrations/` já usa `de`.

**Teste:** `testes/test_colunas_fila.py` confere que a consulta e a
montagem usam a mesma chave e que as colunas lidas existem no schema.

**Como achar de novo:** `grep -n "select=" integracoes/supabase/queue.py`
e comparar com `\d mensagens`. Coluna divergente entre código e schema
sempre dá 400 no PostgREST, nunca erro legível.

---

## 3. Log do comando entrava na outbox — você recebia `hermes chat -q`

**Sintoma:** a tabela `saidas` ganhou a linha:

```
texto  = 'hermes chat -q\n'
status = 'pendente'
```

Esse texto não é resposta do agente. É o `log_cmd` do adapter
(`integracoes/hermes/adapter.py:216`).

**Causa:** quando o Hermes falha, o relay passa o **log** como resposta:

```python
if not r.ok:
    self.db.concluir(entrada.id, r.log or "hermes falhou")
```

E `worki_concluir_entrada` inseria em `saidas` **sem olhar `p_sucesso`**.
Ou seja: log de falha virava mensagem na fila de envio.

**Correção** (`3cb6888`, migration `20261003120000`): o `insert into
saidas` fica dentro de `if p_sucesso then`. Em falha, a entrada vai para
`falhou`, a tarefa fica `bloqueada`, o motivo vai para `auditoria`, e
**nada** entra na outbox.

Idempotência preservada: entrada já concluída continua devolvendo a
saída existente, sem duplicar.

**Como achar de novo:** se `saidas.texto` tem cara de comando
(`hermes`, `curl`, flag `--algo`) em vez de texto para o cliente, o log
está vazando para a fila de envio.

---

## 4. Loop de retomada — o agente se mandava ler a si mesmo

**Sintoma:** respostas repetindo o cabeçalho.

```
Já li a sessão 20261003_131416_57f90a duas vezes — ela só repete esse cabeçalho
```

```
Li a sessão 20261003_131416_57f90a — ela não tem nada além do cabeçalho
```

A sessão chegou a **66 mensagens**, quase todas o mesmo aviso.

**Causa:** duas coisas juntas.

`montar_contexto()` injetava no prompt:

> Você está retomando a sessão 57f90a desta conversa. O contexto anterior
> está nela — não comece do zero.

E o adapter chamava `hermes chat --resume 57f90a` — **a mesma sessão
que estava sendo escrita**. Então cada mensagem gravava mais uma cópia
do aviso dentro do contexto que ela mesma manda reler. Acúmulo sem
limite.

`--resume` já devolve o histórico inteiro. O aviso era redundante.

Agravante: tarefas em `bloqueada` com resultado `hermes chat -q` (bug 3)
entravam no prompt como "tarefas em andamento", com `proxima_acao:
"Revisar execucao antes de retomar"`. O agente procurava execução que
não existia e respondia que não tinha nada para retomar.

**Correção** (`pendente de commit`):

- o aviso de retomada saiu do prompt
- só entram tarefas com trabalho real: status `ativa`/`pendente` ou com
  `checkpoint`. Tarefa `bloqueada` sem checkpoint não vira "em andamento"

O que continua no prompt é o útil: objetivo, próxima ação e checkpoint.

**Teste:** `testes/test_retomada.py` — o aviso não aparece, tarefa
bloqueada é filtrada, tarefa real continua aparecendo com próxima ação e
checkpoint, e o contexto não cresce entre chamadas.

**Como achar de novo:** `sqlite3 /opt/data/state.db` e contar as mensagens
da sessão. Se `message_count` cresce muito sem o conteúdo mudar, olhe o
prompt montado em `montar_contexto()`.

---

## 5. Diagnóstico cego — o log não carregava o motivo da falha

**Sintoma:** `auditoria.comando` guardava só `"hermes chat -q\n"` com
`resultado: erro`. Sem código de saída, sem stdout, sem stderr.

**Por que isso custou horas:** o mesmo comando roda na máquina de
desenvolvimento e devolve `OK`. Sem o stderr do container, não dava
para saber que faltava credencial — a falha parecia de código.

**Correção** (`ee5183d`): o log agora carrega

```
exit=1
stdout='It looks like Hermes isn't configured yet -- no API keys or providers found.'
stderr=...
```

e o `subprocess` recebe ambiente explícito: `HOME`, `USERPROFILE` e
`HERMES_HOME` apontam para `WORKI_HERMES_HOME` (`/opt/data`, o volume),
com `/opt/hermes/.venv/bin` no `PATH`.

Foi essa correção que revelou a causa raiz do Hermes não funcionar.

**Regra:** falha que não registra `exit`, `stdout` e `stderr` não está
diagnosticada. Não é diagnóstico, é registro de que algo quebrou.

---

## 6. AGENTS.md fora do diretório de trabalho

**Sintoma:** `/workspace` vazio. O agente respondia sem identidade, sem
allowlist e sem as proibições de venda do AGENTS.md.

**Causa:** o `Dockerfile` copiava `AGENTS.md` para `/app`, mas o Hermes
lê as regras do **diretório de trabalho** (`cwd=/workspace`).

**Correção:** `COPY AGENTS.md /workspace/AGENTS.md`.

**Como achar de novo:** `docker exec $C ls -la /workspace`. Vazio = o
agente está sem as regras do repo.

---

## Resumo

| # | Problema | Commit | Teste |
|---|---|---|---|
| 1 | Nome do evento da Evolution | `d8620d0` | `test_eventos_evolution.py` |
| 2 | Coluna `remetente` inexistente | `3ba9ccb` | `test_colunas_fila.py` |
| 3 | Log na outbox | `3cb6888` | migration `20261003120000` |
| 4 | Loop de retomada | — | `test_retomada.py` |
| 5 | Log sem diagnóstico | `ee5183d` | — |
| 6 | AGENTS.md fora do cwd | — | — |

Em ordem de quanto custou descobrir: 1, 5, 3, 2, 4, 6.

O 1 escondia todos os outros — sem ele não havia sinal de nada.

---

## Pendências conhecidas

**Uma entrada `falhou` trava a conversa inteira.** A RPC
`worki_reservar_entrada` recusa qualquer conversa que tenha entrada em
`processando` ou `falhou`:

```sql
and not exists(select 1 from entradas x where x.conversa_id=cv.id
              and x.status in ('processando','falhou'))
```

Aconteceu duas vezes em 2026-10-03. Resolvido na hora cancelando a
entrada esgotada, mas o defeito continua: qualquer entrada que esgote as
tentativas vai travar a conversa de novo. A correção é tratar
`max_tentativas` como terminal, não como `falhou`.

**Imagem sem digest.** O `Dockerfile` usa
`FROM nousresearch/hermes-agent:latest`, e o próprio comentário do
arquivo avisa que deveria ser fixado por digest. Um deploy pode pegar
uma imagem diferente da testada. Nunca houve problema atribuído a isso,
mas é risco aberto.

**Sessão corrompida ainda no volume.** A sessão
`20261003_131416_57f90a` está com 66 mensagens de loop. As correções
impedem o crescimento, mas o histórico antigo continua lá. Convém zerar.

---

## Diagnóstico sem MCP

O MCP do EasyPanel aqui só faz `deployAppService` e `inspectAppService`.
`queryServiceLogs` responde `BAD_REQUEST`. Docker não existe na máquina
de trabalho.

Acesso por SSH resolveu:

```
ssh -i ~/.ssh/id_vps_claude root@147.79.82.150
docker exec -it $(docker ps -qf name=worki-agent) <comando>
```

Pelo SSH dá para ler `state.db`, listar `/workspace`, ver o
`hermes --version` e rodar o login do portal. Foi assim que se confirmou
que o Hermes **estava** instalado (`v0.21.5` em `/opt/hermes/bin/hermes`)
e que o volume não tinha `auth.json`.