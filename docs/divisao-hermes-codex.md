# Divisao de trabalho — worki-agent (atualizado 02/10/2026)

Hermes = aplicacao (receptor, worker, adaptador, Docker, testes).
Codex = Supabase e EasyPanel (migracoes, RLS, permissoes, volumes,
validacao de persistencia).

Divisao definida pelo dono da conta.

---

## 1. Onde estou

| Item | Valor |
|---|---|
| Branch | `feat/fila-sessoes-outbox` |
| Ultimo commit | `e0783c6` |
| PR | #1, aberto, **NAO mergeado** |
| Caminho de trabalho | `C:\Users\Samsung\AppData\Local\hermes\cache\scratch\wa\head` |
| Outro checkout do mesmo repo | `C:\Users\Samsung\repos\worki-agent` (em `master`, tem o `.env`) |

**Cuidado com dois working trees do mesmo repo.** Quem editar em um
precisa `git fetch` da branch do outro antes de commitar.

---

## 2. Schema: decisao tomada

O dono decidiu: **usar as tabelas em portugues que ja existem** —
`entradas`, `saidas`, `conversas`, `mensagens`.

Consequencias aplicadas no meu lado:

- A migracao `001_fila.sql` (que criava `jobs`, `inbound_messages`,
  `conversations`, `outbox` em ingles) foi **removida** do repo. Nao
  existe mais, para ninguem aplicar por engano.
- Os clientes do commit anterior viraram `queue_legacy_en.py` e
  `outbox_legacy_en.py`. Nao sao importados por nada; ficam so como
  historico.
- `integracoes/supabase/queue.py` agora fala com as tabelas em portugues
  e com as RPCs do contrato.

---

## 3. Contrato de RPCs que eu preciso

Esta em `docs/contrato-rpcs.md`, com DDL atual lido do banco, as RPCs com
assinatura e comportamento, e as 3 adaptacoes necessarias.

Resumo do que eu chamo (nomes podem mudar, me avise):

| RPC | Para que |
|---|---|
| `worki_registrar_evento` | mensagem + entrada numa transacao; devolve `ja_existia` |
| `worki_reservar_entrada` | reserva atomica (`for update skip locked`), uma por conversa |
| `worki_renovar_lease` | estica lease de tarefa longa |
| `worki_concluir_entrada` | conclui ou devolve com backoff |
| `worki_registrar_saida` | grava resposta ANTES do envio; `idempotency_key` |
| `worki_marcar_saida` | atualiza saida + incrementa tentativas |
| `worki_recuperar_leases` | devolve leases vencidas (uso no boot) |
| `worki_consumir_aprovacao` | consome aprovacao atomicamente |
| `worki_expirar_aprovacoes` | limpa vencidas |

### As 3 adaptacoes que preciso no banco

1. **`saidas` precisa do estado `incerto`.** Hoje o check e
   `('pendente','enviando','enviada','falhou')`. Sem `incerto`, um timeout
   apos o envio cai em `falhou` e e reenviado as cegas — resposta
   duplicada no WhatsApp.
2. **Falta tabela `aprovacoes`.** O guard tem 23 testes passando, mas o
   store real precisa da tabela. DDL no contrato.
3. **Falta `conversa_id` em `entradas`/`mensagens`.** Alternativa: a RPC
   de reserva faz o join e devolve `conversa_id`. Qualquer uma das duas
   serve — me diga qual vc faz.

---

## 4. O que eu ja fiz (nao mexa)

```
integracoes/evolution/webhook.py      receptor HTTP
integracoes/evolution/cliente.py      cliente Evolution + reconciliacao
integracoes/worker/relay.py           worker duravel
integracoes/supabase/queue.py         cliente da fila (contrato novo)
integracoes/config.py                 config do ambiente
integracoes/aprovacao/store_supabase.py  store do guard
Dockerfile, entrypoint.sh, .dockerignore, requirements.txt
docs/contrato-rpcs.md                 o que eu preciso do banco
docs/deploy-easypanel.md              procedimento de deploy
```

---

## 5. Testes

