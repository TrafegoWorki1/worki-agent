"""Testes de fechamento: as garantias que o dono definiu.

O criterio de aceite: continuar uma conversa, reiniciar o servico,
recuperar contexto e tarefa **sem repetir acoes ou respostas**.

Estes testes usam banco e Evolution em memoria (fakes), entao rodam sem
Supabase, sem Hermes e sem gastar token. O que fica de fora — RPC real,
persistencia real — e o que o Codex valida do lado dele.

Rodar:  python testes/test_fechamento.py
"""

import sys
import threading
import time
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

FALHOU, PASSOU = [], []


def checar(nome, condicao, detalhe=""):
    (PASSOU if condicao else FALHOU).append(nome)
    print(f"  [{'ok  ' if condicao else 'FALHA'}] {nome}"
          f"{(' — ' + detalhe) if detalhe else ''}")


# ==========================================================================
# Receptor
# ==========================================================================

def testar_receptor():
    from integracoes.config import Config
    from integracoes.evolution.webhook import (
        Receptor, checar_autorizacao, normalizar)

    print("\n" + "=" * 62)
    print("1. RECEPTOR — dedupe, allowlist e grupo")
    print("=" * 62)

    cfg = Config.carregar()
    cfg.WHATSAPP_NUMERO_AUTORIZADO = "558592494552"
    cfg.WHATSAPP_GRUPO_COMANDO_JIDS = ["1203630@g.us"]
    cfg.WORKI_WEBHOOK_SECRET = "segredo-de-teste"

    gravados = []

    def gravar_fake(ev):
        # Simula dedupe: mesmo provider_message_id nao grava duas vezes.
        for g in gravados:
            # `g` e dict: acesso por indice. Usar `g.provider_message_id`
            # levanta AttributeError, que o receptor trata como falha de
            # persistencia e devolve 500 — obscuring o problema real.
            if g["provider_message_id"] == ev.provider_message_id:
                return g["id"], True
        novo = {"id": f"e{len(gravados) + 1}",
                "provider_message_id": ev.provider_message_id}
        gravados.append(novo)
        return novo["id"], False

    r = Receptor(cfg, gravar=gravar_fake)

    def evento(texto="oi", de="558592494552", remote="558592494552@s.whatsapp.net",
               mid="MSG1", grupo=False, from_me=False, extra=None):
        # `participant` fica dentro de `data.key`, que e onde a Evolution
        # o coloca. Montar em `data` direto produz um evento que nunca
        # acontece e mascara falha de parsing.
        key = {"id": mid, "fromMe": from_me, "remoteJid": remote}
        if grupo:
            key["participant"] = de
        d = {
            "instance": "agent-domintante",
            "remoteJid": remote,
            "key": key,
            "message": {"conversation": texto},
        }
        if extra:
            d["message"].update(extra)
        return {"event": "MESSAGES_UPSERT", "instance": "agent-domintante",
                "data": d}

    cab = {"X-Webhook-Secret": "segredo-de-teste"}

    # --- segredo ---
    st, _ = r.processar([evento()], {})
    checar("segredo errado bloqueia", st == 401, f"HTTP {st}")

    # --- mensagem normal ---
    st, corpo = r.processar([evento("cria uma pagina")], cab)
    checar("autorizado entra", st == 200 and gravados, f"HTTP {st}")

    # --- duplicata ---
    st2, corpo2 = r.processar([evento("cria uma pagina", mid="MSG1")], cab)
    checar("duplicata nao cria entrada nova", len(gravados) == 1,
           f"{len(gravados)} gravadas")
    checar("duplicata responde 200", st2 == 200, f"HTTP {st2}")

    # --- fromMe ignorado ---
    antes = len(gravados)
    r.processar([evento("eu mesmo", mid="MSG2", from_me=True)], cab)
    checar("fromMe ignorado", len(gravados) == antes)

    # --- numero nao autorizado ---
    # Em conversa privada o remetente vem do proprio remoteJid, entao
    # trocar so `de` nao muda nada: o `de` so e lido em grupo (participant).
    # O evento precisa trazer o remoteJid do intruso.
    antes = len(gravados)
    r.processar([evento("oi", remote="5511999999999@s.whatsapp.net",
                        de="5511999999999", mid="MSG3")], cab)
    checar("numero nao autorizado recusado", len(gravados) == antes,
           f"{len(gravados)} vs {antes}")

    # Intruso em grupo autorizado tambem e recusado: o grupo passa, o
    # participant nao.
    antes = len(gravados)
    r.processar([evento("intruso no grupo", remote="1203630@g.us",
                        de="5511999999999", mid="MSG3b", grupo=True)], cab)
    checar("participant nao autorizado em grupo recusado",
           len(gravados) == antes, f"{len(gravados)} vs {antes}")

    # --- instancia errada ---
    antes = len(gravados)
    ev_inst = evento("oi", mid="MSG4")
    ev_inst["instance"] = "outra-instancia"
    ev_inst["data"]["instance"] = "outra-instancia"
    r.processar([ev_inst], cab)
    checar("instancia errada recusada", len(gravados) == antes)

    # --- grupo autorizado, participant resolve ---
    antes = len(gravados)
    st, _ = r.processar(
        [evento("comando de grupo", remote="1203630@g.us", mid="MSG5",
                grupo=True)], cab)
    checar("grupo autorizado entra", len(gravados) == antes + 1, f"HTTP {st}")

    # --- grupo NAO autorizado ---
    antes = len(gravados)
    st, det = r.processar(
        [evento("intruso", remote="999999@g.us", mid="MSG6", grupo=True)], cab)
    checar("grupo nao autorizado recusado", len(gravados) == antes,
           str(det)[:70])

    # --- grupo com lista vazia: negado por padrao ---
    cfg_vazio = Config.carregar()
    cfg_vazio.WHATSAPP_GRUPO_COMANDO_JIDS = []
    r2 = Receptor(cfg_vazio, gravar=gravar_fake)
    ev_norm = normalizar(evento("oi", remote="1203630@g.us", grupo=True,
                                mid="MSG7"), "agent-domintante")
    ok, motivo = checar_autorizacao(ev_norm, cfg_vazio)
    checar("lista vazia nega grupo", not ok and motivo == "grupo_nao_configurado",
           motivo)

    # --- grupo: remetente e participant, nunca o grupo ---
    ev_gr = normalizar(evento("oi", remote="1203630@g.us", grupo=True,
                              de="558592494552", mid="MSG8"),
                       "agent-domintante")
    checar("grupo: remetente = participant",
           ev_gr.remetente_numeros == "558592494552", ev_gr.remetente_numeros)
    checar("grupo: destino = remoteJid",
           ev_gr.chat_jid == "1203630@g.us", ev_gr.chat_jid)

    # --- participantPn (versao nova) ---
    d = {"remoteJid": "1203630@g.us",
         "key": {"id": "MSG9", "remoteJid": "1203630@g.us",
                 "participantPn": "558592494552"},
         "message": {"conversation": "oi via pn"}}
    ev_pn = normalizar({"event": "MESSAGES_UPSERT", "data": d},
                       "agent-domintante")
    checar("participantPn reconhecido",
           ev_pn is not None and ev_pn.remetente_numeros == "558592494552",
           ev_pn.remetente_numeros if ev_pn else "None")

    # --- texto vazio / sem id ---
    checar("vazio nao enfileira", normalizar(evento("   ", mid="M10"),
                                          "agent-domintante") is None)
    ev_sem_id = {"event": "MESSAGES_UPSERT",
                 "data": {"remoteJid": "558592494552@s.whatsapp.net",
                          "key": {}, "message": {"conversation": "oi"}}}
    checar("sem id nao enfileira (dedupe imposible)",
           normalizar(ev_sem_id, "agent-domintante") is None)

    # --- audio nao entra ---
    ev_audio = {"event": "MESSAGES_UPSERT", "data": {
        "remoteJid": "558592494552@s.whatsapp.net",
        "key": {"id": "MA1"},
        "message": {"audioMessage": {"ptt": True}}}}
    checar("audio nao enfileira", normalizar(ev_audio, "agent-domintante") is None)

    # --- falha de persistencia devolve 5xx ---
    def gravar_quebrada(ev):
        raise RuntimeError("banco fora")

    r3 = Receptor(cfg, gravar=gravar_quebrada)
    st, _ = r3.processar([evento("oi", mid="MSG11")], cab)
    checar("falha de persistencia devolve 500", st == 500, f"HTTP {st}")

    # --- lote grande demais ---
    st, _ = r.processar([evento(f"m{i}", mid=f"BULK{i}") for i in range(60)], cab)
    checar("lote grande demais barrado", st == 413, f"HTTP {st}")


