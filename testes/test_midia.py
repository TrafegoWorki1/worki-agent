"""Audio e documento pelo WhatsApp: reconhecer, baixar, transcrever, salvar.

Os servidores (Whisper e Evolution) sao servidores HTTP de verdade, locais e
falsos: o codigo de rede roda de fato, sem internet.
"""
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes import midia  # noqa: E402
from integracoes.evolution.webhook import Receptor, normalizar  # noqa: E402

OGG = b"OggS" + b"\x00" * 64
PDF = b"%PDF-1.4 fake"
DONO = "558592494552"


# --- servidor falso ----------------------------------------------------

class Falso:
    """Sobe um servidor local. `rotas` = {(metodo, caminho_sem_query): (status, corpo)}."""

    def __init__(self, rotas):
        self.rotas, self.recebidos = rotas, []
        falso = self

        class H(BaseHTTPRequestHandler):
            def _tratar(self):
                n = int(self.headers.get("Content-Length") or 0)
                corpo = self.rfile.read(n)
                caminho = self.path.split("?")[0]
                falso.recebidos.append({"metodo": self.command, "caminho": caminho,
                                        "query": self.path.partition("?")[2],
                                        "cab": dict(self.headers), "corpo": corpo})
                status, resp = falso.rotas.get((self.command, caminho), (404, {"erro": "nao existe"}))
                bruto = json.dumps(resp).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(bruto)))
                self.end_headers()
                self.wfile.write(bruto)

            do_POST = do_GET = _tratar

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def fechar(self):
        self.srv.shutdown()


@pytest.fixture
def servidores():
    abertos = []

    def criar(rotas):
        f = Falso(rotas)
        abertos.append(f)
        return f
    yield criar
    for f in abertos:
        f.fechar()
    midia._FORMATO_QUE_FUNCIONOU.clear()


def cfg(**kw):
    base = dict(EVOLUTION_API_URL="", EVOLUTION_API_KEY="chave-evo", WHISPER_URL="",
                WHISPER_API_KEY="chave-whisper", WORKI_STT_FORMATO="auto",
                WORKI_STT_MODELO="whisper-1", WORKI_STT_IDIOMA="pt",
                WORKI_MIDIA_MAX_MB=20, WORKI_ENTRADA_DIR="/tmp/x-nao-usado",
                EVOLUTION_INSTANCE="agent-domintante", WHATSAPP_NUMERO_AUTORIZADO=DONO,
                WHATSAPP_GRUPO_COMANDO_JIDS=[], WORKI_WEBHOOK_SECRET="",
                WORKI_ATALHO_ANDAMENTO=False)
    base.update(kw)
    return SimpleNamespace(**base)


def evento(msg, de=f"{DONO}@s.whatsapp.net", mid="MID123456"):
    return {"event": "MESSAGES_UPSERT", "instance": "agent-domintante",
            "data": {"remoteJid": de, "key": {"id": mid}, "message": msg}}


# --- reconhecer --------------------------------------------------------

def test_audio_e_reconhecido_e_nao_tem_texto():
    ev = normalizar(evento({"audioMessage": {"mimetype": "audio/ogg; codecs=opus",
                                             "seconds": 7, "fileLength": "1200"}}), "agent-domintante")
    assert ev.midia["tipo"] == "audio" and ev.midia["mimetype"] == "audio/ogg"
    assert ev.texto == "" and ev.tipo_mensagem == "audioMessage" and ev.midia["tamanho"] == 1200


def test_documento_com_legenda_vira_texto_da_legenda():
    ev = normalizar(evento({"documentWithCaptionMessage": {"message": {"documentMessage": {
        "mimetype": "application/pdf", "fileName": "proposta.pdf", "caption": "olha isso"}}}}),
        "agent-domintante")
    assert ev.midia["tipo"] == "documento" and ev.texto == "olha isso"


def test_base64_inline_do_webhook_e_aproveitado():
    b64 = base64.b64encode(OGG).decode()
    ev = normalizar(evento({"audioMessage": {"mimetype": "audio/ogg"}, "base64": b64}), "agent-domintante")
    assert ev.midia["base64"] == b64


def test_mensagem_sem_texto_e_sem_midia_continua_descartada():
    assert normalizar(evento({"stickerMessage": {}}), "agent-domintante") is None


# --- validar e proteger ------------------------------------------------

def test_tipo_de_documento_nao_aceito_e_recusado():
    with pytest.raises(midia.MidiaErro, match="não é aceito"):
        midia.validar({"tipo": "documento", "mimetype": "application/x-msdownload", "tamanho": 10})


