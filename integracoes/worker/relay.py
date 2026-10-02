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
from integracoes.hermes.adapter import Adaptador  # noqa: E402
from integracoes.supabase import queue as db  # noqa: E402

log = logging.getLogger("worki.worker")

_PARANDO = threading.Event()
# Ultimo heartbeat do worker. O /ready le isto: um worker que travou sem
# morrer deixa de atualizar e o readiness acusa.
_ULTIMO_TIC = 0.0
_TIC_LOCK = threading.Lock()


def _tic():
    global _ULTIMO_TIC
    with _TIC_LOCK:
        _ULTIMO_TIC = time.time()


def esta_vivo(maxidade_s: int = 90) -> bool:
    """O worker esta rodando? Usado no /ready."""
    with _TIC_LOCK:
        if _ULTIMO_TIC == 0.0:
            return False
    return (time.time() - _ULTIMO_TIC) < maxidade_s


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

        sessao = entrada.session_id or self.db.sessao_da_conversa(
            entrada.conversa_id)
        if sessao:
            partes.append(
                f"Voce esta retomando a sessao {sessao} desta conversa. "
                f"O contexto anterior esta nela — nao comece do zero."
            )

        tarefas = self.db.tarefas_ativas(entrada.conversa_id)
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

        partes.append(
            "Converse em pt-BR. Resposta curta, de conversa humana. "
            "Zero emoji decorativo. Se a acao exigir aprovacao humana "
            "(merge, producao, anuncio, gasto), pare e peça: nao "
            "prometa que vai fazer depois."
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
            r = self.hermes.responder(
                entrada.texto,
                session_id=entrada.session_id,
                contexto=contexto,
            )

            if parar.is_set():
                return Resultado(False, None, entrada.session_id,
                                 "lease perdida durante a execucao")

            if not r.ok:
                self.db.concluir(entrada.id, r.log or "hermes falhou")
                self.falhas += 1
                return Resultado(False, None, r.session_id, r.log)

            if not r.texto or not r.texto.strip():
                self.db.concluir(entrada.id, "resposta vazia")
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
                self.db.concluir(entrada.id, str(e))
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