# ==========================================================================
# Fila: concorrencia
# ==========================================================================

def testar_fila():
    print("\n" + "=" * 62)
    print("2. FILA — reserva atomica e ordem por conversa")
    print("=" * 62)

    from integracoes.supabase import queue as db

    class FilaFake:
        """Implementa a semantica da RPC: skip locked + uma por conversa."""

        def __init__(self):
            self.itens = []          # (id, conversa_id, status, owner)
            self.processo = 0
            self._lock = threading.Lock()
            self._cv = threading.Condition(self._lock)

        def _disponiveis(self, convo_bloqueada):
            # Devolve INDICES, nao os itens: reservar precisa escrever de
            # volta na posicao certa, e devolver o item exigiria buscar o
            # indice de novo (racesco entre leitura e escrita).
            return [n for n, i in enumerate(self.itens)
                    if i[2] == "aguardando" and i[1] != convo_bloqueada]

        def reservar(self, owner):
            with self._lock:
                # Em producao isto e `for update skip locked` numa
                # transacao: uma worker ve a linha travada e pula.
                bloqueio = [i[1] for i in self.itens if i[2] == "processando"]
                cands = self._disponiveis(bloqueio[0] if bloqueio else None)
                if not cands:
                    return None
                pos = cands[0]
                atual = self.itens[pos]
                self.itens[pos] = (atual[0], atual[1], "processando", owner)
                return atual[0]

        def concluir(self, item_id, owner):
            with self._lock:
                for n, it in enumerate(self.itens):
                    if it[0] == item_id:
                        self.itens[n] = (it[0], it[1], "concluida", owner)
                        self._cv.notify_all()

        def liberar_todas(self):
            with self._lock:
                self.itens = [
                    (i[0], i[1], "aguardando" if i[2] == "processando" else i[2],
                     None) for i in self.itens]

    fila = FilaFake()
    # Duas entradas da MESMA conversa, uma de outra.
    fila.itens = [(1, "conversa-A", "aguardando", None),
                  (2, "conversa-A", "aguardando", None),
                  (3, "conversa-B", "aguardando", None)]

    # --- duas workers simultaneas nao pegam o mesmo job ---
    pego = []
    trava = threading.Lock()

    def worker(nome):
        for _ in range(6):
            item = fila.reservar(nome)
            if item is None:
                time.sleep(0.01)
                continue
            with trava:
                pego.append(item)
            time.sleep(0.02)
            fila.concluir(item, nome)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)

    checar("cada entrada pega uma vez so",
           sorted(pego) == [1, 2, 3], f"{sorted(pego)}")

    # --- uma por conversa ao mesmo tempo ---
    fila2 = FilaFake()
    fila2.itens = [(1, "A", "aguardando", None), (2, "A", "aguardando", None)]
    primeiro = fila2.reservar("w1")
    segundo = fila2.reservar("w2")
    checar("segunda da mesma conversa nao sai junto",
           primeiro is not None and segundo is None,
           f"{primeiro} / {segundo}")


