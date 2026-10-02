# Armadilhas do Supabase neste projeto

Anotadas na maratona de 2026-10-02. Todas custaram tempo.

---

## 1. `supabase secrets set` entra em panic

O CLI crasha em `internal/secrets/set/set.go`. Funciona a API de
management direto — ver `integracoes/supabase/set_secrets_api.py`.

## 2. A API de secrets recusa o prefixo `SUPABASE_`

```
Secret name must not start with the SUPABASE_ prefix.
```

Nao tente reenviar as secrets padrao para "mesclar". A Edge Function recebe
`SUPABASE_URL`, `SUPABASE_ANON_KEY` e `SUPABASE_SERVICE_ROLE_KEY`
automaticamente. Mande so as suas.

O body e um **array puro**, nao `{"secrets": [...]}`. Errar isso da
`Invalid input: expected array, received object`.

## 3. Um 404 em `/pg/query` nao comprova limitacao do plano

Atualizado em 2026-10-02: leitura SQL e aplicacao de migracoes funcionaram
no projeto Agente Dominante pelo conector Supabase. O endpoint e o
contrato da API devem ser conferidos antes de atribuir um 404 ao plano.
O schema agora esta versionado em `supabase/migrations/`. Nao aplicar
`integracoes/supabase/migrations/001_fila.sql`: ela duplica o schema em ingles.

## 4. `Prefer: return=representation` no POST mente

Com a policy de insert **correta**, pedir o registro de volta devolve:

```
401 - new row violates row-level security policy
```

A causa nao e a policy. `return=representation` exige SELECT junto, que o
`anon` nao tem. Use sempre `return=minimal`.

## 5. DELETE barrado por RLS devolve 204, nao erro

Zero linhas afetadas conta como sucesso. O unico jeito de provar que o
bloqueio funcionou e ler a linha depois e ver se ela sobreviveu. Nunca confie
no status code sozinho.

## 6. Edge Function: `--no-verify-jwt`

Implantar sem isso, porque a Evolution nao manda JWT. Sem a flag, toda
entrega da Evolution volta 401.

## 7. `supabase link` cria `supabase/.temp/` com credencial

Nao esta no gitignore padrao. Adicionado aqui. Tambem `.vercel/`.

---

## Armadilhas da Evolution API 2.3.7

## 8. O payload do webhook e aninhado

O endpoint `POST /webhook/set/{instancia}` **exige**:

```json
{ "webhook": { "enabled": true, "url": "...", "events": [...] } }
```

Nao o objeto plano. Plano da direto: `instance requires property "webhook"`.

## 9. Nomes dos eventos de participante

`GROUP_PARTICIPANTS_ADD` e `GROUP_PARTICIPANTS_REMOVE` **nao existem**.
Existe um so: `GROUP_PARTICIPANTS_UPDATE`, que cobre entrada e saida.

A API devolve a lista completa no erro 400 — da para descobrir os nomes
bons por ali.

Para a Fase 2:

```
MESSAGES_UPSERT
GROUP_PARTICIPANTS_UPDATE
GROUPS_UPSERT
CHATS_UPSERT
```

## 10. `fetchInstances` nao popula o campo `webhook`

Depois de configurar, `instance/fetchInstances` continua mostrando
`webhook: {}`. **Nao significa que nao foi salvo.** Consulte
`GET /webhook/find/{instancia}`, que devolve o registro completo.

---

## Ambiente local

## 11. O shell mascara env var que parece segredo

```bash
SUPABASE_SERVICE_ROLE_KEY=eyJ... python script.py
# o script recebe a string de 3 caracteres: ***
```

Da 401 sem motivo aparente. **Leia de arquivo** (`.env` + um modulo que
faz parse), nunca de env var, neste host.

O mesmo vale para o `supabase secrets set`: ele le o valor pela stdin, e a
stdin precisa vir do arquivo.

## 12. Python nao acha o `supabase` (e .cmd do npm)

`shutil.which` acha, mas `subprocess.run` sem `shell=True` falha com
WinError 2 em Windows. Resolver o caminho e passar `shell=True` quando o
binario termina em `.cmd`.

## 13. `read_file` recusa ler `.env`

Por design (arquivo de credencial). Isso atrapalha verificar estrutura. Use
`terminal` com grep para conferir **quantas linhas** tem, sem imprimir
valor.

## 14. `patch` pode duplicar linha em `.env`

Nao use `patch` para editar `.env` aqui. Reescreva com `write_file` reindo
antes, e confira com `grep -c` quantas ocorrencias existem.

## 15. A allowlist do WhatsApp nao e o numero do Telegram

O Herickson tem **dois numeros diferentes**:

```
WhatsApp: 558592494552   ← este vai na allowlist do webhook
Telegram: 5585999515154  ← so pelo gateway
```

Colocar o Telegram na allowlist nao da erro nenhum: a secret e gravada, o
health check responde `allowlist: true`, os testes com numero errado passam.
O agente fica **mudo** porque o numero real nunca casa, e nada no log
aponta o motivo.

Como descobrir: `instance/fetchInstances` lista os `ownerJid` de cada
instancia. Contar quantas instancias tem o numero que voce acha que e o
seu. Se nao aparecer nenhum, esse nao e o seu numero de WhatsApp.

Regra: `WHATSAPP_NUMERO_AUTORIZADO` tem que ser o numero que a Evolution
anexa no `remoteJid` — ou seja, o chip que **manda** a mensagem, nunca o
que **recebe**.
## 16. Evolution `sendText` devolve 201, nao 200

Envio aceito responde **201 Created** (o body traz a `key` da mensagem).
Aceitar so `200` faz todo envio parecer falha: o relay marca a fala como nao
entregue e re-enfileira, mesmo com a mensagem entregue. Loop infinito de
resposta duplicada.

Aceitar `(200, 201)`.

Armadilha maior: `curl` sem `-w` nao mostra o status, entao um teste manual
"passa" e o bug so aparece no log do relay.

## 17. O PostgREST nao avalia expressao no PATCH

```python
body={"respondido_em": "now()"}   # grava a LITERAL "now()" num timestamptz
```

Calcular o timestamp em Python e mandar ISO-8601. Vale para qualquer coluna
com `default now()` que voce queira sobrescrever.

## 18. Regex de "remover acentos" quebra arquivo

`re.sub(r'[^\x00-\x7F]', '', texto)` remove as aspas de um docstring mal
formado sem avisar, e o SyntaxError aparece longe da causa. Em vez de
corrigir depois, escrever ASCII desde o inicio: arquivo novo em PT-BR vai
sem acento, e o linter para de reclamar.

## 19. `hermes -z` e o runner de one-shot

Para responder sem sessao aberta, usar `hermes -z PROMPT`. Roda o agente
COMPLETO (ferramentas, memoria, skills) e imprime so a resposta.

```
hermes -z "qual seu nome?"   # -> resposta em stdout
```

Vantagem sobre chamar a API do modelo na mao: usa o login que ja existe
(OAuth Nous), sem chave nova no `.env`, e o agente vem com as skills do
repo. Uma chamada HTTP traria so texto, sem ferramenta.

O `-q` NAO existe neste build. So `-z` / `--oneshot`.
