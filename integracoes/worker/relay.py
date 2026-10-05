"""Worker duravel: reserva, Hermes, tarefa, outbox, entrega.

Substitui o `relay.py` antigo (one-shot local, sem sessao, sem lease).

## A ordem das operacoes e o que garante cada uma

Para cada entrada reservada:

    1. recuperar contexto     (sessao, tarefas ativas, checkpoint)
    2. registrar tarefa       (objetivo + proxima acao, ANTES de executar)
    3. rodar o Hermes         (--resume session_id desta conversa)
    4. gravar saida           (idempotency_key deterministica)  <- ANTES de enviar
    5. concluir a entrada     (marca 'concluida')
    6. enviar pelo WhatsApp
    7. marcar saida           ('enviada' | 'falhou' | 'incerto')

O passo 4 antes do 6 e o que impede repetir trabalho: se o processo cair
entre o 4 e o 6, a retentativa recria a **mesma** chave de idempotencia, o
indice unico rejeita, e o passo 3 (Hermes) nunca roda outra vez.

O passo 5 antes do 6 e o que fecha a outra janela: se cair entre o 5 e o
6, a entrada ja esta concluida e nao volta para a fila — quem reenvia e o
`entregar_saidas`, que manda a mensagem ja pronta.

Se cair entre o 3 e o 4: o Hermes ja rodou e nao ha registro. A entrada
volta pela lease vencida e o Hermes roda de novo. **Esta e a janela
residual e ela e real.** O mitigacao e o checkpoint por passo
(`task_steps`), ainda pendente: e ele que impedira a acao externa de
repetir, nao o texto.

## Sessao por conversa

`--resume` so com o `session_id` que veio da conversa. Nunca `latest`: e
global e duas mensagens da mesma conversa em paralelo terminariam na
mesma sessao, embaralhando o contexto. Nunca adivinha senao criada por
outro processo: usa so o id que o proprio Hermes devolveu.

## Ordem por conversa

A RPC de reserva ja nao devolve duas entradas da mesma conversa se existe
uma em 'processando'. Aqui ha um segundo filtro, porque o requisito e
duro: se duas conversas differentes tem entradas, processa uma por vez
(concurrency 1 na versao inicial) e nunca duas da mesma.

## Lease

Tarefa longa renova a lease periodicamente. Se nao conseguir renovar,
perdeu a lease para outra worker: aborta em vez de terminar um trabalho
duplicado.
"""

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from integracoes.config import Config  # noqa: E402
from integracoes.evolution import cliente as ev  # noqa: E402
from integracoes.evolution import formato  # noqa: E402
from integracoes import ferramentas, memoria  # noqa: E402
from integracoes.hermes.adapter import Adaptador  # noqa: E402
from integracoes.supabase import queue as db  # noqa: E402

log = logging.getLogger("worki.worker")

# Aviso de andamento (opt-in via WORKI_ACK_AFTER_SECONDS). Curto, neutro e sem
# promessa de prazo: o objetivo e o usuario saber que a mensagem foi vista.
MENSAGEM_AVISO = "Recebi. Estou trabalhando nisso, te respondo em seguida."

_PARANDO = threading.Event()

# Heartbeat do worker em ARQUIVO, nao em memoria.
#
# Receptor e worker sao PROCESSOS SEPARADOS (o entrypoint sobe os dois com &).
# Um global de modulo so existe dentro de um processo: o `_ULTIMO_TIC` que o
# worker atualiza nao e o mesmo objeto que o receptor le em `esta_vivo()`.
# Por isso o /ready ficava em 503 com "worker": false mesmo com o worker
# vivo e trabalhando — e o sintoma e identico ao de worker morto, o que
# leva a diagnosticar um servico saudavel como quebrado.
#
# O arquivo e o unico canal que os dois processos compartilham. Fica em
# WORKI_HERMES_HOME, que ja e volume persistente.
#
# O global em memoria continua existindo para os testes, que rodam tudo no
# mesmo processo; o arquivo e apenas o espelho para o caso cross-processo.
_ULTIMO_TIC = 0.0
_TIC_LOCK = threading.Lock()


def _caminho_tic() -> str:
    base = os.environ.get("WORKI_HERMES_HOME") or "/opt/data"
    return os.path.join(base, "worker.heartbeat")


