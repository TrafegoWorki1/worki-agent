"""Testes do guard de aprovacao.

Cobre os casos que o blueprint chama de obrigatorios:

- producao bloqueada sem aprovacao
- aprovacao de outra tarefa rejeitada
- palavra ambigua com duas pendencias
- mudanca material invalida a aprovacao
- aprovacao consumida uma vez so
- expiracao por prazo
- palavra nao relacionada nao aprova

Rodar:  python testes/test_guard.py        (nao chama Hermes, nao gasta token)
"""

import sys
from datetime import timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from integracoes.aprovacao.guard import (  # noqa: E402
    Ambiguo,
    AguardandoAprovacao,
    Bloqueado,
    Guard,
    agora,
    hash_artefato,
)

FALHOU, PASSOU = [], []


def checar(nome, condicao, detalhe=""):
    (PASSOU if condicao else FALHOU).append(nome)
    print(f"  [{'ok  ' if condicao else 'FALHA'}] {nome}"
          f"{(' — ' + detalhe) if detalhe else ''}")


class StoreMemoria:
    """Store em memoria, mesmo contrato do cliente Supabase."""

    def __init__(self):
        self.itens = []
        self._n = 0

    def listar(self, task_id="", acao=None):
        return [
            a for a in self.itens
            if (not task_id or a.metadata.get("task_id") == task_id)
            and (acao is None or a.acao == acao)
        ]

    def registrar_pendente(self, ap, conversation_id=""):
        self._n += 1
        ap.id = f"ap{self._n}"
        ap.status = "pendente"
        self.itens.append(ap)

    def salvar(self, ap, conversation_id=""):
        self.itens = [a for a in self.itens if a.id != ap.id]
        self.itens.append(ap)

    def consumir(self, ap_id, conversation_id="", acao="", alvo="", payload_hash=""):
        for a in self.itens:
            if a.id == ap_id:
                a.status = "consumida"
                return True
        return False


