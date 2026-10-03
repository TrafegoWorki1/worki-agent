"""Cliente da Evolution API: envio e reconciliacao.

Tres estados de envio, nao dois. A distincao importa:

- ENTREGUE  — a Evolution aceitou e temos o id. Pode reenviar sem medo se
  falhar depois.
- FALHOU   — recusou com erro claro (4xx/5xx com corpo). Nao saiu nada.
  Reenviar faz sentido.
- INCERTO  — timeout ou queda de rede **depois** do envio. A mensagem
  pode ter saído. Reenviar manda a mesma resposta duas vezes para o
  Herickson. Por isso vira `incerto` e espera o reconciliador.

Nao prometemos exactly-once numa API externa que nao suporta. O que
fazemos e ser honestos sobre o que nao sabemos.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integracoes.config import Config, digitos  # noqa: E402
from integracoes.evolution import formato  # noqa: E402

# Pausa entre as partes de uma resposta dividida: garante a ordem de chegada
# e evita rajada na Evolution.
PAUSA_ENTRE_PARTES_S = 0.6

_CFG = None


def cfg() -> Config:
    """Config em cache. Ler a cada envio reparseia o ambiente a toa."""
    global _CFG
    if _CFG is None:
        _CFG = Config.carregar()
    return _CFG


class Estado:
    ENTREGUE = "entregue"
    FALHOU = "falhou"
    INCERTO = "incerto"


def _headers() -> dict:
    c = cfg()
    h = {"apikey": c.EVOLUTION_API_KEY, "Content-Type": "application/json"}
    if c.EVOLUTION_WEBHOOK_TOKEN:
        # A versao nova aceita token por instancia em header separado.
        h["Authorization"] = f"Bearer {c.EVOLUTION_WEBHOOK_TOKEN}"
    return h


def _limite() -> int:
    """Caracteres por mensagem (WORKI_WHATSAPP_MAX_CHARS); 0 desliga a divisao."""
    return getattr(cfg(), "WORKI_WHATSAPP_MAX_CHARS", formato.LIMITE_PADRAO)


def enviar_texto(numero: str, texto: str, timeout: int = 45) -> tuple[str, str | None]:
    """Formata, divide se for longo e envia. Devolve (estado, provider_message_id).

    `numero` so com digitos, sem @s.whatsapp.net. Em grupo, o receptor ja
    resolveu o destinatario antes de chegar aqui.

    O texto passa por `formato.preparar`: Markdown de terminal vira o dialeto
    do WhatsApp e respostas longas viram varias mensagens, quebradas em limite
    de paragrafo. O id devolvido e o da primeira parte.

    Falha no meio de uma resposta dividida: se a PRIMEIRA parte nao saiu,
    devolve o estado dela (nada foi enviado, pode reenviar tudo). Se a
    primeira saiu e uma seguinte falhou, devolve ENTREGUE e registra o aviso:
    reenviar tudo repetiria o comeco para o usuario, e esta camada prefere
    perder o final a duplicar (AGENTS.md: nao reenviar as cegas).
    """
    destino = str(numero or "") if str(numero or "").endswith("@g.us") else digitos(numero)
    partes = formato.preparar(texto, _limite())
    if not partes:
        return Estado.FALHOU, None
    if len(partes) == 1:
        return _enviar_uma(destino, partes[0], timeout)

    primeiro_id = None
    for i, parte in enumerate(partes):
        estado, mid = _enviar_uma(destino, parte, timeout)
        if i == 0:
            primeiro_id = mid
        if estado != Estado.ENTREGUE:
            if i == 0:
                return estado, None
            print(f"  AVISO: parte {i + 1}/{len(partes)} nao saiu ({estado}); "
                  f"as anteriores ja foram entregues", file=sys.stderr)
            return Estado.ENTREGUE, primeiro_id
        if i < len(partes) - 1:
            time.sleep(PAUSA_ENTRE_PARTES_S)
    return Estado.ENTREGUE, primeiro_id


def _enviar_uma(destino: str, texto: str, timeout: int = 45) -> tuple[str, str | None]:
    """Uma chamada a Evolution, uma mensagem. Os tres estados valem para ela."""
    c = cfg()
    url = f"{c.EVOLUTION_API_URL}/message/sendText/{c.EVOLUTION_INSTANCE}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"number": destino, "text": texto}).encode(),
        headers=_headers(), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # A Evolution devolve 201 Created em envio aceito. Aceitar so
            # 200 faz todo envio parecer falha e a resposta some.
            if r.status not in (200, 201):
                return Estado.FALHOU, None
            bruto = r.read().decode()
            try:
                dados = json.loads(bruto) if bruto else {}
            except json.JSONDecodeError:
                # Aceitou mas resposta nao parseia: entregamos, sem id.
                return Estado.ENTREGUE, None
            return Estado.ENTREGUE, _extrair_id(dados)
    except urllib.error.HTTPError as e:
        # Erro HTTP com resposta: a Evolution recusou. Nao saiu nada.
        try:
            corpo = e.read().decode()[:200]
        except Exception:
            corpo = ""
        print(f"  Evolution recusou: HTTP {e.code} {corpo}", file=sys.stderr)
        return Estado.FALHOU, None
    except (TimeoutError, urllib.error.URLError) as e:
        # A requisicao pode ter saido antes da rede cair. Nao sabemos.
        print(f"  Evolution erro de rede: {e}", file=sys.stderr)
        return Estado.INCERTO, None


def _extrair_id(dados: dict) -> str | None:
    """O id vem em chaves diferentes conforme a versao da Evolution."""
    for chave in ("key", "messageId", "message_id", "id"):
        v = dados.get(chave)
        if isinstance(v, dict):
            v = v.get("id")
        if v:
            return str(v)
    return None


def listar_mensagens(limite: int = 20) -> list[dict]:
    """Mensagens recentes da instancia. Usado pelo reconciliador.

    Se uma saida esta `incerto`, procuramos aqui o texto para decidir se
    saiu. matching por texto + janela de tempo, nao por id: o id do
    provedor nao foi guardado nesse caminho.
    """
    c = cfg()
    url = (f"{c.EVOLUTION_API_URL}/chat/fetchMessages/{c.EVOLUTION_INSTANCE}"
           f"?limit={limite}")
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            bruto = r.read().decode()
            dados = json.loads(bruto) if bruto else []
            return dados if isinstance(dados, list) else dados.get("messages", [])
    except Exception as e:
        print(f"  fetchMessages falhou: {e}", file=sys.stderr)
        return []


def reconciliar(saida: dict) -> str:
    """Decide o que fazer com uma saida `incerto`.

    Devolve 'entregue' se o texto aparece na conversa (saiu), 'falhou' se
    nao aparece depois de uma janela razoavel (nao saiu, pode reenviar).

    Criterio conservador: exigir **duas** mensagens com o mesmo texto, ou
    o mesmo texto com o prefixo da Evolution. Um match unico pode ser
    eco. Errar para o lado de "nao reenviar" e melhor: uma resposta a
    mais incomoda, uma pergunta sem resposta trava o trabalho.
    """
    original = (saida.get("texto") or "").strip()
    if not original:
        return "falhou"
    # Compara com o que foi de fato enviado: a primeira parte do texto ja
    # formatado. Sem isso, "**negrito**" virou "*negrito*" no WhatsApp, o
    # prefixo nao bate e o reconciliador concluiria que nao saiu e reenviaria.
    partes = formato.preparar(original, _limite())
    texto = partes[0] if partes else original

    mensagens = listar_mensagens()
    if not mensagens:
        # Nao da para confirmar. Mantem incerto e tenta de novo depois —
        # nunca reenviar as cegas.
        return "incerto"

    normalizado = texto[:80]
    achados = 0
    for m in mensagens:
        if not isinstance(m, dict):
            continue
        msg = m.get("message") if isinstance(m.get("message"), dict) else {}
        corpo = (msg.get("conversation")
                 or (msg.get("extendedTextMessage") or {}).get("text")
                 or "")
        chave = m.get("key") if isinstance(m.get("key"), dict) else {}
        # Uma mensagem nossa conta: e a propria resposta voltando.
        # Confirma que saiu, sem depender do texto completo bater.
        if chave.get("fromMe"):
            if corpo and normalizado[:40] in corpo:
                achados += 1
        elif corpo and normalizado[:40] in corpo:
            achados += 1

    if achados >= 1:
        return "entregue"
    return "falhou"


def enviar_reconciliando(numero: str, texto: str, saida: dict) -> tuple[str, str | None]:
    """Envia, e se ficar incerto tenta reconciliar antes de desistir."""
    estado, mid = enviar_texto(numero, texto)
    if estado != Estado.INCERTO:
        return estado, mid
    # Incerto: pergunta a Evolution se saiu, em vez de reenviar.
    try:
        veredito = reconciliar(saida)
        if veredito == "entregue":
            return Estado.ENTREGUE, saida.get("provider_message_id")
        if veredito == "falhou":
            return Estado.FALHOU, None
    except Exception as e:
        print(f"  reconciliacao falhou: {e}", file=sys.stderr)
    return Estado.INCERTO, None
