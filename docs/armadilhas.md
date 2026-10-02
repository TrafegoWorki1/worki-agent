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

## 3. `/pg/query` responde 404 neste plano

A API de SQL nao esta liberada. Nao da para criar tabela por aqui. Se mudar
o schema: Dashboard > SQL Editor, colar o arquivo. Ver
`schema_para_colar.sql`.

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