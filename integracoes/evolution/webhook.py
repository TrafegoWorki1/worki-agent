"""Receptor HTTP dos eventos da Evolution.

Substitui a Edge Function `evolution-webhook`. Mesmas responsabilidades,
com as correcoes que o blueprint apontou.

## As tres correcoes que importam

**1. Grupo: `participant` e o remetente.**

O codigo antigo fazia:

    const de = digitos(msg.from ?? remoteJid);   // index.ts:34

Em grupo, `remoteJid` e o JID do **grupo** — o mesmo para todo mundo. O
fallback transformava o numero do grupo na identidade do autor, e a
comparacao com a allowlist passava/falhava pelo numero errado.

Aqui: `participant` (com fallback para `participantPn`/`participantLid`,
que existem na versao nova do Baileys) e quem manda. `remoteJid` e so o
destino da conversa. Se nao der para resolver a identidade, **nao
executa** — silencio e o comportamento correto (AGENTS.md s.2).

**2. Grupo negado por padrao.**

O codigo antigo tinha `GRUPOS.length > 0 && !GRUPOS.includes(grupo)`:
lista vazia deixava **passar** qualquer grupo. Como a configuracao atual
tem zero grupos, qualquer grupo nao autorizado era aceito.

Aqui: lista vazia significa **nenhum grupo permitido**. Negado e o
padrao; permissao e que precisa ser declarada.

**3. Nao confirma sem persistir.**

O codigo antigo respondia 200 sempre, mesmo com o banco fora. A Evolution
interpretava como "entregue" e o evento se perdia para sempre, sem log de
erro.

Aqui: 2xx so depois do commit. Falha de persistencia devolve 5xx para a
Evolution repetir. Duplicata e 200 sem trabalho novo.

## Resposta: rapida e sem esperar a tarefa

O receptor grava e responde. Nao chama o Hermes e nao espera a tarefa
terminar — quem faz isso e o worker, que ve a fila. Uma tarefa longa nao
pode segurar a conexao da Evolution.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integracoes.config import Config, digitos  # noqa: E402

log = logging.getLogger("worki.receptor")

# A Evolution manda MESSAGES_UPSERT. Nome pode chegar com o prefixo da
# instancia: "evolution_api.MESSAGES_UPSERT".
TIPOS_ACEITOS = ("MESSAGES_UPSERT",)


@dataclass
class Evento:
    """Evento normalizado da Evolution."""

    provider_message_id: str
    instancia: str
    chat_jid: str
    remetente_jid: str
    remetente_numeros: str
    texto: str
    tipo_mensagem: str | None = None
    grupo: str | None = None
    de_mim: bool = False
    participants: list[str] = field(default_factory=list)

    @property
    def em_grupo(self) -> bool:
        return self.chat_jid.endswith("@g.us")


def _dict(valor) -> dict:
    """Coage para dict. Payload de webhook e JSON sem schema: qualquer
    campo pode ser null, lista ou string onde se espera objeto."""
    return valor if isinstance(valor, dict) else {}


def _primeiro(msg: dict, *caminhos):
    """Primeiro valor nao-nulo num caminho aninhado: msg['a']['b']['c']."""
    for c in caminhos:
        atual = msg
        for parte in c.split("."):
            atual = _dict(atual).get(parte)
            if atual is None:
                break
        if atual:
            return atual
    return None


def normalizar(evento: dict, instancia_padrao: str) -> Evento | None:
    """Converte o payload bruto da Evolution em Evento.

    Devolve None quando o evento nao serve (tipo errado, sem conteudo,
    sem id). O receptor responde 200 nesses casos: nao ha o que processar
    e repetir nao muda nada.
    """
    dados = _dict(evento.get("data"))
    msg = _dict(dados.get("message"))
    key = _dict(dados.get("key"))

    # --- tipo do evento ---
    nome = (evento.get("event") or evento.get("eventType") or "").split(".")[-1]
    if nome and nome not in TIPOS_ACEITOS:
        return None

    # --- mensagem que eu enviei ---
    if key.get("fromMe"):
        return None

    remote_jid = str(_primeiro(dados, "remoteJid") or key.get("remoteJid") or "")
    if not remote_jid:
        return None

    em_grupo = remote_jid.endswith("@g.us")
    data_part = dados.get("participant")

    # --- remetente ---
    # Em grupo, quem manda e `participant`. `remoteJid` e o grupo, entao
    # usa-lo como identidade faria o numero do grupo virar o autor.
    #
    # O `participant` aparece em `data` em algumas versoes e dentro de
    # `data.key` em outras. Os dois precisam ser lidos: se so um for
    # lido, a lista de autorizacao nunca casa e o agente fica mudo.
    #
    # ParticipantPn/ParticipantLid sao as variacoes da versao nova: o
    # mesmo numero aparece em dois formatos, e comparar so com um deles
    # deixa mensagem legitima fora da allowlist.
    remetente = str(
        data_part
        or key.get("participant")
        or msg.get("participantPn")
        or key.get("participantPn")
        or msg.get("participantLid")
        or key.get("participantLid")
        or dados.get("participantPn")
        or dados.get("participantLid")
        or ""
    )
    if not remetente:
        remetente = str(msg.get("from") or ("" if em_grupo else remote_jid))

    todos_part = []
    for fonte in (msg, key, dados):
        for chave_p in ("participant", "participantPn", "participantLid"):
            v = fonte.get(chave_p)
            if v and str(v) not in todos_part:
                todos_part.append(str(v))

    # --- conteudo ---
    texto = ""
    tipo = None
    ext = _dict(msg.get("extendedTextMessage"))
    img = _dict(msg.get("imageMessage"))
    btn = _dict(msg.get("buttonResponseMessage"))
    if isinstance(msg.get("conversation"), str):
        texto, tipo = msg["conversation"], "conversation"
    elif isinstance(ext.get("text"), str):
        texto, tipo = ext["text"], "extendedTextMessage"
    elif isinstance(img.get("caption"), str):
        texto, tipo = img["caption"], "imageMessage"
    elif btn.get("selectedButtonId"):
        texto, tipo = str(btn["selectedButtonId"]), "buttonResponseMessage"

    # Audio NAO entra. Transcricao e um passo explicito e nao esta
    # resolvido nesta versao. Aceitar e enfileirar vazio produziria job
    # sem conteudo.
    if not texto.strip():
        return None

    mid = str(key.get("id") or "")
    if not mid:
        # Sem id nao da para deduplicar. Melhor recusar do que gravar
        # duas vezes o mesmo evento.
        return None

    return Evento(
        provider_message_id=mid,
        instancia=str(dados.get("instance") or evento.get("instance")
                      or instancia_padrao),
        chat_jid=remote_jid,
        remetente_jid=remetente,
        remetente_numeros=digitos(remetente),
        texto=texto.strip(),
        tipo_mensagem=tipo,
        grupo=remote_jid if em_grupo else None,
        participants=todos_part,
    )


def checar_autorizacao(ev: Evento, cfg) -> tuple[bool, str]:
    """Allowlist. Devolve (autorizado, motivo).

    Ordem de decisao: instancia, grupo, numero.

    - Instancia errada: e outra conexao do WhatsApp, nao e o Herickson.
    - Grupo: precisa estar na lista, e a lista vazia nega.
    - Numero: so `WHATSAPP_NUMERO_AUTORIZADO`.
    """
    if ev.instancia != cfg.EVOLUTION_INSTANCE:
        return False, "instancia_diferente"

    if ev.em_grupo:
        if not cfg.WHATSAPP_GRUPO_COMANDO_JIDS:
            # Sem lista configurada, nenhum grupo e autorizado.
            return False, "grupo_nao_configurado"
        if ev.chat_jid not in cfg.WHATSAPP_GRUPO_COMANDO_JIDS:
            return False, "grupo_nao_autorizado"
        # Em grupo o autor e o participant, nunca o remoteJid.

    if not cfg.WHATSAPP_NUMERO_AUTORIZADO:
        return False, "allowlist_vazia"

    if ev.remetente_numeros != cfg.WHATSAPP_NUMERO_AUTORIZADO:
        return False, "numero_nao_autorizado"

    return True, "autorizado"


class Receptor:
    """Guarda o estado e expoe as rotas. Sem estado global solto."""

    def __init__(self, cfg, gravar=None):
        self.cfg = cfg
        # `gravar(evento) -> (entrada_id, ja_existia)`. Injetavel para
        # testar sem banco.
        self.gravar = gravar or self._gravar_real
        self.auditado = 0
        self.autorizados = 0
        self.duplicados = 0
        self.recusados = 0
        self.falhas = 0

    def _gravar_real(self, ev: Evento):
        """Chama a RPC que grava mensagem + entrada numa transacao.

        Importado aqui (e nao no topo) porque a RPC so existe depois que
        o Codex aplicar o contrato. ate la, o receptor sobe e responde,
        mas nao persiste — e o /health mostra isso.
        """
        from integracoes.supabase import queue
        return queue.registrar_evento(ev)

    def checar_segredo(self, headers) -> bool:
        """Segredo do webhook, quando configurado.

        Comparacao em tempo constante: `==` em string secreta cede
        informacao por tempo. Nao e paranoia, e o padrao.
        """
        if not self.cfg.WORKI_WEBHOOK_SECRET:
            return True
        recebido = (headers.get("X-Webhook-Secret") or "").strip()
        if not recebido:
            return False
        return hmac.compare_digest(recebido, self.cfg.WORKI_WEBHOOK_SECRET)

    def processar(self, payload, headers) -> tuple[int, dict]:
        """Trata um POST. Devolve (status_http, corpo)."""
        if not self.checar_segredo(headers):
            return 401, {"ok": False, "erro": "segredo invalido"}

        eventos = payload if isinstance(payload, list) else [payload]
        if not eventos:
            return 200, {"ok": True, "recebidos": 0}

        if len(eventos) > 50:
            # Acima disso nao e mensagem, e abuso (ou bug da Evolution).
            # 413 faz ela nao repetir.
            return 413, {"ok": False, "erro": "lote grande demais"}

        detalhe = []
        for bruto in eventos:
            self.auditado += 1
            ev = normalizar(bruto, self.cfg.EVOLUTION_INSTANCE)

            if ev is None:
                self.recusados += 1
                detalhe.append({"motivo": "nao_relevante"})
                continue

            ok, motivo = checar_autorizacao(ev, self.cfg)
            if not ok:
                self.recusados += 1
                # Log com o numero mascarado: o log vai para o container
                # e pode ser lido por quem nao deveria.
                detalhe.append({
                    "motivo": motivo,
                    "remetente": f"...{ev.remetente_numeros[-4:]}"
                                  if ev.remetente_numeros else None,
                })
                log.info("recusado %s de ...%s", motivo, ev.remetente_numeros[-4:])
                continue

            try:
                entrada_id, ja_existia = self.gravar(ev)
            except Exception as e:
                # Nao confirmar. 500 faz a Evolution repetir, e a RPC
                # deduplica pelo provider_message_id — entao a repeticao
                # nao cria trabalho duplicado.
                self.falhas += 1
                log.error("falha ao persistir: %s", e)
                return 500, {"ok": False, "erro": "persistencia falhou"}

            if entrada_id is None:
                self.falhas += 1
                log.error("RPC devolveu entrada_id nulo")
                return 500, {"ok": False, "erro": "persistencia falhou"}

            self.autorizados += 1
            if ja_existia:
                self.duplicados += 1
            detalhe.append({
                "entrada": str(entrada_id),
                "duplicada": bool(ja_existia),
                "grupo": bool(ev.em_grupo),
            })

        return 200, {"ok": True, "recebidos": len(eventos), "detalhe": detalhe}

    def health(self) -> tuple[int, dict]:
        """Liveness: o processo responde. Nao diz se o banco esta de pe."""
        return 200, {"ok": True, "servico": "worki-agent",
                     "componentes": ["receptor"]}

    def readiness(self) -> tuple[int, dict]:
        """Readiness: dependencias de pe.

        Distingue liveness de readiness porque sao coisas diferentes: um
        container que nao fala com o banco nao deve receber trafego, mas
        tambem nao deve ser morto.
        """
        estado = {
            "supabase": False,
            "worker": False,
            "evolution": bool(self.cfg.EVOLUTION_API_URL),
        }
        try:
            from integracoes.supabase import queue
            estado["supabase"] = queue.ping()
        except Exception as e:
            estado["supabase"] = False
            estado["erro_supabase"] = str(e)[:200]
        try:
            # Importar pelo nome do modulo, e NAO executar o arquivo: o
            # heartbeat vive em `_ULTIMO_TIC`, que e global deste modulo.
            #
            # Se o worker subisse por `python -m integracoes.worker.relay`, ele
            # rodaria sob o nome `__main__` — um objeto de modulo DIFERENTE,
            # com outro `_ULTIMO_TIC`. O receptor, que importa pelo nome, veria
            # o valor congelado em 0.0 e reportaria worker morto para sempre,
            # mesmo com o worker vivo e trabalhando.
            #
            # Por isso o entrypoint usa `-c "import ...; main()"` em vez de
            # `-m`: o processo registra o modulo com o nome canonico, e o
            # heartbeat passa a ser visivel para o receptor.
            from integracoes.worker import relay
            estado["worker"] = relay.esta_vivo()
        except Exception as e:
            estado["worker"] = False
            estado["erro_worker"] = str(e)[:200]

        ok = estado["supabase"] and estado["worker"]
        # 503 quando nao esta pronto: o EasyPanel usa isso para nao
        # mandar trafego. 200 com {"ok": false} seria enganoso.
        return (200 if ok else 503), {"ok": ok, "dependencias": estado}


def fazer_handler(receptor: Receptor):
    """Monta o handler HTTP ligado a um Receptor."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "worki-agent"
        sys_version = ""

        def _responder(self, status: int, corpo: dict):
            bruto = json.dumps(corpo, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(bruto)))
            self.end_headers()
            self.wfile.write(bruto)

        def do_GET(self):
            rota = self.path.split("?")[0].rstrip("/") or "/"
            if rota in ("/", "/health", "/healthz"):
                status, corpo = receptor.health()
            elif rota in ("/ready", "/readyz"):
                status, corpo = receptor.readiness()
            else:
                status, corpo = 404, {"ok": False, "erro": "rota nao existe"}
            self._responder(status, corpo)

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/webhooks/evolution":
                self._responder(404, {"ok": False, "erro": "rota nao existe"})
                return
            try:
                tamanho = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._responder(400, {"ok": False, "erro": "Content-Length invalido"})
                return
            # Limite de tamanho: sem isso, um POST gigante vira consumo
            # de memoria. 1 MB e folgado para payload de WhatsApp.
            if tamanho > 1_048_576:
                self._responder(413, {"ok": False, "erro": "corpo grande demais"})
                return
            try:
                payload = json.loads(self.rfile.read(tamanho) or b"[]")
            except json.JSONDecodeError:
                # JSON invalido nao adianta repetir.
                self._responder(200, {"ok": False, "erro": "json invalido"})
                return

            status, corpo = receptor.processar(payload, self.headers)
            self._responder(status, corpo)

        def log_message(self, formato, *args):
            # Log de acesso do BaseHTTPRequestHandler vai para stderr com
            # formatacao propria. Redireciona para o logger, e sem o
            # corpo da mensagem.
            log.debug("%s", formato % args)

    return Handler


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = Config.carregar()
    except SystemExit as e:
        log.error("configuracao incompleta: %s", e)
        raise

    receptor = Receptor(cfg)
    servidor = ThreadingHTTPServer(("0.0.0.0", cfg.PORT), fazer_handler(receptor))
    log.info("receptor ouvindo em :%d | instancia=%s | grupos=%d",
             cfg.PORT, cfg.EVOLUTION_INSTANCE, len(cfg.WHATSAPP_GRUPO_COMANDO_JIDS))

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log.info("parando")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
