#!/bin/sh
# Entrypoint do worki-agent: receptor HTTP + worker, sob supervisao.
#
# Por que nao `cmd &` solto: com dois processos em background e um CMD
# esperando, se o worker morre o container continua "de pe" e o EasyPanel
# nao reinicia nada. O sintoma e o agente ficar mudo sem erro — que e
# exatamente o que o AGENTS.md s.2 descreve como falha silenciosa.
#
# Este supervisor:
#   - sobe receptor e worker
#   - reinicia qualquer um que morrer
#   - propaga SIGTERM no stop do Docker (shutdown gracioso)
#   - sai com codigo != 0 se o receptor morrer, para o EasyPanel reiniciar
#
# A imagem oficial ja supervisiona o `gateway run` dela via s6-overlay;
# este entrypoint cuida so dos dois processos do Worki.

set -eu

log() { echo "[entrypoint] $*"; }


# --- segredos de terceiros em arquivo no volume ---
#
# GITHUB_TOKEN e VERCEL_TOKEN nao podem viver no env do servico. Em
# 2026-10-03, com eles cadastrados via `docker service update`, o proximo
# deploy sobrescreveu e os dois sumiram do swarm sem erro: a CLI nao autentica
# e o agente responde "sem CLI do Supabase" sem explicar o motivo.
#
# A API publica do EasyPanel expoe MCP e deploy, nao edicao de env. E editar
# o LMDB de /etc/easypanel/data com o painel segurando o lock e arriscado.
#
# Entao os tokens ficam em arquivo no volume /opt/data, que o EasyPanel nao
# reescreve: este arquivo sobrevive a todo deploy. O volume ja guarda a
# autenticacao do Hermes (auth.json) pelo mesmo motivo.
#
# Formato: KEY=VALOR, uma por linha, mesmo formato de .env. Ausente ou vazio
# = o agente simplesmente nao tem acesso those servicos; o boot nao falha.
[ -f /opt/data/env.secrets ] && {
    log "carregando segredos de terceiros de /opt/data/env.secrets"
    set -a
    # shellcheck disable=SC1091
    . /opt/data/env.secrets
    set +a
}

LOG_LEVEL="${LOG_LEVEL:-INFO}"
export LOG_LEVEL


# `python3` e o nome em que a imagem base do Hermes nao tem: o venv que ela
# cria em /opt/hermes/.venv expoe `python`, e o PATH da imagem pode nao ter
# nenhum `python3`. Com `PYTHON:-python3` o boot falha em
# "python3: not found" e o container nunca sobe — que e o que a revisao
# do build mostrava como 502 no proxy.
#
# A ordem abaixo e deliberada:
#   1. PYTHON explicito na configuracao do serviço (EasyPanel), se houver
#   2. /opt/hermes/.venv/bin/python — o Python da propria imagem
#   3. qualquer `python` no PATH
#   4. `python3` por ultimo
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for candidato in /opt/hermes/.venv/bin/python python python3; do
        if command -v "$candidato" >/dev/null 2>&1; then
            PY="$candidato"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "[entrypoint] CONFIGURACAO INVALIDA: nenhum interpretador Python encontrado" >&2
    exit 78
fi
log "python: $PY ($("$PY" --version 2>&1))"

APP="${WORKI_APP_DIR:-/app}"

# --- validacao de configuracao antes de subir qualquer coisa ---
# Falhar aqui, no boot, e melhor do que subir e falhar em cada request.
if ! "$PY" -c "from integracoes.config import Config; Config.carregar()" 2>/tmp/cfg.err; then
    log "CONFIGURACAO INVALIDA:"
    cat /tmp/cfg.err >&2
    log "O container nao sobe. Veja docs/deploy-easypanel.md."
    exit 78   # EX_CONFIG
fi
if [ -z "${WORKI_WEBHOOK_SECRET:-}" ] || [ "${WORKI_WEBHOOK_SECRET:-}" = "***" ]; then
    log "CONFIGURACAO INVALIDA: WORKI_WEBHOOK_SECRET e obrigatorio"
    exit 78
fi
log "configuracao ok"

# --- volumes ---
if [ ! -d /opt/data ]; then
    log "AVISO: /opt/data nao existe. As sessoes nao vao persistir."
fi
mkdir -p "$APP" 2>/dev/null || true