def main():
    print("=" * 62)
    print("Testes do guard de aprovacao (sem token, sem banco)")
    print("=" * 62)

    # ---- 1. producao bloqueada sem aprovacao ----
    print("\n1. acao protegida exige aprovacao")
    st = StoreMemoria()
    g = Guard(store=st)
    try:
        g.exigir("deploy_producao", "https://x.vercel.app", "v1 do bundle")
        checar("producao bloqueada sem aprovacao", False, "NAO LEVANTOU — falha grave")
    except AguardandoAprovacao as e:
        checar("producao bloqueada sem aprovacao", True)
        checar("pedido cita a palavra certa", "sobe" in e.pedido, e.pedido[:40])
    checar("pendencia registrada", len(st.itens) == 1)

    # ---- 2. palavra sozinha aprova a pendencia ----
    print("\n2. 'sobe' aprova a acao pendente")
    ap = g.aprovar_palavra("sobe", task_id="")
    checar("aprovacao concedida", ap.status == "aprovada")
    checar("aprovada por herickson", ap.approved_by == "herickson")
    g.exigir("deploy_producao", "https://x.vercel.app", "v1 do bundle")
    checar("agora passa", True, "exigir nao levantou")

    # ---- 3. aprovacao consumida uma vez so ----
    print("\n3. aprovacao vale uma vez")
    st2 = StoreMemoria()
    g2 = Guard(store=st2)
    try:
        g2.exigir("merge", "PR #12", "commit abc")
    except AguardandoAprovacao:
        pass
    g2.aprovar_palavra("aprova", task_id="")
    g2.exigir("merge", "PR #12", "commit abc")  # consome
    try:
        g2.exigir("merge", "PR #12", "commit abc")
        checar("segunda vez bloqueada", False, "reaproveitou a mesma")
    except AguardandoAprovacao:
        checar("segunda vez bloqueada", True)

    # ---- 4. aprovacao de outra tarefa nao vale ----
    print("\n4. aprovacao e vinculada ao alvo")
    # Duas conversas separadas, como seriam duas tarefas. Aqui nao pode
    # haver duas pendencias ao mesmo tempo: o guard recusa palavra solta
    # nesse caso (teste 6), que e o comportamento certo.
    st3a, st3b = StoreMemoria(), StoreMemoria()
    g3a, g3b = Guard(store=st3a), Guard(store=st3b)
    try:
        g3a.exigir("merge", "PR #12", "commit abc")
    except AguardandoAprovacao:
        pass
    try:
        g3b.exigir("merge", "PR #13", "commit abc")
    except AguardandoAprovacao:
        pass
    g3a.aprovar_palavra("aprova", task_id="")
    g3a.exigir("merge", "PR #12", "commit abc")  # consome a do 12
    checar("PR #12 liberado", True)
    try:
        # A conversa do PR #13 tem a propria pendencia, nao a do #12.
        g3b.exigir("merge", "PR #13", "commit abc")
        checar("PR #13 nao usa aprovacao do #12", False, "vazou")
    except AguardandoAprovacao:
        checar("PR #13 nao usa aprovacao do #12", True)

    # Mesmo store, alvo diferente: a aprovacao do #12 nao cobre o #13.
    st3c = StoreMemoria()
    g3c = Guard(store=st3c)
    try:
        g3c.exigir("merge", "PR #12", "commit abc")
    except AguardandoAprovacao:
        pass
    g3c.aprovar_palavra("aprova", task_id="")
    g3c.exigir("merge", "PR #12", "commit abc")
    try:
        g3c.exigir("merge", "PR #99", "commit abc")
        checar("outro alvo nao passa", False, "vazou")
    except AguardandoAprovacao:
        checar("outro alvo nao passa", True)

    # ---- 5. mudanca material invalida ----
    print("\n5. artefato que muda invalida a aprovacao")
    st4 = StoreMemoria()
    g4 = Guard(store=st4)
    try:
        g4.exigir("merge", "PR #20", "versao original do codigo")
    except AguardandoAprovacao:
        pass
    ap4 = g4.aprovar_palavra("aprova", task_id="")
    try:
        # O agente mudou o arquivo depois da aprovacao.
        g4.exigir("merge", "PR #20", "versao MODIFICADA do codigo")
        checar("mudanca material bloqueia", False, "aprovou o que mudou")
    except AguardandoAprovacao:
        checar("mudanca material bloqueia", True)
    ok, motivo = ap4.valida("merge", "PR #20", hash_artefato("outra coisa"))
    checar("motivo correto", "artefato mudou" in motivo, motivo)

    # ---- 6. palavra ambigua ----
    print("\n6. palavra solta com duas pendencias")
    st5 = StoreMemoria()
    g5 = Guard(store=st5)
    for alvo in ("PR #30", "PR #31"):
        try:
            g5.exigir("merge", alvo, "c")
        except AguardandoAprovacao:
            pass
    try:
        g5.aprovar_palavra("aprova", task_id="")
        checar("duas pendencias recusa", False, "escolheu sozinho")
    except Ambiguo as e:
        checar("duas pendencias recusa", True)
        checar("lista as duas", len(e.pendentes) == 2, str(e.pendentes))

    # ---- 7. palavra errada ----
    print("\n7. palavra que nao cobre a acao")
    st6 = StoreMemoria()
    g6 = Guard(store=st6)
    try:
        g6.exigir("deploy_producao", "site", "c")
    except AguardandoAprovacao:
        pass
    try:
        g6.aprovar_palavra("aprova", task_id="")   # aprova merge, nao deploy
        checar("'aprova' nao sobe producao", False, "vazou")
    except Bloqueado as e:
        checar("'aprova' nao sobe producao", True, e.motivo[:50])

    # ---- 8. palavra sem pendencia ----
    print("\n8. palavra solta sem nada pendente")
    try:
        Guard(store=StoreMemoria()).aprovar_palavra("sobe", task_id="")
        checar("palavra solta nao cria poder", False)
    except Bloqueado as e:
        checar("palavra solta nao cria poder", True, e.motivo[:40])

    # ---- 9. expiracao ----
    print("\n9. aprovacao expirada nao vale")
    st7 = StoreMemoria()
    g7 = Guard(store=st7)
    try:
        g7.exigir("merge", "PR #40", "c")
    except AguardandoAprovacao as e:
        pass
    ap7 = g7.aprovar_palavra("aprova", task_id="")
    ap7.expires_at = agora() - timedelta(minutes=1)   # passa do prazo
    ok, motivo = ap7.valida("merge", "PR #40", hash_artefato("c"))
    checar("expirada rejeitada", not ok, motivo)
    checar("motivo e expiracao", "expirada" in motivo, motivo)

    # ---- 10. limpeza de expiradas ----
    print("\n10. expirar() limpa as vencidas")
    st8 = StoreMemoria()
    g8 = Guard(store=st8)
    try:
        g8.exigir("merge", "PR #50", "c")
    except AguardandoAprovacao:
        pass
    ap8 = g8.aprovar_palavra("aprova", task_id="")
    ap8.expires_at = agora() - timedelta(minutes=1)
    n = g8.expirar()
    checar("contou 1 expirada", n == 1, str(n))
    checar("status virou expirada", ap8.status == "expirada")

    # ---- 11. case e pontuacao ----
    print("\n11. palavra com caixa e pontuacao")
    for forma in ("APROVA", "aprova!", "  aprova.  "):
        try:
            Guard(store=StoreMemoria()).aprovar_palavra(forma, task_id="")
            ch = False
        except Bloqueado as e:
            ch = "nao ha acao pendente" in e.motivo
        checar(f"'{forma}' reconhecida", ch)

    print("\n" + "=" * 62)
    print(f"Passou: {len(PASSOU)}   Falhou: {len(FALHOU)}")
    for f in FALHOU:
        print(f"  FALHOU: {f}")
    print("=" * 62)
    return 1 if FALHOU else 0


if __name__ == "__main__":
    sys.exit(main())
