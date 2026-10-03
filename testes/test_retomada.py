"""Regressao do loop de retomada.

Em 2026-10-03 a sessao 20261003_131416_57f90a chegou a 66 mensagens,
quase todas um cabecalho repetido, e o agente passou a responder
"ja li a sessao X duas vezes — ela so repete esse cabecalho".

Causa: montar_contexto() injetava no prompt

    "Voce esta retomando a sessao X desta conversa. O contexto
     anterior esta nela — nao comece do zero."

e o adapter chamava `hermes chat --resume X` — a MESMA sessao que
estava sendo escrita. Cada mensagem gravava mais uma copia do aviso
dentro do contexto que ela mesma manda reler.

`--resume` ja devolve o historico. O aviso era redundante e se
acumulava sem limite.

Este teste trava o comportamento.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.worker.relay import Relay  # noqa: E402


class DBFalso:
    """Banco minimo: so o que montar_contexto() toca."""

    def __init__(self, tarefas):
        self._tarefas = tarefas

    def sessao_da_conversa(self, _):
        return "20261003_131416_57f90a"

    def tarefas_ativas(self, _):
        return self._tarefas


class Entrada:
    texto = "oi"
    session_id = "20261003_131416_57f90a"
    conversa_id = "72a022a5"


def contexto(tarefas):
    relay = Relay.__new__(Relay)          # sem __init__: nao ha config
    relay.db = DBFalso(tarefas)
    return relay.montar_contexto(Entrada())


def principal():
    falhas = []

    # 1. O aviso de retomada nao pode mais aparecer no prompt.
    c = contexto([])
    if "retomando a sessao" in c.lower():
        falhas.append("o aviso de retomada voltou para o prompt")
    if "20261003_131416_57f90a" in c:
        falhas.append("o id da sessao esta sendo anunciado no prompt")

    # 2. Tarefa bloqueada sem checkpoint nao e trabalho a retomar.
    bloqueadas = [{
        "id": "d99d918f", "objetivo": "Ola",
        "status": "bloqueada",
        "proxima_acao": "Revisar execucao antes de retomar",
        "resultado": "hermes chat -q\n",
        "checkpoint": None,
    }]
    c2 = contexto(bloqueadas)
    if "Tarefas em andamento" in c2:
        falhas.append("tarefa bloqueada sem checkpoint foi anunciada como trabalho")

    # 3. Tarefa real continua sendo anunciada, com o que fazer agora.
    reais = [{
        "id": "abc", "objetivo": "publicar a landing",
        "status": "ativa",
        "proxima_acao": "aguardar deploy",
        "checkpoint": "branch criada, falta o deploy",
    }]
    c3 = contexto(reais)
    if "Tarefas em andamento" not in c3:
        falhas.append("tarefa real sumiu do contexto")
    if "aguardar deploy" not in c3:
        falhas.append("a proxima acao sumiu do contexto")
    if "branch criada" not in c3:
        falhas.append("o checkpoint sumiu do contexto")

    # 4. A instrucao de tom e idioma permanece.
    if "pt-BR" not in c3:
        falhas.append("a instrucao de idioma sumiu")

    # 5. Repetir o contexto nao pode gerar crescimento de tamanho.
    if len(c) != len(contexto([])):
        falhas.append("o contexto cresce a cada chamada")

    if falhas:
        print("FALHOU:")
        for f in falhas:
            print(f"  - {f}")
        return 1

    print("Passou (sem aviso de retomada, filtro de tarefas, contexto estavel)")
    return 0


if __name__ == "__main__":
    sys.exit(principal())