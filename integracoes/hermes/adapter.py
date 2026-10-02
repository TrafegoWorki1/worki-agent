"""Adaptador do Hermes: sessao por conversa, execucao e resposta limpa.

O relay antigo chamava `hermes -z <texto>` e recebia stdout. Isso funciona
para uma mensagem solta e quebra para conversa: cada chamada abre uma sessao
nova, entao a segunda mensagem da mesma conversa nao sabe da primeira.

Este modulo liga a sessao a uma conversa pelo ID do state.db.

## Como o session_id e obtido (e por que nao "latest")

`--pass-session-id` NAO resolve. Ele coloca o ID no system prompt do agente
(texto que o modelo le), nao no stdout — nao ha o que parsear.

`--resume latest` tambem nao serve: e global, e duas workers em paralelo
terminariam na mesma sessao.

O caminho nativo e `hermes chat -q ... -Q`, que imprime duas coisas em
canais separados (verificado nesta maquina):

    stdout:  a resposta final, e nada mais
    stderr:  "session_id: 20261002_193158_1fea5d"

Como os canais sao separados, nao ha banner nem log no texto que vai para o
WhatsApp, e o ID sai exato — sem heuristica.

Atencao: `-z` (oneshot global) e `chat -q` NAO aceitam `--source` igual. Em
`-z` a opcao e desconhecida e o comando falha. E `chat -q` que aceita
`--source`, entao e esse que o adaptador usa.

Fica um fallback por state.db para quando o Hermes nao emitir o ID: snapshot
de `max(started_at)` antes do run, depois localizar a sessao criada depois.
O caminho principal e o stderr; o fallback cobre versao que mude o formato.

Testado nesta maquina: sessao retomada com `--resume` respondeu "SESSAO"
(lembrou do pedido anterior) enquanto um run novo respondeu "NOVA".

## Por que `hermes -z` e nao uma chamada na API do modelo

O relay nao substitui o Hermes por HTTP. `-z` usa o login que ja existe e
vem com as ferramentas e skills: uma chamada HTTP traria so texto, sem
terminal, sem git, sem Vercel. E o AGENTS.md deste repo e lido pelo agente,
entao o contexto e o mesmo dos dois canais.
"""

import os
import re
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# Sessao do state.db e um recurso compartilhado: duas threads abrindo
# escrita ao mesmo tempo em WAL produzem "database is locked". O lock e
# por processo e nao cobre outros processos — para isso existe a lease.
_LOCK = threading.Lock()

# O banner e a linha de "restored workspace dir" vao para stderr, mas
# nao confie: em versao futura podem ir para stdout e acabar no WhatsApp.
_RUIDO = re.compile(
    r"^(↪|↻|│|╭|╰|─|\s*$)", re.MULTILINE
)


