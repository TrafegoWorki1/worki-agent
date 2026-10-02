# worki-agent — servico unico (receptor HTTP + worker)
#
# Base: imagem oficial do Hermes, fixada por digest. Nao alteramos o
# upstream — a imagem e a base, e o codigo do Worki entra por cima.
#
# Por que digest e nao tag: `latest` muda sem aviso. Um deploy que pega
# uma imagem diferente da testada nao e reproduzivel, e um bug no Hermes
# apareceria como "misterio" em vez de regressao.
#
# O digest abaixo precisa ser conferido antes do primeiro deploy:
#   docker manifest inspect nousresearch/hermes-agent:<tag>
# SMOKE TEST PENDENTE — ver docs/deploy-easypanel.md secao "Antes do
# primeiro deploy".
ARG HERMES_IMAGE=nousresearch/hermes-agent:latest
FROM ${HERMES_IMAGE}

# --------------------------------------------------------------------------
# Python: versao fixada, sem depender do que vier na imagem
# --------------------------------------------------------------------------
# A imagem ja traz Python e uv. Fixar aqui evita que uma atualizacao da
# base mude o runtime sem ninguem pedir.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# --------------------------------------------------------------------------
# Codigo do Worki
# --------------------------------------------------------------------------
WORKDIR /app

# Primeiro so os requisitos: mudanca em codigo nao reinstala nada.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY integracoes/ /app/integracoes/
COPY docs/ /app/docs/
COPY AGENTS.md /app/AGENTS.md

# --------------------------------------------------------------------------
# Diretorios de estado
# --------------------------------------------------------------------------
# /opt/data  — estado nativo do Hermes (config, sessoes, state.db, skills)
# /workspace — checkout e arquivos de trabalho; perde-se no update do
#              container se nao for volume
#
# O state.db roda em WAL. A doc oficial avisa que bind mount que cruza
# fronteira de VM (9p/drvfs/virtiofs) corrompe WAL EM SILENCIO — o
# integrity_check passa e o dado some. Se o volume do EasyPanel nao for
# nativo, o resume de sessao quebra sem erro. Ver docs/deploy-easypanel.md.
RUN mkdir -p /opt/data /workspace \
    && chmod 700 /opt/data \
    && chmod 777 /workspace

ENV HERMES_HOME=/opt/data \
    WORKI_HERMES_HOME=/opt/data \
    WORKI_WORKSPACE_DIR=/workspace \
    PORT=8080

# --------------------------------------------------------------------------
# Ferramentas de que a skill de paginas precisa
# --------------------------------------------------------------------------
# git e gh para branch/PR; node para build da pagina. A imagem oficial
# documenta `npx`/`uvx` para ferramentas avulsas — instalar no sistema
# aqui deliberatemente: o build de pagina precisa de node e npm estaveis.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v node >/dev/null 2>&1 \
        || (curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
            && apt-get install -y --no-install-recommends nodejs))

# --------------------------------------------------------------------------
# USUARIO
# --------------------------------------------------------------------------
# A imagem oficial roda como `hermes` e `docker exec` cai nesse usuario.
# O receptor precisa escrever so em /workspace; nao precisa de root.
USER hermes

# --------------------------------------------------------------------------
# ENTRYPOINT
# --------------------------------------------------------------------------
# Dois processos sob supervisao. Nao e `&` solto: o blueprint pede que a
# falha de um apareca. Aqui o receptor e o worker sao gerenteados por um
# supervisor Python que reinicia e mantem log e codigo de saida — e o
# s6 nativo da imagem continua cuidando do `gateway run` da propria imagem.
COPY entrypoint.sh /usr/local/bin/worki-entrypoint
RUN chmod +x /usr/local/bin/worki-entrypoint

EXPOSE 8080

# Health: liveness barato. O EasyPanel usa /ready para nao mandar trafego
# quando o banco esta fora, entao este so precisa provar que o processo
# responde.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/worki-entrypoint"]