# --- diretorio de trabalho do agente ---
# O Hermes espera um git repo no cwd para o AGENTS.md ser lido. Sem isso
# ele roda sem as regras do repo.
if [ ! -d /workspace/.git ]; then
    log "/workspace nao e um repo git. Copie o codigo do agente para la"
    log "ou monte um volume com o repo. O AGENTS.md so e lido em repo."
fi

RECEPTOR_PID=""
WORKER_PID=""
SHUTTING_DOWN=0

shutdown() {
    SHUTTING_DOWN=1
    log "SIGTERM recebido — encerrando com migracao"
    # O worker trata SIGTERM parando de pegar trabalho novo e devolvendo
    # o que estava segurando. Dar tempo para isso.
    [ -n "$WORKER_PID" ] && kill -TERM "$WORKER_PID" 2>/dev/null || true
    [ -n "$RECEPTOR_PID" ] && kill -TERM "$RECEPTOR_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    log "encerrado"
    exit 0
}
trap shutdown TERM INT

# --- receptor ---
cd "$APP"
"$PY" -m integracoes.evolution.webhook &
RECEPTOR_PID=$!
log "receptor no ar (pid $RECEPTOR_PID)"

# Pequena espera: se a config ou o import falhar, o worker nem precisa
# tentar subir.
sleep 2
if ! kill -0 "$RECEPTOR_PID" 2>/dev/null; then
    log "o receptor morreu na subida — erro acima"
    exit 1
fi

# --- worker ---
# Nao usar `-m integracoes.worker.relay`: com -m o arquivo roda sob o nome
# `__main__`, que e um objeto de modulo DIFERENTE de `integracoes.worker.relay`.
# O heartbeat vive em `_ULTIMO_TIC`, global do modulo. O receptor importa pelo
# nome canonico e veria o valor congelado em 0.0, reportando worker morto
# para sempre — foi o que travou o /ready em 503 mesmo com o worker no ar.
# Importando e chamando main(), o modulo e registrado com o nome certo.
"$PY" -c "from integracoes.worker import relay; relay.main()" &
WORKER_PID=$!
log "worker no ar (pid $WORKER_PID)"

# --- loop de supervisao ---
# Enquanto os dois viverem, so espera. Se um cair, reinicia. Se cair
# vezes demais seguidas, desiste e deixa o EasyPanel mandar SIGTERM —
# melhor reinicio limpo do container do que loop infinito de crash.
RECEPTOR_FALHAS=0
WORKER_FALHAS=0
MAX_FALHAS=5

while [ "$SHUTTING_DOWN" -eq 0 ]; do
    sleep 2

    if ! kill -0 "$RECEPTOR_PID" 2>/dev/null; then
        wait "$RECEPTOR_PID" 2>/dev/null || true
        RECEPTOR_FALHAS=$((RECEPTOR_FALHAS + 1))
        log "receptor morreu (falha $RECEPTOR_FALHAS/$MAX_FALHAS)"
        if [ "$RECEPTOR_FALHAS" -ge "$MAX_FALHAS" ]; then
            log "receptor instavel demais — saindo para o orquestrador reiniciar"
            kill -TERM "$WORKER_PID" 2>/dev/null || true
            exit 1
        fi
        "$PY" -m integracoes.evolution.webhook &
        RECEPTOR_PID=$!
        log "receptor reiniciado (pid $RECEPTOR_PID)"
    else
        RECEPTOR_FALHAS=0
    fi

    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        wait "$WORKER_PID" 2>/dev/null || true
        WORKER_FALHAS=$((WORKER_FALHAS + 1))
        log "worker morreu (falha $WORKER_FALHAS/$MAX_FALHAS)"
        if [ "$WORKER_FALHAS" -ge "$MAX_FALHAS" ]; then
            log "worker instavel demais — saindo para o orquestrador reiniciar"
            kill -TERM "$RECEPTOR_PID" 2>/dev/null || true
            exit 1
        fi
        # Nao usar `-m integracoes.worker.relay`: com -m o arquivo roda sob o nome
# `__main__`, que e um objeto de modulo DIFERENTE de `integracoes.worker.relay`.
# O heartbeat vive em `_ULTIMO_TIC`, global do modulo. O receptor importa pelo
# nome canonico e veria o valor congelado em 0.0, reportando worker morto
# para sempre — foi o que travou o /ready em 503 mesmo com o worker no ar.
# Importando e chamando main(), o modulo e registrado com o nome certo.
"$PY" -c "from integracoes.worker import relay; relay.main()" &
        WORKER_PID=$!
        log "worker reiniciado (pid $WORKER_PID)"
    else
        WORKER_FALHAS=0
    fi
done
