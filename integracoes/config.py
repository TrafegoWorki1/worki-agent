"""Configuracao do worki-agent: le do ambiente primeiro.

Por que existe: o `env.py` antigo (o do relay) so sobrepoe nomes que ja
vieram do `.env`. No EasyPanel nao existe `.env` — as variaveis sao do
servico. Com o codigo antigo, `chave('SUPABASE_URL')` levantaria
SystemExit num container corretamente configurado, porque o arquivo nao
esta la.

Regra: **ambiente ganha sempre.** O `.env` e opcional e serve so para
desenvolvimento local.

Nenhum valor e impresso. `mostrar()` mascara tudo.
"""

import os
import sys
from pathlib import Path

# Este arquivo mora em <raiz>/integracoes/config.py, entao a raiz do repo
# e parents[1]. Usar parents[2] apontaria para o diretorio acima do repo
# e o .env local nunca seria encontrado — falha silenciosa em dev.
RAIZ = Path(__file__).resolve().parents[1]


def _do_arquivo() -> dict:
    """Le o .env local, se existir. Opcional — o container nao tem."""
    dados: dict[str, str] = {}
    caminho = RAIZ / ".env"
    if not caminho.exists():
        return dados
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        dados[k.strip()] = v.strip().strip("'\"")
    return dados


# Arquivo primeiro, ambiente por cima. Ordem importa.
_CFG = {**_do_arquivo(), **{k: v for k, v in os.environ.items()
                            if k.isupper()}}


def chave(nome: str, padrao: str | None = None, obrigatorio: bool = True) -> str:
    """Valor de configuracao.

    `obrigatorio=False` devolve `padrao` em vez de levantar — para o caso
    de feature opcional (grupo, repositorio de pagina) que o servico roda
    sem.
    """
    v = _CFG.get(nome, "").strip()
    if v == "***":
        v = ""
    if v:
        return v
    if obrigatorio:
        raise SystemExit(
            f"Falta {nome} no ambiente.\n"
            f"No EasyPanel: variaveis do servico. Localmente: copie "
            f".env.example para .env.\n"
            f"(procurado em .env e no ambiente; nenhum dos dois tem {nome})"
        )
    return padrao or ""


def inteiro(nome: str, padrao: int, obrigatorio: bool = False) -> int:
    bruto = chave(nome, obrigatorio=obrigatorio)
    if not bruto:
        return padrao
    try:
        return int(bruto)
    except ValueError:
        raise SystemExit(f"{nome} tem que ser inteiro, veio: {bruto!r}")


def lista(nome: str, padrao: str = "") -> list[str]:
    """Lista separada por virgula, sem vazio. Para allowlist de grupo."""
    bruto = chave(nome, obrigatorio=False)
    if not bruto:
        bruto = padrao
    return [p.strip() for p in bruto.split(",") if p.strip()]


def mascarar(v: str, n: int = 8) -> str:
    """Mostra so o inicio e o fim. Regra do AGENTS.md secao 5."""
    if not v:
        return "(vazio)"
    if len(v) <= n * 2:
        return "***"
    return f"{v[:n]}...{v[-4:]}"


def digitos(valor) -> str:
    """So numeros. JID do WhatsApp vem com sufixo (@s.whatsapp.net, @g.us)."""
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


