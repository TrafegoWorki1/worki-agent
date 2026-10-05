#!/bin/sh
# Grava os tokens do agente em ARQUIVO (chmod 600) e instala os wrappers.
#
# POR QUE: o Hermes remove GITHUB_TOKEN, GH_TOKEN e VERCEL_TOKEN do ambiente de
# todo comando que o agente roda (tools/environments/local_env_policy.py:
# _ALWAYS_STRIP_KEYS e a lista de provedores), e a config
# `terminal.env_passthrough` NAO consegue liberar esses nomes. O container tem
# as variaveis, o worker tem, mas `gh` e `vercel` rodando pelo agente nao
# enxergam. Resultado: "nao tenho token", mesmo com tudo configurado.
#
# O que se faz aqui, a cada boot, a partir do ambiente do container (que este
# script ainda enxerga):
#   - grava o token em $CRED/<nome> (so o dono le);
#   - instala em $BIN os wrappers `gh`, `vercel` e o ajudante de credencial do
#     git, que leem o arquivo e passam o token so ao processo que vao executar.
#
# Uso: preparar_credenciais.sh <dir-dos-wrappers-no-repo> [CRED] [BIN]
# Token ausente no ambiente = arquivo removido: o estado fica explicito.
set -eu

ORIGEM="${1:?uso: preparar_credenciais.sh <dir-dos-wrappers> [CRED] [BIN]}"
CRED="${2:-${WORKI_CREDENCIAIS_DIR:-/opt/data/.credenciais}}"
BIN="${3:-${WORKI_BIN_DIR:-/opt/data/bin}}"

umask 077
mkdir -p "$CRED" "$BIN"
chmod 700 "$CRED"

grava() {  # grava <nome-do-arquivo> <valor>
    if [ -n "$2" ]; then
        printf '%s' "$2" > "$CRED/$1"
        chmod 600 "$CRED/$1"
        echo "[credenciais] $1: presente"
    else
        rm -f "$CRED/$1"
        echo "[credenciais] $1: ausente (o agente fica sem esse acesso)"
    fi
}

grava github_token "${GITHUB_TOKEN:-${GH_TOKEN:-}}"
grava vercel_token "${VERCEL_TOKEN:-}"

for f in gh vercel git-credential-worki; do
    cp -f "$ORIGEM/$f" "$BIN/$f"
    chmod 755 "$BIN/$f"
done