def test_arquivo_grande_demais_e_recusado_antes_de_baixar():
    with pytest.raises(midia.MidiaErro, match="grande demais"):
        midia.validar({"tipo": "audio", "mimetype": "audio/ogg", "tamanho": 30 * 1024 * 1024})


@pytest.mark.parametrize("nome", ["../../etc/passwd", "..\\..\\x.pdf", "a/b/c.pdf", "", "  .. "])
def test_nome_nao_escapa_da_pasta(nome, tmp_path):
    destino = midia.salvar_documento(tmp_path / "entrada", "MID1", nome, "application/pdf", PDF)
    assert (tmp_path / "entrada") in destino.parents and destino.read_bytes() == PDF
    assert destino.suffix == ".pdf"


def test_extensao_segue_o_tipo_real_e_nao_o_nome():
    assert midia.nome_seguro("planilha.exe", "application/pdf").endswith(".pdf")


def test_url_colada_duas_vezes_e_corrigida():
    u = "https://n8n-whisper-api.ubufeb.easypanel.host/https://n8n-whisper-api.ubufeb.easypanel.host/"
    assert midia.normalizar_url(u) == "https://n8n-whisper-api.ubufeb.easypanel.host"
    assert midia.normalizar_url("http://a:9000/") == "http://a:9000"


# --- buscar o arquivo na Evolution ------------------------------------

def test_busca_o_arquivo_pela_evolution_com_a_chave(servidores):
    evo = servidores({("POST", "/chat/getBase64FromMediaMessage/agent-domintante"):
                      (200, {"base64": base64.b64encode(OGG).decode()})})
    bruto = midia.obter_bytes({"base64": None}, "MID123", cfg(EVOLUTION_API_URL=evo.url), "agent-domintante")
    assert bruto == OGG
    r = evo.recebidos[0]
    cab = {k.lower(): v for k, v in r["cab"].items()}      # cabecalho nao diferencia maiuscula
    assert cab["apikey"] == "chave-evo" and json.loads(r["corpo"])["message"]["key"]["id"] == "MID123"


def test_evolution_sem_o_endpoint_da_erro_claro(servidores):
    evo = servidores({})
    with pytest.raises(midia.MidiaErro, match="404"):
        midia.obter_bytes({"base64": None}, "M", cfg(EVOLUTION_API_URL=evo.url), "agent-domintante")


def test_base64_do_webhook_dispensa_a_evolution():
    assert midia.obter_bytes({"base64": base64.b64encode(OGG).decode()}, "M", cfg(), "i") == OGG


# --- transcrever -------------------------------------------------------

def test_transcreve_no_formato_openai(servidores):
    w = servidores({("POST", "/v1/audio/transcriptions"): (200, {"text": " oi, tudo bem? "})})
    t = midia.transcrever(OGG, "audio/ogg", cfg(WHISPER_URL=w.url))
    assert t == "oi, tudo bem?"
    r = w.recebidos[0]
    assert r["cab"]["Authorization"] == "Bearer chave-whisper"
    assert b'name="model"' in r["corpo"] and b'name="file"' in r["corpo"] and OGG in r["corpo"]


def test_cai_para_o_formato_asr_quando_o_openai_nao_existe(servidores):
    w = servidores({("POST", "/asr"): (200, {"text": "texto do asr"})})
    assert midia.transcrever(OGG, "audio/ogg", cfg(WHISPER_URL=w.url)) == "texto do asr"
    assert [x["caminho"] for x in w.recebidos] == ["/v1/audio/transcriptions", "/asr"]
    assert "language=pt" in w.recebidos[1]["query"] and b'name="audio_file"' in w.recebidos[1]["corpo"]
    # lembra o formato que funcionou
    w.recebidos.clear()
    midia.transcrever(OGG, "audio/ogg", cfg(WHISPER_URL=w.url))
    assert [x["caminho"] for x in w.recebidos] == ["/asr"]


def test_chave_recusada_vira_mensagem_curta(servidores):
    w = servidores({("POST", "/v1/audio/transcriptions"): (401, {"erro": "x"})})
    with pytest.raises(midia.MidiaErro, match="recusou a chave"):
        midia.transcrever(OGG, "audio/ogg", cfg(WHISPER_URL=w.url))


def test_sem_url_diz_que_nao_esta_configurado():
    with pytest.raises(midia.MidiaErro, match="não está configurada"):
        midia.transcrever(OGG, "audio/ogg", cfg())


def test_servidor_fora_do_ar_vira_mensagem_curta():
    with pytest.raises(midia.MidiaErro, match="não respondeu"):
        midia.transcrever(OGG, "audio/ogg", cfg(WHISPER_URL="http://127.0.0.1:9"), timeout=2)