class Config:
    """Snapshot da configuracao. Ler tudo no boot, nao no meio da tarefa."""

    # --- Evolution ---
    EVOLUTION_API_URL: str = ""
    EVOLUTION_API_KEY: str = ""
    EVOLUTION_INSTANCE: str = ""
    EVOLUTION_WEBHOOK_TOKEN: str = ""

    # --- Supabase ---
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # --- Allowlist ---
    WHATSAPP_NUMERO_AUTORIZADO: str = ""
    WHATSAPP_GRUPO_COMANDO_JIDS: list[str] = []
    WHATSAPP_AGENTE_NUMERO: str = ""

    # --- Servico ---
    PORT: int = 8080
    WORKI_WEBHOOK_SECRET: str = ""
    WORKI_WORKER_CONCURRENCY: int = 1
    WORKI_WORKER_LEASE_S: int = 120
    WORKI_RECOVERY_POLL_SECONDS: int = 3
    WORKI_TASK_TIMEOUT_SECONDS: int = 1500
    # 0 = desligado. >0: se o Hermes ainda nao respondeu depois desse numero
    # de segundos, o worker manda um "recebi, estou trabalhando" no WhatsApp.
    WORKI_ACK_AFTER_SECONDS: int = 0
    # Caracteres por mensagem no WhatsApp; respostas maiores sao divididas em
    # limite de paragrafo. 0 = nao dividir.
    WORKI_WHATSAPP_MAX_CHARS: int = 1500
    WORKI_WORKSPACE_DIR: str = "/workspace"
    WORKI_HERMES_HOME: str = "/opt/data"
    WORKI_OWNER_ID: str = "herickson"
    WORKI_DRY_RUN: bool = False

    @classmethod
    def carregar(cls) -> "Config":  # noqa: UP037
        c = cls()
        c.EVOLUTION_API_URL = chave("EVOLUTION_API_URL").rstrip("/")
        c.EVOLUTION_API_KEY = chave("EVOLUTION_API_KEY")
        # Grafia 'agent-domintante' (sem o "i") e a real do deploy.
        # NAO corrigir. Ver docs/contrato-rpcs.md secao 1.
        c.EVOLUTION_INSTANCE = chave("EVOLUTION_INSTANCE", "agent-domintante",
                                     obrigatorio=False)
        c.EVOLUTION_WEBHOOK_TOKEN = chave("EVOLUTION_WEBHOOK_TOKEN",
                                          obrigatorio=False)

        c.SUPABASE_URL = chave("SUPABASE_URL").rstrip("/")
        c.SUPABASE_SERVICE_ROLE_KEY = chave("SUPABASE_SERVICE_ROLE_KEY")

        c.WHATSAPP_NUMERO_AUTORIZADO = digitos(
            chave("WHATSAPP_NUMERO_AUTORIZADO"))
        # Plural, padronizado. O .env real tem o singular
        # (WHATSAPP_GRUPO_COMANDO_JID) e a Edge Function usava plural —
        # divergencia que o blueprint apontou. Aceito os dois nomes para
        # nao quebrar o .env existente, mas o canonico e o plural.
        grupos = lista("WHATSAPP_GRUPO_COMANDO_JIDS")
        if not grupos:
            grupos = lista("WHATSAPP_GRUPO_COMANDO_JID")
        c.WHATSAPP_GRUPO_COMANDO_JIDS = grupos
        c.WHATSAPP_AGENTE_NUMERO = digitos(
            chave("WHATSAPP_AGENTE_NUMERO", obrigatorio=False))

        c.PORT = inteiro("PORT", 8080)
        c.WORKI_WEBHOOK_SECRET = chave("WORKI_WEBHOOK_SECRET",
                                       obrigatorio=False)
        c.WORKI_WORKER_CONCURRENCY = inteiro("WORKI_WORKER_CONCURRENCY", 1)
        c.WORKI_WORKER_LEASE_S = inteiro("WORKI_WORKER_LEASE_S", 120)
        c.WORKI_RECOVERY_POLL_SECONDS = inteiro(
            "WORKI_RECOVERY_POLL_SECONDS", 3)
        c.WORKI_TASK_TIMEOUT_SECONDS = inteiro(
            "WORKI_TASK_TIMEOUT_SECONDS", 1500)
        c.WORKI_ACK_AFTER_SECONDS = inteiro("WORKI_ACK_AFTER_SECONDS", 0)
        c.WORKI_WHATSAPP_MAX_CHARS = inteiro("WORKI_WHATSAPP_MAX_CHARS", 1500)
        c.WORKI_WORKSPACE_DIR = chave("WORKI_WORKSPACE_DIR", "/workspace",
                                      obrigatorio=False)
        c.WORKI_HERMES_HOME = chave("WORKI_HERMES_HOME", "/opt/data",
                                    obrigatorio=False)
        c.WORKI_OWNER_ID = chave("WORKI_OWNER_ID", "herickson",
                                 obrigatorio=False)
        c.WORKI_DRY_RUN = chave("WORKI_DRY_RUN", obrigatorio=False).lower() in (
            "1", "true", "yes")
        return c

    @classmethod
    def mostrar(cls) -> dict:
        """Estado da config, com segredos mascarados. Vai no /health."""
        c = cls.carregar()
        return {
            "evolution": {
                "url": c.EVOLUTION_API_URL,
                "instancia": c.EVOLUTION_INSTANCE,
                "webhook_token": bool(c.EVOLUTION_WEBHOOK_TOKEN),
            },
            "supabase": {"url": c.SUPABASE_URL, "service_role": bool(
                c.SUPABASE_SERVICE_ROLE_KEY)},
            "allowlist": {
                # Show so o fim: o numero inteiro e PII.
                "numero": f"...{c.WHATSAPP_NUMERO_AUTORIZADO[-4:]}"
                          if c.WHATSAPP_NUMERO_AUTORIZADO else None,
                "grupos": len(c.WHATSAPP_GRUPO_COMANDO_JIDS),
                # Grupo negado por padrao. Lista vazia = nenhum grupo.
                "grupo_permitido": bool(c.WHATSAPP_GRUPO_COMANDO_JIDS),
            },
            "servico": {
                "porta": c.PORT,
                "concurrency": c.WORKI_WORKER_CONCURRENCY,
                "lease_s": c.WORKI_WORKER_LEASE_S,
                "task_timeout_s": c.WORKI_TASK_TIMEOUT_SECONDS,
                "ack_after_s": c.WORKI_ACK_AFTER_SECONDS,
                "workspace": c.WORKI_WORKSPACE_DIR,
                "hermes_home": c.WORKI_HERMES_HOME,
                "dry_run": c.WORKI_DRY_RUN,
            },
        }


CFG = Config

if __name__ == "__main__":
    import json
    try:
        print(json.dumps(Config.mostrar(), indent=2, ensure_ascii=False))
    except SystemExit as e:
        print(f"CONFIG INCOMPLETA\n{e}", file=sys.stderr)
        sys.exit(1)