```
python testes/test_guard.py          23 ok
python testes/test_fechamento.py     35 ok
python testes/test_adapter.py        13 ok (chama o Hermes de verdade)
python testes/test_adapter.py --rapido   so os locais, sem token
```

`test_fechamento.py` cobre o criterio de aceite: dedupe do webhook,
duas workers em thread nao pegando o mesmo job, uma entrada por conversa,
sessao retomada depois de "reinicio", envio falho reenviando sem rodar o
Hermes, envio incerto reconciliando sem reenviar, chave de idempotencia
estavel.

Verifiquei o servidor HTTP de ponta a ponta em porta de teste: `/health`
200, `/ready` 503 sem worker, POST sem segredo 401, com segredo 200,
duplicata 200 sem gravar duas vezes, rota errada 404.

---

## 6. Pendencias — e uma e seria

**Nenhuma RPC rodou contra Postgres.** O contrato ainda nao foi aplicado.
Ate la o receptor responde 200 mas devolve 500 em toda persistencia, e o
worker loga "fila indisponivel" a cada 30s.

Outras pendencias, todas minhas:

- Dockerfile **nunca foi buildado** (sem Docker nesta maquina).
- Imagem do Hermes **nao fixada por digest** — o `Dockerfile` usa
  `ARG HERMES_IMAGE=nousresearch/hermes-agent:latest` de proposito, com o
  comentario de que fixar sem smoke test nao da garantia nenhuma.
- `fetchMessages` (reconciliacao) **nao testado** nesta versao da
  Evolution. Se o endpoint nao existir, o veredito fica `incerto` e a
  saida nao e reenviada — comportamento seguro, mas a resposta para ate
  resolver.
- **`task_steps` nao existe.** Esta e a janela residual: se o processo
  cair entre o Hermes responder e a saida ser gravada, a entrada volta
  pela lease vencida e o Hermes roda de novo. O `idempotency_key` fecha
  as janelas *depois* da gravacao; esta, antes. Documentado em
  `docs/deploy-easypanel.md` secao 8.
- Skill `criar-publicar-pagina` nao existe. Sem ela, o guard **existe mas
  nao barra nada** — a regra esta testada e o store pronto, mas nenhum
  caminho de execucao o chama ainda.
- Memoria estruturada e RAG nao existem.
- CLI de continuidade no computador nao existe.

---

## 7. Risco que preciso que voce verifique no EasyPanel

O `state.db` do Hermes roda em **WAL**. A doc oficial avisa que bind
mount que cruza fronteira de VM (9p / drvfs / virtiofs) corrompe WAL
**em silencio** — o `PRAGMA integrity_check` passa e o dado some.

Se o volume do `/opt/data` nao for nativo, **o teste de fechamento falha
sem erro visivel**: o agente simplesmente esquece o contexto as vezes.

Como verificar, e o que fazer se nao for nativo, esta em
`docs/deploy-easypanel.md` secao 4.

---

## 8. Medidas de seguranca (verificadas, sem alarme)

Nesta sessao, do meu lado:

- Anon nao le `falas` (34 linhas) nem `auditoria` (205): devolve `[]`.
- INSERT anon em `auditoria` devolve 401.
- DELETE/UPDATE anon devolvem 204 afetando **0 linhas**. Confirmei as
  contagens depois: nada mudou. Um 204 nao prova sucesso — e a armadilha
  #5 do seu `docs/armadilhas.md`.

## 9. Duas pendencias suas (do PR anterior, continuam abertas)

- `worki-agent` esta **publico** e legivel por anon, com o `AGENTS.md`
  (numero `558592494552` incluso) exposto. Recomendo privado.
- `WorkiDigital/servidor-api` **nao existe**. O `AGENTS.md` o lista como
  repo de referencia; convem corrigir.

## 10. Sequencia

1. Voce revisa `docs/contrato-rpcs.md` e me diz o que mudou de nome.
2. Voce aplica as DDL e as RPCs.
3. Me avisa que rodou.
4. Eu troco os nomes nos clientes e rodo integracao contra o banco real.
5. Build da imagem + smoke test com Hermes autenticado.
6. So entao deploy.

Nada meu toca em migracao, RLS, policy ou config do EasyPanel.

Nenhum token, senha ou valor de `.env` neste documento.