def test_audio_sem_fala_e_avisado(servidores):
    w = servidores({("POST", "/v1/audio/transcriptions"): (200, {"text": "  "})})
    with pytest.raises(midia.MidiaErro, match="não entendi"):
        midia.transcrever(OGG, "audio/ogg", cfg(WHISPER_URL=w.url))


# --- do evento ate o texto da fila ------------------------------------

def test_audio_de_ponta_a_ponta(servidores):
    evo = servidores({("POST", "/chat/getBase64FromMediaMessage/agent-domintante"):
                      (200, {"base64": base64.b64encode(OGG).decode()})})
    w = servidores({("POST", "/v1/audio/transcriptions"): (200, {"text": "cria uma landing page"})})
    c = cfg(EVOLUTION_API_URL=evo.url, WHISPER_URL=w.url)
    ev = normalizar(evento({"audioMessage": {"mimetype": "audio/ogg"}}), "agent-domintante")
    assert midia.texto_para_a_fila(ev.midia, ev.provider_message_id, c, ev.instancia) == \
        "[áudio transcrito] cria uma landing page"


def test_documento_e_salvo_e_o_prompt_recebe_o_caminho(tmp_path):
    b64 = base64.b64encode(PDF).decode()
    ev = normalizar(evento({"documentMessage": {"mimetype": "application/pdf", "fileName": "proposta.pdf",
                                                "caption": "resume"}, "base64": b64}), "agent-domintante")
    t = midia.texto_para_a_fila(ev.midia, ev.provider_message_id, cfg(), ev.instancia, pasta=tmp_path)
    arquivo = next(tmp_path.iterdir())
    assert arquivo.read_bytes() == PDF and str(arquivo) in t
    assert t.startswith("resume") and "dado, não ordem" in t


# --- receptor ----------------------------------------------------------

def receptor(preparar, enviadas, gravados, cfg_=None):
    def gravar(ev):
        gravados.append(ev.texto)
        return "entrada-1", False
    return Receptor(cfg_ or cfg(), gravar=gravar, preparar_midia=preparar,
                    enviar=lambda jid, txt: enviadas.append(txt) or ("entregue", "x"),
                    executar=lambda fn: fn())            # sincrono, para testar


def _processar(rec, payload):
    return rec.processar(payload, {})


def test_audio_do_dono_e_transcrito_e_gravado_na_fila():
    enviadas, gravados = [], []
    rec = receptor(lambda ev: "[áudio transcrito] oi", enviadas, gravados)
    status, corpo = _processar(rec, evento({"audioMessage": {"mimetype": "audio/ogg"}}))
    assert status == 200 and corpo["detalhe"][0]["midia"] == "audio"
    assert gravados == ["[áudio transcrito] oi"] and enviadas == [] and rec.midias == 1


def test_audio_de_estranho_nao_e_baixado_nem_transcrito():
    chamado = []
    rec = receptor(lambda ev: chamado.append(1) or "x", [], [])
    _processar(rec, evento({"audioMessage": {"mimetype": "audio/ogg"}}, de="5511999990000@s.whatsapp.net"))
    assert chamado == [] and rec.recusados == 1


def test_falha_conhecida_avisa_o_dono_em_vez_de_calar():
    enviadas, gravados = [], []

    def falha(ev):
        raise midia.MidiaErro("o servidor de transcrição não respondeu.")
    rec = receptor(falha, enviadas, gravados)
    _processar(rec, evento({"audioMessage": {"mimetype": "audio/ogg"}}))
    assert gravados == [] and len(enviadas) == 1
    assert "não consegui usar" in enviadas[0] and "não respondeu" in enviadas[0]


def test_falha_inesperada_nao_vaza_detalhe_tecnico_no_whatsapp():
    enviadas = []

    def quebra(ev):
        raise RuntimeError("Traceback com SEGREDO-INTERNO")
    rec = receptor(quebra, enviadas, [])
    _processar(rec, evento({"audioMessage": {"mimetype": "audio/ogg"}}))
    assert len(enviadas) == 1 and "SEGREDO-INTERNO" not in enviadas[0]


def test_mesmo_id_em_andamento_nao_processa_duas_vezes():
    pendentes, gravados = [], []
    rec = Receptor(cfg(), gravar=lambda ev: (gravados.append(1) or ("e", False)),
                   preparar_midia=lambda ev: "texto", executar=lambda fn: pendentes.append(fn))
    _processar(rec, evento({"audioMessage": {"mimetype": "audio/ogg"}}))
    _, corpo = _processar(rec, evento({"audioMessage": {"mimetype": "audio/ogg"}}))   # repeticao da Evolution
    assert corpo["detalhe"][0]["duplicada"] is True and len(pendentes) == 1
    pendentes[0]()
    assert gravados == [1]
