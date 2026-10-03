# Ligar o Hermes completo (mantendo a Evolution)

Este documento descreve a mudanca que faz o container subir o Hermes
**completo** (skills, config, ferramentas) em vez de pela metade, sem trocar
o canal: a Evolution continua entregando as mensagens, o receptor e o worker
continuam no ar.

Leia antes de dar deploy. A parte de Dockerfile/entrypoint **nao foi testada
em build/deploy reais** nesta maquina (sem Docker aqui). A parte de banco e de
Python **foi testada** (ver secao "O que foi testado").

---

## O problema

O `Dockerfile` sobrescrevia o `ENTRYPOINT` da imagem oficial do Hermes pelo
`worki-entrypoint`. Com isso o `/init` do s6-overlay nunca rodava, e o
`stage2-hook` da imagem (que faz remap de UID, chown dos volumes, seed da
config e **sincronizacao das skills**) nunca executava. O Hermes subia sem
skills sincronizadas e sem config seedada. O comentario antigo no Dockerfile
dizia que "o s6 nativo continua cuidando do gateway" — nao continuava.

Confirmado na imagem oficial:
- `ENTRYPOINT` oficial: `/opt/hermes/docker/entrypoint-dispatch.sh`, que quando
  e PID 1 roda `exec /init /opt/hermes/docker/main-wrapper.sh`.
- `stage2-hook.sh` roda como cont-init e chama `tools/skills_sync.py`.
- A imagem termina em `USER root` e rebaixa o privilegio sozinha (o
  `main-wrapper` roda o CMD ja como `hermes`).

## A correcao

No `Dockerfile`:
1. Removido `USER hermes`. O container volta a comecar como root, para o
   stage2 ter privilegio de preparar os volumes e sincronizar skills.
2. Removido o override `ENTRYPOINT ["/usr/local/bin/worki-entrypoint"]`.
   Herdamos o ENTRYPOINT oficial.
3. `CMD ["/usr/local/bin/worki-entrypoint"]`. O receptor e o worker passam a
   rodar como "programa principal", DEPOIS do stage2 e ja como `hermes`.

No `entrypoint.sh`:
- A cada boot, o `AGENTS.md` e copiado de `/app/AGENTS.md` (sempre atual, vem
  do build) para `/workspace/AGENTS.md`. O `/workspace` e um volume
  persistente, entao o `COPY` da imagem so vale na primeira criacao; sem o
  refresh, as regras ficariam congeladas numa versao antiga.
- Corrigido o aviso errado de que o Hermes precisa de repo git para ler o
  `AGENTS.md`. Ele le do cwd mesmo sem git (confirmado em
  `agent/prompt_builder.py::_agents_md_directory_chain`).

## Passos manuais que ainda faltam

Estes nao dao para automatizar com seguranca daqui e precisam da sua acao:

### 1. Fixar a imagem por digest

O `Dockerfile` ainda usa `ARG HERMES_IMAGE=nousresearch/hermes-agent:latest`.
`latest` muda sem aviso. Depois de validar o build, fixe o digest:

```
docker manifest inspect nousresearch/hermes-agent:latest | grep -m1 digest
# use o valor:
#   ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:<digest>
```

### 2. Ligar as aprovacoes de verdade

A secao 3 do `AGENTS.md` lista as palavras (`aprova`, `sobe`, `confirma`), mas
isso e prompt, nao trava. A trava real do Hermes e config. No volume
`/opt/data/config.yaml` (HERMES_HOME), configure:

```yaml
approvals:
  mode: manual          # pede aprovacao em comando perigoso
  unattended_mode: deny # webhook/worker nao tem humano: nega em vez de travar
```

O modulo `integracoes/aprovacao/guard.py` (aprovacao ligada ao alvo e ao hash
do artefato) continua **nao conectado** a execucao. Liga-lo e trabalho
separado.

### 3. Seguranca do webhook antigo do Supabase

Fora do escopo desta mudanca, mas pendente: a Edge Function
`evolution-webhook` aceita sem conferir segredo. Ver a analise inicial.

---

## O que foi testado

Banco (Postgres 16 local, migracoes reais aplicadas):
- Reproduzido o bug: apos uma entrada `falhou`, a conversa travava (a proxima
  mensagem nunca era reservada), o `session_id` virava `erro` e o log tecnico
  ia como texto para o WhatsApp.
- Com a migracao `20261003190000_falha_nao_trava_conversa.sql`: a conversa
  destrava, a sessao e preservada e a mensagem seguinte e processada.
- A suite SQL do repo (`supabase/tests/runtime_portugues.sql`) passa com a
  migracao aplicada (ordem e um-por-vez preservados).

Python (`pytest`, 15 testes, todos passam), incluindo
`testes/test_concluir_falha.py` que fixa: sessao nao vira `erro`, log nao vai
ao WhatsApp, mensagem neutra no lugar.

## O que NAO foi testado (precisa de build + deploy)

- O container subir com o ENTRYPOINT oficial + CMD do worki.
- As skills de fato sincronizarem em `/opt/data/skills`.
- O `/ready` responder 200 com worker e banco de pe.
- O HEALTHCHECK (`python -c ...`) continuar resolvendo `python` no PATH.

---

## Teste de fumaca (fazer no deploy, antes de considerar pronto)

Depois do build e deploy no EasyPanel:

```
# 1. container de pe e receptor respondendo
curl -fsS https://<host-do-worki>/health

# 2. dependencias prontas (worker + banco)
curl -fsS https://<host-do-worki>/ready      # espera 200 e {"ok":true}

# 3. skills sincronizadas dentro do container
docker exec <container> ls -1 /opt/data/skills | head

# 4. AGENTS.md atual no workspace
docker exec <container> head -5 /workspace/AGENTS.md

# 5. o Hermes enxerga as skills (roda como hermes)
docker exec <container> hermes skills list 2>/dev/null | head

# 6. ponta a ponta: mande "ping" no WhatsApp do numero autorizado -> "pong"
#    depois uma pergunta real e confira a resposta.
```

Criterio de pronto (AGENTS.md secao 13): so considere concluido quando 1 a 6
passarem. Leitura de codigo nao conta.

## Rollback

Se o container nao subir, reverta o Dockerfile para o `ENTRYPOINT` anterior:

```
# volta ao comportamento antigo (Hermes pela metade, mas sobe)
USER hermes
ENTRYPOINT ["/usr/local/bin/worki-entrypoint"]
```

A migracao de banco e a correcao de Python sao independentes e seguras: podem
ficar mesmo que o rollback do Dockerfile aconteca. A migracao usa
`create or replace` e nao destroi dados.