def _tic():
    """Registra o heartbeat, em memoria e em arquivo."""
    global _ULTIMO_TIC
    agora = time.time()
    with _TIC_LOCK:
        _ULTIMO_TIC = agora
    try:
        caminho = _caminho_tic()
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        # Escreve e renomeia: o leitor nunca ve um arquivo pela metade.
        tmp = caminho + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(agora))
        os.replace(tmp, caminho)
    except OSError as e:
        log.debug("nao gravei o heartbeat em arquivo: %s", e)


# Janela de 3x o intervalo de poll: tolera dois tics perdidos sem acusar
# worker morto, e em 3x o poll nao ha atraso relevante entre a morte real
# e o /ready virar 503. Acusar cedo demais rejeitaria trafego de um worker
# vivo, que e o erro mais caro dos dois.
_WORKER_POLL_S = 30


def _janela_padrao() -> int:
    poll = _WORKER_POLL_S
    try:
        poll = int(os.environ.get("WORKI_RECOVERY_POLL_SECONDS") or _WORKER_POLL_S)
    except (TypeError, ValueError):
        pass
    # 3x o poll: tolera dois tics perdidos sem acusar worker morto.
    return max(15, poll * 3)


def esta_vivo(maxidade_s: int | None = None) -> bool:
    """O worker esta rodando? Usado no /ready.

    Le o heartbeat do arquivo (cross-processo) e cai no global em memoria
    (mesmo processo, caso dos testes).
    """
    if maxidade_s is None:
        maxidade_s = _janela_padrao()
    agora = time.time()

    caminho = _caminho_tic()
    try:
        with open(caminho, "r", encoding="utf-8") as fh:
            ultimo = float(fh.read().strip())
        if ultimo > 0 and (agora - ultimo) < maxidade_s:
            return True
    except (OSError, ValueError):
        pass

    with _TIC_LOCK:
        ultimo_mem = _ULTIMO_TIC
    if ultimo_mem == 0.0:
        return False
    return (agora - ultimo_mem) < maxidade_s


@dataclass
class Resultado:
    ok: bool
    texto: str | None
    sessao: str | None
    erro: str = ""


