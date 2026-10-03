"""Triagem rapida de mensagens, antes da fila.

Hoje: uma regra fechada que reconhece perguntas de andamento ("terminou?",
"cade?", "e ai?"). Ela existe porque o worker processa UM pedido por vez e a
reserva trava a conversa enquanto ha um pedido em 'processando': sem este
atalho, "terminou?" so chegava ao Hermes quando a tarefa ja tinha acabado, e o
dono ficava minutos sem resposta (medido em 2026-10-03: esperas de 338 s e
796 s em "Cade ?" e "Oii ?").

Este e o lugar previsto para um modelo de decisao (ex.: o Jev, da TypeSafe AI):
`e_pergunta_de_andamento` pode passar a consultar o modelo para as frases
ambiguas que a regra nao pega. O contrato e o mesmo: devolve bool, e na duvida
devolve False, o que mantem o caminho normal (fila -> Hermes).
"""

import re
import unicodedata
from datetime import datetime, timezone

# Mensagem inteira, ja normalizada (minusculas, sem acento, sem pontuacao).
# Regra conservadora: so frases curtas que, sozinhas, sao cobranca de
# andamento. "terminou a pagina? agora cria outra" NAO casa (e um pedido novo).
_ANDAMENTO = re.compile(
    r"^(e )?("
    r"ai|entao|agora|"
    r"cade|cade ai|"
    r"terminou|ja terminou|acabou|ja acabou|ja foi|foi|deu certo|conseguiu|"
    r"ta pronto|esta pronto|ficou pronto|pronto|"
    r"como esta|como ta|como vai|como anda|"
    r"andamento|status|novidade|novidades|alguma novidade|"
    r"demora|vai demorar|ainda nao|"
    r"ta ai|esta ai|ta trabalhando|esta trabalhando"
    r")( ai| isso| ja| entao| ainda)?$"
)
_TAMANHO_MAXIMO = 30


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    so_letras = re.sub(r"[^a-z0-9 ]+", " ", sem_acento.lower())
    return re.sub(r"\s+", " ", so_letras).strip()


def e_pergunta_de_andamento(texto: str) -> bool:
    """True se a mensagem, sozinha, e uma cobranca de andamento."""
    norm = _normalizar(texto)
    if not norm or len(norm) > _TAMANHO_MAXIMO:
        return False
    return bool(_ANDAMENTO.match(norm))


def _minutos_desde(iso: str | None, agora: datetime) -> int | None:
    if not iso:
        return None
    try:
        inicio = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=timezone.utc)
    return max(0, int((agora - inicio).total_seconds() // 60))


def mensagem_andamento(tarefa: dict, agora: datetime | None = None) -> str:
    """Resposta curta com o que o banco sabe. Nao inventa progresso.

    Durante a execucao o Hermes e uma caixa-preta: o banco so sabe qual foi o
    pedido e quando comecou. A resposta diz exatamente isso.
    """
    agora = agora or datetime.now(timezone.utc)
    objetivo = " ".join(str(tarefa.get("objetivo") or "").split())
    if len(objetivo) > 80:
        objetivo = objetivo[:77].rstrip() + "..."
    minutos = _minutos_desde(tarefa.get("iniciada_em"), agora)

    linhas = ["Ainda estou trabalhando nisso."]
    if objetivo:
        linhas.append(f"Pedido: \"{objetivo}\"")
    if minutos is not None:
        linhas.append("Comecei agora ha pouco." if minutos < 1
                      else f"Comecei ha {minutos} min.")
    linhas.append("Te respondo assim que terminar.")
    return "\n".join(linhas)