def hermes_home() -> Path:
    """Raiz de dados do Hermes. Perfil e respeitado via $HERMES_HOME."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def state_db() -> Path:
    return hermes_home() / "state.db"


@dataclass
class Resultado:
    """Resposta do Hermes, separada dos logs.

    Separar as duas coisas e o que impede mandar log com banner para o
    WhatsApp: stdout cru vai para `texto`, o resto para `log`.
    """

    ok: bool
    texto: str | None
    session_id: str | None
    log: str = ""
    codigo: int | None = None
    cancelado: bool = False


# O Hermes emite "session_id: 20261002_193158_1fea5d" no stderr ao final
# de um run. O formato do ID e YYYYMMDD_HHMMSS_<hex>, documentado na doc
# de sessoes. Aceitar so esse formato evita casar com um ID de PR, de
# deployment ou qualquer outro numero que apareca no log.
_SID = re.compile(r"^session_id:\s*(\d{8}_\d{6}_[0-9a-f]{6,8})\s*$", re.MULTILINE)


def _id_do_stderr(stderr: str) -> str | None:
    """Extrai o session_id que o Hermes reportou. None se nao houver."""
    if not stderr:
        return None
    achado = _SID.search(stderr)
    return achado.group(1) if achado else None


def _snapshot_inicio() -> tuple[float, set[str]]:
    """Marca temporal e ids existentes, para saber o que e novo."""
    try:
        with _LOCK, sqlite3.connect(f"file:{state_db()}?mode=ro", uri=True) as cx:
            linha = cx.execute(
                "select coalesce(max(started_at), 0) from sessions"
            ).fetchone()
            ids = {r[0] for r in cx.execute("select id from sessions")}
        return float(linha[0] or 0), ids
    except sqlite3.Error as e:
        # Sem state.db legivel o Hermes ainda roda; so perde-mos o
        # vinculo de sessao. Nao e motivo para recusar a tarefa.
        return 0.0, set()


def _sessao_nova(apos: float, antes_ids: set[str]) -> str | None:
    """Sessao criada depois do snapshot e que nao existia antes."""
    try:
        with _LOCK, sqlite3.connect(f"file:{state_db()}?mode=ro", uri=True) as cx:
            linhas = cx.execute(
                "select id, started_at from sessions "
                "where id not in (%s) and started_at >= ? "
                "order by started_at desc limit 5"
                % ",".join("?" * len(antes_ids) or ["''"]),
                (*antes_ids, apos),
            ).fetchall()
        for sid, started in linhas:
            if sid not in antes_ids:
                return sid
    except sqlite3.Error:
        return None
    return None


def _limpar(texto: str) -> str:
    """Tira o que e ruido de terminal, nao conteudo do agente."""
    if not texto:
        return ""
    # O one-shot imprime a resposta final crua; qualquer linha de
    # progresso vem com marcador. Remove so essas, nunca uma palavra
    # do meio de um paragrafo.
    linhas = [ln for ln in texto.splitlines() if not _RUIDO.match(ln)]
    return "\n".join(linhas).strip()


class Adaptador:
    """Executa o Hermes e devolve sessao + resposta, sempre separados.

    Uso tipico:

        ad = Adaptador(timeout=1800)
        r = ad.responder(texto, session_id=conversa.hermes_session_id)
        if r.ok:
            conversa.hermes_session_id = r.session_id
    """

    def __init__(
        self,
        binario: str = "hermes",
        timeout: int = 1800,
        cwd: str | Path | None = None,
        fonte: bool = True,
    ):
        self.binario = binario
        self.timeout = timeout
        self.cwd = str(cwd) if cwd else None
        # source=oneshot esconde a sessao dos seletores da TUI/Desktop.
        # Nao queremos o WhatsApp do Herickson entulhando a lista de
        # sessoes que ele ve no terminal.
        self.source = "oneshot"
        self.fonte = fonte

    def _comando(self, prompt: str, session_id: str | None) -> list[str]:
        # `chat -q`, nao `-z`: e o subcomando que aceita --source. Com
        # `-z` a opcao e rejeitada e o Hermes nao roda.
        # -Q tira banner e spinner, deixando stdout so com a resposta.
        cmd = [self.binario, "chat", "-q", prompt, "-Q", "--source", self.source]
        if session_id:
            # Retomar: o ID precisa existir no state.db deste perfil.
            cmd += ["--resume", session_id]
        if self.cwd:
            # --in tambem prende a sessao ao diretorio, entao um resume
            # nao faz cd para o cwd antigo gravado na sessao.
            cmd += ["--in", self.cwd]
        return cmd

    def responder(
        self,
        prompt: str,
        session_id: str | None = None,
        contexto: str = "",
    ) -> Resultado:
        """Roda o Hermes. `session_id` None abre sessao nova.

        `contexto` (opcional) entra no prompt como material de apoio —
        briefing, decisao anterior, estado da tarefa. Vai separado do
        pedido para o agente saber o que e instrucao e o que e dado.
        """
        if not prompt or not prompt.strip():
            return Resultado(ok=False, texto=None, session_id=session_id,
                             log="prompt vazio")

        texto_prompt = prompt
        if contexto:
            texto_prompt = f"{contexto.rstrip()}\n\n---\n\n{prompt}"

        antes, antes_ids = _snapshot_inicio()
        cmd = self._comando(texto_prompt, session_id)
        log_cmd = (
            f"{self.binario} chat -q [--resume {session_id}]"
            if session_id else f"{self.binario} chat -q"
        )

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            # Nao marca a sessao como conclusion: o proximo run cria outra
            # e a conversa continua. O que nao pode e dizer "pronto".
            return Resultado(
                ok=False, texto=None, session_id=session_id,
                log=f"timeout apos {self.timeout}s", cancelado=True,
            )
        except FileNotFoundError:
            return Resultado(
                ok=False, texto=None, session_id=session_id,
                log=f"binario {self.binario} nao encontrado no PATH",
            )

        stdout = _limpar(proc.stdout or "")
        stderr = (proc.stderr or "").strip()
        log = f"{log_cmd}\n{stderr[:1000]}"

        # Caminho principal: o proprio Hermes reporta o id no stderr.
        # Fallback: state.db, para versao que mude o formato.
        novo = _id_do_stderr(stderr) or _sessao_nova(antes, antes_ids) or session_id

        if proc.returncode != 0:
            return Resultado(
                ok=False, texto=None,
                # Mesmo em erro a sessao existe: preservar evita que a
                # proxima mensagem abra outra e perca o contexto.
                session_id=novo,
                log=log, codigo=proc.returncode,
            )

        if not stdout:
            # Sem resposta e sem erro: o agente nao falou. Nao inventar
            # texto e mandar para o WhatsApp.
            return Resultado(
                ok=False, texto=None, session_id=novo,
                log=log + "\nstdout vazio",
            )

        return Resultado(ok=True, texto=stdout, session_id=novo, log=log)


if __name__ == "__main__":
    # Teste manual:  python adapter.py "pergunta" [session_id]
    p = sys.argv[1] if len(sys.argv) > 1 else "Responda APENAS: OK"
    s = sys.argv[2] if len(sys.argv) > 2 else None
    r = Adaptador(timeout=120).responder(p, session_id=s)
    print(f"ok={r.ok} session={r.session_id}")
    print(f"texto={r.texto!r}")
    if r.log:
        print(f"log={r.log[:300]!r}")