class Relay:
    """Ciclo do worker. Injetavel para teste sem banco e sem Hermes."""

    def __init__(self, cfg=None, adaptador=None, dbmod=None, evmod=None):
        self.cfg = cfg or Config.carregar()
        self.hermes = adaptador or Adaptador(
            timeout=self.cfg.WORKI_TASK_TIMEOUT_SECONDS,
            cwd=self.cfg.WORKI_WORKSPACE_DIR if Path(
                self.cfg.WORKI_WORKSPACE_DIR).exists() else None,
        )
        self.db = dbmod or db
        self.ev = evmod or ev
        self.processadas = 0
        self.falhas = 0
        self.entregues = 0

    # ------------------------------------------------------------------
    # contexto
    # ------------------------------------------------------------------

    def montar_contexto(self, entrada) -> str:
        """Monta o material de apoio do prompt.

        Vai separado do pedido, com um divisor, para o agente saber o que
        e instrucao e o que e dado. Isso tambem impede que um documento
        recuperado seja lido como ordem — documento e dado, nunca
        autoridade para ignorar o AGENTS.md.
        """
        partes = []

        # NAO anunciar a retomada no prompt.
        #
        # O `--resume` ja devolve o historico inteiro da sessao. Injetar
        # "voce esta retomando a sessao X" gravava o aviso DENTRO da
        # propria sessao X, que e a que esta sendo escrita: cada mensagem
        # acrescentava mais uma copia do aviso ao contexto que ela mesma
        # manda reler.
        #
        # Medido em 2026-10-03: a sessao 20261003_131416_57f90a chegou a
        # 66 mensagens, quase todas o mesmo cabecalho, e o agente passou a
        # responder "ja li a sessao X duas vezes — ela so repete esse
        # cabecalho". O aviso era redundante: `--resume` faz o trabalho.
        #
        # A informacao util — o que fazer agora — vem do bloco de tarefas
        # abaixo, que traz objetivo, proxima acao e checkpoint.

        # Uma tarefa so entra no prompt se houver trabalho de verdade.
        #
        # `bloqueada` com a etapa "Resultado salvo" nao e trabalho a
        # retomar: o resultado gravado era o log do comando (bug da outbox,
        # corrigido em 3cb6888), nao a saida do agente. Anunciar isso como
        # "tarefa em andamento" fazia o agente procurar execucao que nao
        # existe e responder que nao tinha nada para retomar.
        tarefas = [
            t for t in self.db.tarefas_ativas(entrada.conversa_id)
            if t.get("status") in ("ativa", "pendente")
            or t.get("checkpoint")
        ]
        if tarefas:
            resumo = []
            for t in tarefas[:3]:
                linha = (f"- tarefa {t.get('id')}: "
                         f"{t.get('objetivo', '')[:120]}")
                if t.get("proxima_acao"):
                    linha += f" | proxima: {t['proxima_acao'][:80]}"
                if t.get("checkpoint"):
                    linha += f" | checkpoint: {str(t['checkpoint'])[:300]}"
                resumo.append(linha)
            partes.append(
                "Tarefas em andamento nesta conversa:\n" + "\n".join(resumo)
            )

        # Memorias de longo prazo do dono. Falha aqui nunca derruba a
        # resposta: sem memorias, o agente responde como antes.
        try:
            dono = memoria.dono_do_prompt(getattr(entrada, "de", ""))
            if dono and hasattr(self.db, "memorias_ativas"):
                bloco = memoria.bloco_de_memorias(
                    self.db.memorias_ativas(dono), dono)
                if bloco:
                    partes.append(bloco)
                mid = getattr(entrada, "mensagem_id", "")
                if dono and mid:
                    partes.append(
                        "Id desta mensagem do dono (use em --fonte ao "
                        f"guardar uma memoria, AGENTS.md secao 15): {mid}")
        except Exception as e:  # noqa: BLE001
            log.warning("memorias indisponiveis, seguindo sem elas: %s", e)

        partes.append(ferramentas.linha_de_ferramentas())

        partes.append(
            "Converse em pt-BR. Resposta curta, de conversa humana. "
            "Zero emoji decorativo. Se a acao exigir aprovacao humana "
            "(merge, producao, anuncio, gasto), pare e peça: nao "
            "prometa que vai fazer depois."
        )
        partes.append(
            "Formato da resposta (vai para o WhatsApp): escreva SOMENTE em "
            "portugues do Brasil, sem misturar palavras ou caracteres de "
            "outros idiomas no meio das frases (so termos tecnicos, e "
            "prompts quando o dono pedir em ingles). Organize em paragrafos "
            "de 1 a 3 linhas separados por linha em branco. Listas com "
            "'- '. Negrito com *asteriscos simples*. Sem tabelas, sem "
            "titulos com '#', sem blocos de codigo a menos que o dono peça. "
            "Texto para copiar (prompt, comando, codigo) vai sozinho num "
            "bloco proprio, separado do resto, nunca no meio de uma frase."
        )
        partes.append(
            "Brevidade: resultado primeiro, em ate 6 linhas. Nao liste o "
            "que voce verificou nem conte erros intermediarios, a menos que o "
            "dono pergunte; so o que ele precisa para decidir ou usar. Uma "
            "pergunta por vez. Detalhe so se ele pedir."
        )
        return "\n\n".join(partes)

    # ------------------------------------------------------------------
    # lease
    # ------------------------------------------------------------------

    def _renovar_em_loop(self, entrada_id: str, parar: threading.Event):
        """Estica a lease enquanto a tarefa roda.

        Se nao conseguir renovar, a lease foi tomada por outra worker: o
        main aborta a tarefa em vez de deixar dois processos fazendo o
        mesmo trabalho.
        """
        while not parar.wait(self.cfg.WORKI_WORKER_LEASE_S / 3):
            try:
                if not self.db.renovar(entrada_id, self.cfg.WORKI_WORKER_LEASE_S):
                    log.warning("lease perdida em %s — abortando tarefa",
                                entrada_id)
                    parar.set()
                    return False
            except Exception as e:
                log.warning("nao consegui renovar lease %s: %s", entrada_id, e)
        return True

    # ------------------------------------------------------------------
    # aviso de andamento
    # ------------------------------------------------------------------

    def _agendar_aviso(self, entrada):
        """Agenda um "recebi" se o Hermes demorar. Devolve (timer, terminou).

        Desligado por padrao (WORKI_ACK_AFTER_SECONDS=0). Tarefas de minutos
        sem nenhum sinal fazem o usuario achar que travou; o aviso so sai se
        o Hermes ainda nao respondeu depois do prazo, entao resposta rapida
        nao gera mensagem a mais. E melhor esforco: falha no envio e ignorada
        e nunca afeta a tarefa. Fora da outbox de proposito: nao e resposta,
        e repeti-lo numa retentativa e inofensivo.

        `terminou` fecha a janela em que o timer dispara no mesmo instante em
        que a resposta fica pronta: o aviso nunca sai depois da resposta.
        """
        espera = getattr(self.cfg, "WORKI_ACK_AFTER_SECONDS", 0) or 0
        if espera <= 0 or not entrada.chat_jid:
            return None, None
        terminou = threading.Event()

        def enviar():
            if terminou.is_set():
                return
            try:
                self.ev.enviar_texto(entrada.chat_jid, MENSAGEM_AVISO)
            except Exception as e:
                log.warning("aviso de andamento nao enviado: %s", e)

        timer = threading.Timer(espera, enviar)
        timer.daemon = True
        timer.start()
        return timer, terminou

    def _concluir_falha(self, entrada, erro, session_id=None):
        """Conclui a entrada como falha e garante que o usuario saiba.

        O banco pode estar em dois estados (ver queue.concluir): a RPC cria uma
        saida com o aviso (a outbox entrega), ou nao cria nenhuma (migration
        20261003120000: outbox so no sucesso). No segundo caso o usuario ficaria
        sem resposta nenhuma, a "falha silenciosa" que o AGENTS.md s.2 proibe,
        entao o worker manda o aviso direto. Nunca nos dois: se a RPC criou a
        saida, quem entrega e a outbox.

        Melhor esforco: falha no aviso nao muda o desfecho da entrada.
        """
        saida = self.db.concluir(entrada.id, erro, session_id=session_id)
        if saida or not entrada.chat_jid:
            return
        try:
            self.ev.enviar_texto(entrada.chat_jid, db.MENSAGEM_FALHA_PADRAO)
        except Exception as e:
            log.warning("aviso de falha nao enviado: %s", e)

    # ------------------------------------------------------------------
    # uma entrada
    # ------------------------------------------------------------------

    def processar(self, entrada) -> Resultado:
        """Processa uma entrada. Nao levanta em erro de infra."""
        parar = threading.Event()
        renovador = threading.Thread(
            target=self._renovar_em_loop,
            args=(entrada.id, parar), daemon=True)
        renovador.start()

        tarefa_id = None
        try:
            contexto = self.montar_contexto(entrada)

            # 2. tarefa ANTES de executar: objetivo e proxima acao ficam
            # no banco, entao um reinicio sabe o que falta.
            if hasattr(self.db, "iniciar_entrada"):
                tarefa_id = self.db.iniciar_entrada(entrada.id)
            else:
                tarefa_id = self.db.criar_tarefa(
                    entrada.conversa_id, entrada.id, entrada.texto[:300])

            # 3. Hermes. `--resume` so com id desta conversa.
            aviso, terminou = self._agendar_aviso(entrada)
            try:
                r = self.hermes.responder(
                    entrada.texto,
                    session_id=entrada.session_id,
                    contexto=contexto,
                )
            finally:
                if aviso:
                    terminou.set()
                    aviso.cancel()

            if parar.is_set():
                return Resultado(False, None, entrada.session_id,
                                 "lease perdida durante a execucao")

            if not r.ok:
                # Preserva a sessao (r.session_id, ou a que a conversa ja
                # tinha) para o --resume da proxima mensagem continuar o
                # contexto. O log tecnico fica no servidor, nao no WhatsApp.
                self._concluir_falha(entrada, r.log or "hermes falhou",
                                     r.session_id or entrada.session_id)
                self.falhas += 1
                return Resultado(False, None, r.session_id, r.log)

            if r.texto and formato.tem_caracteres_cjk(r.texto):
                # Nao altera a resposta (apagar o trecho mudaria o sentido);
                # so deixa rastro para medir a frequencia da mistura de idioma.
                log.warning("resposta da entrada %s tem caracteres de outro "
                            "idioma (CJK); revisar modelo/temperatura", entrada.id)

            if not r.texto or not r.texto.strip():
                self._concluir_falha(entrada, "resposta vazia",
                                     r.session_id or entrada.session_id)
                return Resultado(False, None, r.session_id, "resposta vazia")

            # sessao gravada antes de qualquer outra coisa: e o que faz a
            # proxima mensagem continuar o contexto.
            if r.session_id and r.session_id != entrada.session_id:
                try:
                    self.db.salvar_sessao(entrada.conversa_id, r.session_id)
                except Exception as e:
                    # Perder o vinculo custa menos que refazer o trabalho.
                    log.warning("nao gravei session_id: %s", e)

            # 4. OUTBOX ANTES DO ENVIO. A partir daqui a retentativa so
            # reenvia: o Hermes nao roda de novo.
            if hasattr(self.db, "concluir_atomic"):
                saida_id = self.db.concluir_atomic(
                    entrada.id, r.session_id or entrada.session_id or "",
                    r.texto, True)
            else:
                saida_id = self.db.registrar_saida(
                    entrada.id, entrada.chat_jid, r.texto)

            if not saida_id:
                # Sem outbox nao ha garantia de entrega unica. Devolver
                # para a fila refaria o Hermes — que e caro, mas melhor
                # do que enviar sem registro e poder duplicar.
                self.db.concluir(entrada.id, "outbox nao gravou a saida")
                self.falhas += 1
                return Resultado(False, None, r.session_id,
                                 "registrar_saida devolveu vazio")

            if tarefa_id and not hasattr(self.db, "concluir_atomic"):
                self.db.salvar_progresso(
                    tarefa_id,
                    checkpoint=f"resposta pronta: {r.texto[:500]}",
                    resultado=r.texto,
                    status="concluida", proxima="enviar resposta",
                )

            # 5. concluir a entrada ANTES de enviar. Se cair aqui, a
            # entrega fica para o `entregar_saidas` e o Hermes nao repete.
            if not hasattr(self.db, "concluir_atomic"):
                self.db.concluir(entrada.id)

            # 6. enviar.
            estado, mid = self.ev.enviar_texto(entrada.chat_jid, r.texto)
            self.db.marcar_saida(
                saida_id,
                estado if estado != "incerto" else "incerto",
                provider_message_id=mid,
                erro=None if estado == "entregue" else estado,
            )

            if estado == ev.Estado.ENTREGUE:
                self.entregues += 1
            elif estado == ev.Estado.INCERTO:
                log.warning("saida %s incerto — aguardando reconciliacao",
                            saida_id)
            else:
                self.falhas += 1

            self.processadas += 1
            return Resultado(True, r.texto, r.session_id)

        except Exception as e:
            log.exception("erro processando %s", entrada.id)
            self.falhas += 1
            try:
                if tarefa_id:
                    self.db.cancelar_tarefa(tarefa_id, str(e)[:300])
                self._concluir_falha(entrada, str(e))
            except Exception:
                pass
            return Resultado(False, None, entrada.session_id, str(e))
        finally:
            parar.set()

    # ------------------------------------------------------------------
    # entregas pendentes
    # ------------------------------------------------------------------

    def entregar_saidas(self) -> tuple[int, int, int]:
        """Entrega o que ficou na outbox. Nao roda o Hermes.

        Devolve (entregues, falhos, incertos).
        """
        entregues = falhos = incertos = 0
        buscar_saidas = getattr(self.db, "reservar_saida", self.db.saidas_pendentes)
        for saida in buscar_saidas():
            texto = saida.get("texto") or ""
            chat = saida.get("chat_jid") or ""
            if not texto or not chat:
                self.db.marcar_saida(str(saida["id"]), "falhou",
                                     erro="registro incompleto")
                falhos += 1
                continue

            # `enviando` = ja tentamos e o processo morreu antes do
            # resultado. Nao reenviar as cegas: reconciliar.
            if saida.get("status") in ("enviando", "incerto"):
                # `enviando` = tentamos e o processo morreu antes do
                # resultado. `incerto` = timeout depois do envio.
                # Nos dois nao sabemos se saiu: reconciliar antes de
                # qualquer reenvio.
                try:
                    veredito = self.ev.reconciliar(saida)
                    if veredito == "entregue":
                        self.db.marcar_saida(
                            str(saida["id"]), "enviada",
                            provider_message_id=saida.get("provider_message_id"))
                        entregues += 1
                        continue
                    if veredito == "falhou":
                        estado, mid = self.ev.enviar_texto(chat, texto)
                    else:
                        self.db.marcar_saida(str(saida["id"]), "incerto",
                                             erro="reconciliacao inconclusiva")
                        incertos += 1
                        continue
                except Exception as e:
                    log.warning("reconciliacao falhou: %s", e)
                    self.db.marcar_saida(str(saida["id"]), "incerto",
                                         erro="reconciliacao inconclusiva")
                    incertos += 1
                    continue
            else:
                # Marca `enviando` ANTES de chamar a Evolution. Sem isso,
                # um crash no meio deixa a linha em `pendente` e a
                # proxima rodada reenvia sem tentar reconciliar — que e
                # exatamente o envio duplicado que o estado existe para
                # impedir.
                if not hasattr(self.db, "reservar_saida"):
                    try:
                        self.db.marcar_saida(str(saida["id"]), "enviando")
                    except Exception as e:
                        log.warning("nao marquei enviando: %s", e)
                estado, mid = self.ev.enviar_texto(chat, texto)

            if estado == ev.Estado.ENTREGUE:
                self.db.marcar_saida(str(saida["id"]), "enviada",
                                     provider_message_id=mid)
                entregues += 1
            elif estado == ev.Estado.INCERTO:
                self.db.marcar_saida(str(saida["id"]), "incerto",
                                     erro="timeout de rede")
                incertos += 1
            else:
                self.db.marcar_saida(str(saida["id"]), "falhou",
                                     erro="Evolution recusou")
                falhos += 1

        return entregues, falhos, incertos

    # ------------------------------------------------------------------
    # ciclo
    # ------------------------------------------------------------------

    def uma_rodada(self) -> int:
        """Uma passada: entrega pendentes, reserva e processa."""
        _tic()

        # Entregar primeiro: resposta pronta ha mais tempo e mais valor
        # para o Herickson do que um trabalho novo comecando.
        try:
            self.entregar_saidas()
        except Exception as e:
            log.warning("entrega de saidas falhou: %s", e)

        if _PARANDO.is_set():
            return 0

        entradas = self.db.reservar(limite=1,
                                    lease_s=self.cfg.WORKI_WORKER_LEASE_S)
        if not entradas:
            return 0

        for entrada in entradas:
            if _PARANDO.is_set():
                # Devolve para a fila em vez de deixar presa.
                try:
                    self.db.concluir(entrada.id, "parando")
                except Exception:
                    pass
                break
            log.info("processando entrada %s (conversa %s, sessao %s)",
                     entrada.id, entrada.conversa_id,
                     entrada.session_id or "nova")
            self.processar(entrada)
        return len(entradas)

    def rodar(self):
        """Loop principal. Sai em SIGTERM, sem deixar trabalho pela metade."""
        _tic()
        intervalo = self.cfg.WORKI_RECOVERY_POLL_SECONDS

        try:
            n = self.db.recuperar_leases()
            log.info("boot: %d lease(s) vencida(s) devolvida(s) a fila", n)
        except Exception as e:
            log.warning("nao recuperei leases no boot: %s", e)

        log.info("worker no ar: dono=%s concurrency=%d poll=%ds",
                 self.db.dono(), self.cfg.WORKI_WORKER_CONCURRENCY, intervalo)

        while not _PARANDO.is_set():
            try:
                processadas = self.uma_rodada()
            except db.FilaNaoDisponivel as e:
                # Contrato nao aplicado. Log uma vez por minuto, nao a cada
                # 3 segundos — senao o log enche de ruido.
                log.warning("fila indisponivel: %s", e)
                _PARANDO.wait(30)
                continue
            except Exception as e:
                log.exception("erro na rodada: %s", e)
                _PARANDO.wait(intervalo)
                continue

            if not processadas:
                _PARANDO.wait(intervalo)

        log.info("worker parando: %d processadas, %d falhas, %d entregues",
                 self.processadas, self.falhas, self.entregues)


def _instalar_sinais():
    """SIGTERM/SIGINT viram um Evento, nao uma morte.

    O Docker manda SIGTERM no stop. Sem handler, o processo morre no meio
    de uma tarefa e deixa a entrada presa em 'processando' ate a lease
    vencer. Com handler, ele para de pegar trabalho novo e devolve o que
    estava segurando.
    """
    def parar(signum, _frame):
        log.info("sinal %s recebido — parando", signum)
        _PARANDO.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, parar)
        except (ValueError, OSError):
            # Fora da thread principal: ignora, quem instala e o main.
            pass


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _instalar_sinais()
    try:
        cfg = Config.carregar()
    except SystemExit as e:
        log.error("configuracao incompleta: %s", e)
        raise
    Relay(cfg).rodar()


if __name__ == "__main__":
    main()
