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
# Dependencias Python
# --------------------------------------------------------------------------
# A imagem base traz Python em /opt/hermes/.venv, criado com uv — e uv NAO
# instala pip nesse venv. Rodar `python -m pip` falha com:
#   /opt/hermes/.venv/bin/python: No module named pip
#
# E o requirements.txt esta vazio de proposito: receptor e worker usam
# apenas a biblioteca padrao. Nao ha nada a instalar.
#
# Para entrar no requisito daqui, use `uv pip install` ou `uv add`, nunca
# `python -m pip`.

WORKDIR /app

COPY integracoes/ /app/integracoes/
COPY docs/ /app/docs/
COPY AGENTS.md /app/AGENTS.md
# O Hermes le AGENTS.md do DIRETORIO DE TRABAALHO (cwd), nao do /app.
# Sem copiar para ca, o agente roda sem identidade, sem allowlist e
# sem as prohibicoes de venda: em 2026-10-03 /workspace estava vazio e
# o agente respondeu sem nenhuma regra do repo.
COPY AGENTS.md /workspace/AGENTS.md
COPY --chmod=755 entrypoint.sh /usr/local/bin/worki-entrypoint

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
    && chmod 777 /workspace \
    && chown -R hermes:hermes /opt/data /workspace /app /usr/local/bin/worki-entrypoint

ENV HERMES_HOME=/opt/data \
    WORKI_HERMES_HOME=/opt/data \
    WORKI_WORKSPACE_DIR=/workspace \
    PORT=8080

# --------------------------------------------------------------------------
# Ferramentas de pagina
# --------------------------------------------------------------------------
# git/node sao necessarios so pela skill de paginas, que builda uma landing.
# NAO sao requisito do servico: receptor e worker usam apenas a biblioteca
# padrao do Python (ver requirements.txt — hoje esta vazio de proposito).
#
# Este bloco saiu do caminho de build de proposito. Ele trazia duas
# dependencias de rede frágeis para o build do servico:
#   - apt-get com repositorio externo
#   - curl do nodesource, falho por rate limit e por DNS
# Build de servico nao pode depender de CDN de terceiro. A skill de paginas
# usa `uvx`/`npx` no container em tempo de execucao, que e o caminho que a
# propria imagem do Hermes documenta para ferramentas avulsas.
#
# Se a skill de paginas precisar de node estavel, instala em runtime com
# `uvx` ou no entrypoint, e nao no build.
#
# --------------------------------------------------------------------------
# USUARIO
# --------------------------------------------------------------------------
# A imagem oficial roda como `hermes` e `docker exec` cai nesse usuario.
# O receptor precisa escrever so em /workspace; nao precisa de root.
# --------------------------------------------------------------------------
# CLI de terceiros — o agente precisa, nao so as chaves
# --------------------------------------------------------------------------
# GITHUB_TOKEN e VERCEL_TOKEN no ambiente nao bastam: sem os binarios o
# agente responde "sem CLI do Firebase, sem CLI do Supabase" e nao faz
# nada. Em 2026-10-03 era exatamente esse o estado: os tokens estavam
# ausentes e o `gh`/`vercel` tambem. As duas metades precisam vir juntas.
#
# gh: repositorio publico por padrao, entao o token precisa de escopo de
# repo. Vercel: token da conta, escopo minimo de producao.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    npm install -g @vercel/cli; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*; \
    gh --version; \
    vercel --version

USER hermes

# --------------------------------------------------------------------------
# ENTRYPOINT
# --------------------------------------------------------------------------
# Dois processos sob supervisao. Nao e `&` solto: o blueprint pede que a
# falha de um apareca. Aqui o receptor e o worker sao gerenteados por um
# supervisor Python que reinicia e mantem log e codigo de saida — e o
# s6 nativo da imagem continua cuidando do `gateway run` da propria imagem.
EXPOSE 8080

# Health: liveness barato. O EasyPanel usa /ready para nao mandar trafego
# quando o banco esta fora, entao este so precisa provar que o processo
# responde.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/worki-entrypoint"]