# ==========================================================================
# Worker: continuidade e nao-repeticao
# ==========================================================================

def testar_worker():
    print("\n" + "=" * 62)
    print("3. WORKER — sessao, gravacao antes do envio, sem repetir")
    print("=" * 62)

    from integracoes.config import Config
    from integracoes.worker.relay import Relay

    class DBFake:
        def __init__(self):
            self.sessoes = {"conversa-1": "20261002_100000_aaaaaa"}
            self.tarefas = []
            self.saidas = {}
            self.entradas_status = {}
            self.saida_gravada_antes_do_envio = None
            self.entrada_concluida_antes_do_envio = None
            self.hermes_rodou = 0
            self.renovadas = 0
            self._seq = 0
            self.eventos = []

        def dono(self):
            return "worker-fake"

        def sessao_da_conversa(self, cid):
            return self.sessoes.get(cid)

        def salvar_sessao(self, cid, sid):
            self.sessoes[cid] = sid
            self.eventos.append(("sessao", sid))

        def tarefas_ativas(self, cid):
            return [t for t in self.tarefas if t.get("conversa_id") == cid]

        def criar_tarefa(self, cid, eid, objetivo):
            self._seq += 1
            tid = f"t{self._seq}"
            self.tarefas.append({"id": tid, "conversa_id": cid,
                                 "entrada_id": eid, "objetivo": objetivo,
                                 "status": "ativa", "proxima_acao": "executar"})
            return tid

        def salvar_progresso(self, tid, checkpoint, resultado=None,
                             status="ativa", proxima=None):
            for t in self.tarefas:
                if t["id"] == tid:
                    t.update({"checkpoint": checkpoint, "status": status,
                              "proxima_acao": proxima, "resultado": resultado})

        def cancelar_tarefa(self, tid, motivo):
            for t in self.tarefas:
                if t["id"] == tid:
                    t.update({"status": "cancelada", "checkpoint": motivo})

        def concluir_tarefa(self, tid, resultado):
            self.salvar_progresso(tid, resultado, resultado=resultado,
                                  status="concluida", proxima="")

        def concluir(self, eid, erro=None):
            self.entradas_status[eid] = "falhou" if erro else "concluida"
            self.eventos.append(("concluir", eid))

        def renovar(self, eid, lease_s=120):
            self.renovadas += 1
            return True

        def registrar_saida(self, eid, chat, texto):
            self._seq += 1
            sid = f"s{self._seq}"
            self.saidas[sid] = {"id": sid, "entrada_id": eid, "chat_jid": chat,
                                "texto": texto, "status": "pendente"}
            self.eventos.append(("saida", sid))
            return sid

        def marcar_saida(self, sid, status, provider_message_id=None, erro=None):
            if sid in self.saidas:
                self.saidas[sid]["status"] = status
            self.eventos.append(("marcar_saida", sid, status))

        def saidas_pendentes(self, limite=10):
            # Inclui 'incerto': o worker reconcilia antes de reenviar.
            # Se o fake nao devolvesse, a reconciliacao nunca seria
            # exercitada e o teste passaria sem provar nada.
            return [s for s in self.saidas.values()
                    if s["status"] in ("pendente", "enviando", "falhou",
                                       "incerto")]

        def recuperar_leases(self):
            return 0

        def reservar(self, limite=1, lease_s=120):
            return []

    class HermesFake:
        def __init__(self, sessao_nova="20261002_110000_bbbbbb"):
            self.sessao_nova = sessao_nova
            self.entradas = []
            self.db = None
            self.responder_vezes = 0
            self.texto = "pronto, fiz isso"

        def responder(self, prompt, session_id=None, contexto=""):
            self.responder_vezes += 1
            self.entradas.append({"prompt": prompt,
                                  "session_id": session_id,
                                  "contexto": contexto})
            self.db.hermes_rodou += 1
            if session_id:
                # Retomada: devolve o mesmo id, como o Hermes faz.
                return types.SimpleNamespace(
                    ok=True, texto=self.texto, session_id=session_id, log="")
            return types.SimpleNamespace(
                ok=True, texto=self.texto, session_id=self.sessao_nova, log="")

    class EVFake:
        def __init__(self, estado="entregue"):
            from integracoes.evolution.cliente import Estado
            self.Estado = Estado
            self.estado = estado
            self.enviados = []

        def enviar_texto(self, numero, texto, timeout=45):
            self.enviados.append((numero, texto))
            return (self.estado, "MID123" if self.estado == "entregue" else None)

        def reconciliar(self, saida):
            return "entregue"

    from integracoes.supabase.queue import Entrada

    cfg = Config.carregar()
    cfg.WORKI_WORKER_LEASE_S = 3   # renova rapido, para o teste
    cfg.WORKI_WORKER_CONCURRENCY = 1

    dbf = DBFake()
    hf = HermesFake()
    hf.db = dbf
    evf = EVFake()
    relay = Relay(cfg, adaptador=hf, dbmod=dbf, evmod=evf)

    entrada = Entrada(id="E1", mensagem_id="M1", conversa_id="conversa-1",
                      session_id="20261002_100000_aaaaaa",
                      texto="cria uma pagina", de="558592494552",
                      chat_jid="558592494552@s.whatsapp.net")

    # --- 1. processa ---
    r = relay.processar(entrada)
    checar("processou com sucesso", r.ok, r.erro[:80])
    checar("Hermes rodou 1 vez", hf.responder_vezes == 1, str(hf.responder_vezes))
    checar("retomou a sessao da conversa",
               hf.entradas[0]["session_id"] == "20261002_100000_aaaaaa",
               str(hf.entradas[0]["session_id"]))
    # O id da sessao NAO vai no prompt: `--resume` ja devolve o historico.
    # Anunciar a retomada gravava o aviso dentro da propria sessao que
    # esta sendo escrita — a sessao 20261003_131416_57f90a chegou a 66
    # mensagens por causa disso. Ver testes/test_retomada.py.
    checar("contexto nao anuncia a retomada",
           "20261002_100000_aaaaaa" not in hf.entradas[0]["contexto"]
           and "retomando" not in hf.entradas[0]["contexto"].lower(),
           hf.entradas[0]["contexto"][:60])
    checar("resposta enviada", len(evf.enviados) == 1)

    # --- ordem das operacoes: outbox antes de enviar ---
    ordem = [e[0] for e in dbf.eventos if e[0] in ("saida", "concluir",
                                                   "marcar_saida")]
    idx_saida = next((i for i, e in enumerate(dbf.eventos)
                      if e[0] == "saida"), -1)
    idx_marcar = next((i for i, e in enumerate(dbf.eventos)
                       if e[0] == "marcar_saida"), -1)
    checar("outbox gravada antes de marcar entrega",
           0 <= idx_saida < idx_marcar, f"{idx_saida} < {idx_marcar}")

    # --- 2. entrega falha: repete SO o envio ---
    print("\n   -- envio falha --")
    ev2 = EVFake(estado="falhou")
    relay.ev = ev2
    antes_hermes = hf.responder_vezes

    # saida ainda pendente (falhou), entrada ja concluida
    primeiro_saida = list(dbf.saidas)[0]
    dbf.saidas[primeiro_saida]["status"] = "falhou"
    relay.entregar_saidas()
    checar("reenviou sem rodar o Hermes",
           hf.responder_vezes == antes_hermes,
           f"{antes_hermes} -> {hf.responder_vezes}")
    checar("reenviou 1 vez", len(ev2.enviados) == 1, str(len(ev2.enviados)))

    # --- 3. envio incerto: nao reenvia, reconcilia ---
    print("\n   -- envio incerto --")
    dbf2 = DBFake()
    hf2 = HermesFake()
    hf2.db = dbf2
    relay2 = Relay(cfg, adaptador=hf2, dbmod=dbf2, evmod=EVFake())
    relay2.processar(entrada)

    # forca incerto
    sid = list(dbf2.saidas)[0]
    dbf2.saidas[sid]["status"] = "incerto"

    ev_certo = EVFake(estado="incerto")
    enviados_certo = []
    ev_certo.enviar_texto = lambda n, t, timeout=45: (
        enviados_certo.append((n, t)),
        (ev_certo.Estado.INCERTO, None))[1]
    reconciliados = []
    ev_certo.reconciliar = lambda s: (reconciliados.append(s), "entregue")[1]
    relay2.ev = ev_certo

    relay2.entregar_saidas()
    checar("incerto NAO reenvia as cegas", len(enviados_certo) == 0,
           f"{len(enviados_certo)} envios")
    checar("incerto foi reconciliado", len(reconciliados) >= 1,
           f"{len(reconciliados)} reconciliacoes")

    # --- 4. incerto entra na lista PARA RECONCILIAR ---
    print("\n   -- filtro da fila de saidas --")
    from integracoes.supabase import queue as qmod
    capturado = {}
    qmod._rest = lambda m, c, body=None, prefer="", timeout=20: (
        capturado.update({"caminho": c}) or [])
    qmod.saidas_pendentes()
    # 'incerto' ENTRA, para o worker reconciliar. O que nao pode e
    # reenviar sem reconciliar — e isso e testado acima.
    checar("incerto entra para reconciliacao",
          "incerto" in capturado.get("caminho", ""),
          capturado.get("caminho", "")[:95])

    # --- 5. reinicio: sessao recuperada ---
    print("\n   -- reinicio do processo --")
    dbf3 = DBFake()
    hf3 = HermesFake(sessao_nova="20261002_120000_cccccc")
    hf3.db = dbf3
    relay3 = Relay(cfg, adaptador=hf3, dbmod=dbf3, evmod=EVFake())

    # processo novo, mesma sessao persistida
    entrada_nova = Entrada(id="E2", mensagem_id="M2",
                           conversa_id="conversa-1",
                           session_id=dbf.sessoes.get("conversa-1"),
                           texto="e agora?", de="558592494552",
                           chat_jid="558592494552@s.whatsapp.net")
    relay3.processar(entrada_nova)
    checar("apos reinicio retomou a sessao salva",
           hf3.entradas[0]["session_id"] == "20261002_100000_aaaaaa",
           str(hf3.entradas[0]["session_id"]))

    # --- 6. chave de idempotencia deterministica ---
    print("\n   -- idempotencia --")
    k1 = qmod.chave_idempotencia("E1", "mesma resposta")
    k2 = qmod.chave_idempotencia("E1", "mesma resposta")
    k3 = qmod.chave_idempotencia("E2", "mesma resposta")
    checar("chave estavel para mesma entrada", k1 == k2)
    checar("chave muda com entrada diferente", k1 != k3)


def main():
    print("#" * 62)
    print("# TESTES DE FECHAMENTO — worki-agent")
    print("# Sem Supabase, sem Evolution real, sem token de modelo.")
    print("#" * 62)
    testar_receptor()
    testar_fila()
    testar_worker()
    print("\n" + "=" * 62)
    print(f"Passou: {len(PASSOU)}   Falhou: {len(FALHOU)}")
    for f in FALHOU:
        print(f"  FALHOU: {f}")
    print("=" * 62)
    return 1 if FALHOU else 0


if __name__ == "__main__":
    sys.exit(main())
