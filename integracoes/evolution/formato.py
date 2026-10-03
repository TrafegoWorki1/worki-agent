"""Formatacao de texto para o WhatsApp: converter e dividir.

Duas funcoes puras, sem rede, que o cliente da Evolution aplica antes de
enviar. O modelo escreve em Markdown de terminal; o WhatsApp tem outro
dialeto e um teto de leitura confortavel:

- `para_whatsapp`: `**negrito**` vira `*negrito*`, `# Titulo` vira `*Titulo*`,
  marcadores `- ` e `* ` viram `• `, `[texto](url)` vira `texto (url)`.
  Blocos de codigo (```) ficam intactos, porque o WhatsApp os renderiza.
- `dividir`: quebra uma resposta longa em varias mensagens, SEMPRE em limite
  de paragrafo (depois de linha, frase ou espaco, so se nao houver jeito),
  em vez de uma parede de texto cortada pelo "Ler mais".
- `preparar`: as duas em sequencia. E o que o cliente e o reconciliador usam,
  para os dois enxergarem exatamente o mesmo texto.

Nao inventa conteudo, nao resume e nao remove palavras: so muda a forma.
"""

import re

# Limite padrao de caracteres por mensagem. O WhatsApp aceita bem mais, mas
# acima disso o app dobra a mensagem atras de "Ler mais".
LIMITE_PADRAO = 1500

_CERCA = re.compile(r"(```.*?```)", re.DOTALL)
_NEGRITO = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_TITULO = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_MARCADOR = re.compile(r"^([ \t]*)[-*+][ \t]+(?=\S)", re.MULTILINE)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_REGUA = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_VARIAS_VAZIAS = re.compile(r"\n{3,}")
# Ideogramas CJK, hiragana e katakana. Num texto em portugues, aparecer um
# deles no meio de uma frase e sinal de mistura de idiomas do modelo.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿＀-￯]")


def _converter_trecho(trecho: str) -> str:
    trecho = _NEGRITO.sub(lambda m: f"*{m.group(2)}*", trecho)
    trecho = _TITULO.sub(lambda m: f"*{m.group(1).strip()}*", trecho)
    trecho = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", trecho)
    trecho = _REGUA.sub("", trecho)
    trecho = _MARCADOR.sub(lambda m: f"{m.group(1)}• ", trecho)
    return trecho


def para_whatsapp(texto: str) -> str:
    """Converte Markdown de terminal para o dialeto do WhatsApp."""
    if not texto:
        return ""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    # Fora dos blocos de codigo: partes pares da divisao; dentro: impares.
    partes = _CERCA.split(texto)
    for i in range(0, len(partes), 2):
        partes[i] = _converter_trecho(partes[i])
    texto = "".join(partes)
    texto = "\n".join(linha.rstrip() for linha in texto.split("\n"))
    return _VARIAS_VAZIAS.sub("\n\n", texto).strip()


def _quebrar_paragrafo(par: str, limite: int) -> list[str]:
    """Quebra um paragrafo maior que o limite: linha, depois frase, depois espaco."""
    pedacos: list[str] = []
    atual = ""
    for linha in par.split("\n"):
        candidato = f"{atual}\n{linha}" if atual else linha
        if len(candidato) <= limite:
            atual = candidato
            continue
        if atual:
            pedacos.append(atual)
            atual = ""
        while len(linha) > limite:
            corte = max(linha.rfind(". ", 0, limite), linha.rfind("? ", 0, limite),
                        linha.rfind("! ", 0, limite))
            corte = corte + 1 if corte > limite // 3 else linha.rfind(" ", 0, limite)
            if corte <= 0:
                corte = limite
            pedacos.append(linha[:corte].rstrip())
            linha = linha[corte:].lstrip()
        atual = linha
    if atual:
        pedacos.append(atual)
    return pedacos


def dividir(texto: str, limite: int = LIMITE_PADRAO) -> list[str]:
    """Divide em mensagens de ate `limite` caracteres, em limite de paragrafo.

    `limite <= 0` desliga a divisao (devolve o texto inteiro). Um bloco de
    codigo (```) e tratado como um paragrafo: so e partido se sozinho passar
    do limite.
    """
    texto = (texto or "").strip()
    if not texto:
        return []
    if limite <= 0 or len(texto) <= limite:
        return [texto]

    # Paragrafos sao separados por linha em branco, exceto dentro de ```.
    blocos: list[str] = []
    for i, parte in enumerate(_CERCA.split(texto)):
        if i % 2 == 1:
            blocos.append(parte)
        else:
            blocos.extend(p for p in re.split(r"\n\s*\n", parte) if p.strip())

    mensagens: list[str] = []
    atual = ""
    for bloco in blocos:
        bloco = bloco.strip("\n")
        for pedaco in ([bloco] if len(bloco) <= limite else _quebrar_paragrafo(bloco, limite)):
            candidato = f"{atual}\n\n{pedaco}" if atual else pedaco
            if len(candidato) <= limite:
                atual = candidato
            else:
                if atual:
                    mensagens.append(atual)
                atual = pedaco
    if atual:
        mensagens.append(atual)
    return mensagens


def preparar(texto: str, limite: int = LIMITE_PADRAO) -> list[str]:
    """Converte e divide. Lista vazia se nao ha o que enviar."""
    return dividir(para_whatsapp(texto), limite)


def tem_caracteres_cjk(texto: str) -> bool:
    """True se ha ideogramas/kana no texto: mistura de idiomas do modelo."""
    return bool(_CJK.search(texto or ""))
