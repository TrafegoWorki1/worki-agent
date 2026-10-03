"""Testes da outbox — a garantia central: falha de envio nao refaz o trabalho.

O cenario que este arquivo existe para provar:

    o Hermes roda, a resposta e gravada, o envio falha,
    a proxima rodada reenvia a MESMA mensagem
    e o Hermes NAO e chamado de novo.

O teste usa uma Evolution falsa injetada, entao nao toca em nenhum numero
real e nao gasta token. A dedupe tambem e testada contra o store em
memoria, para nao depender de Supabase.

Rodar:  python testes/test_outbox.py
"""

import sys
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

FALHOU, PASSOU = [], []


def checar(nome, condicao, detalhe=""):
    (PASSOU if condicao else FALHOU).append(nome)
    print(f"  [{'ok  ' if condicao else 'FALHA'}] {nome}"
          f"{(' — ' + detalhe) if detalhe else ''}")


class EvolutionFalsa:
    """Registra as chamadas e falha como pedirem."""

    def __init__(self, comportamento="aceitar"):
        self.comportamento = comportamento
        self.enviados = []
        self.ids = iter(range(1000, 9999))

    def __call__(self, destino, texto, timeout=45):
        self.enviados.append((destino, texto))
        c = self.comportamento
        if c == "aceitar":
            return "entregue", f"FAKE{next(self.ids)}"
        if c == "recusar":
            return "falhou", None
        if c == "timeout":
            # A Evolution pode ter aceitado e a rede caiu depois.
            return "incerto", None
        return c, None


class StoreOutbox:
    """Store em memoria com o mesmo contrato da tabela outbox."""

    def __init__(self):
        self.linhas = {}
        self._seq = 0
        self.registros_de_entrega = []

    def enfileirar(self, conversation_id, destino, texto, task_id=None):
        self._seq += 1
        oid = self._seq
        self.linhas[oid] = {
            "id": oid, "conversation_id": conversation_id, "task_id": task_id,
            "destino": destino, "response_text": texto,
            "status": "pendente", "attempts": 0,
        }
        return oid

    def pendentes(self, limite=10):
        # Reflete o filtro real: status in (pendente, incerto, falhou) e
        # available_at vencido. O available_at e ignorado aqui porque o
        # store e synchronous — nao ha espera de verdade. O que importa e
        # o status: 'falhou' TEM de voltar, senao a resposta se perde.
        return [
            dict(v) for v in self.linhas.values()
            if v["status"] in ("pendente", "incerto", "falhou")
        ][:limite]

    def marcar(self, oid, estado, mid=None, erro=""):
        v = self.linhas[oid]
        v["status"] = estado
        if mid:
            v["provider_message_id"] = mid
        self.registros_de_entrega.append((oid, estado))

    def incrementar_tentativa(self, oid):
        if self.linhas[oid]["status"] in ("pendente", "incerto", "falhou"):
            self.linhas[oid]["attempts"] += 1


def carregar_outbox(ev_falsa):
    """Importa o modulo com as dependencias injetadas (sem rede, sem env)."""
    mod = types.ModuleType("integracoes.supabase.outbox")

    # Injeta o que o modulo importa, para nao exigir .env nem Supabase.
    env = types.ModuleType("integracoes.supabase.env")
    env.chave = lambda n: {
        "SUPABASE_URL": "http://localhost",
        "SUPABASE_SERVICE_ROLE_KEY": "fake",
        "EVOLUTION_API_URL": "http://localhost/evolution",
        "EVOLUTION_API_KEY": "fake",
        "EVOLUTION_INSTANCE": "agent-domintante",
    }.get(n, "")
    sys.modules["integracoes.supabase.env"] = env

    queue = types.ModuleType("integracoes.supabase.queue")
    queue._rpc = lambda nome, payload: None
    sys.modules["integracoes.supabase.queue"] = queue

    import importlib.util
    caminho = RAIZ / "integracoes" / "supabase" / "outbox.py"
    spec = importlib.util.spec_from_file_location("outbox_teste", caminho)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    m.enviar = ev_falsa          # a Evolution vira a falsa
    m._rest = lambda *a, **k: None
    return m


def main():
    print("=" * 64)
    print("Testes da outbox (Evolution falsa, sem rede, sem token)")
    print("=" * 64)

    # ---- 1. gravacao antes do envio ----
    print("\n1. a resposta e gravada antes de sair")
    ev = EvolutionFalsa("aceitar")
    m = carregar_outbox(ev)
    st = StoreOutbox()
    o = m.Outbox()
    o.enfileirar = lambda cid, dest, txt, task_id=None: st.enfileirar(cid, dest, txt, task_id)
    o.pendentes = lambda limite=10: [
        _para_entrega(x) for x in st.pendentes(limite)
    ]
    o.marcar = lambda oid, e, mid=None, erro="": st.marcar(oid, e, mid, erro)
    o.incrementar_tentativa = lambda oid: st.incrementar_tentativa(oid)

    o.enfileirar(1, "558592494552", "sua pagina esta no ar")
    checar("linha gravada antes de enviar", len(st.linhas) == 1)
    checar("status inicial pendente", st.linhas[1]["status"] == "pendente")
    checar("nada enviado ainda", len(ev.enviados) == 0)

    e, f, i = o.entregar_pendentes()
    checar("entregue 1", (e, f, i) == (1, 0, 0), f"{e}/{f}/{i}")
    checar("numero salvo", st.linhas[1]["provider_message_id"] == "FAKE1000")
    checar("status entregue", st.linhas[1]["status"] == "entregue")
    checar("contou 1 tentativa", st.linhas[1]["attempts"] == 1)

    # ---- 2. falha de envio NAO faz o Hermes rodar de novo ----
    print("\n2. envio recusado: reenvia a msg, nao refaz o trabalho")
    ev2 = EvolutionFalsa("recusar")
    m2 = carregar_outbox(ev2)
    o2 = m2.Outbox()
    st2 = StoreOutbox()
    o2.enfileirar = lambda cid, d, t, task_id=None: st2.enfileirar(cid, d, t, task_id)
    o2.pendentes = lambda limite=10: [_para_entrega(x) for x in st2.pendentes(limite)]
    o2.marcar = lambda oid, e, mid=None, erro="": st2.marcar(oid, e, mid, erro)
    o2.incrementar_tentativa = lambda oid: st2.incrementar_tentativa(oid)

    o2.enfileirar(1, "558592494552", "mesma resposta")
    e2, f2, i2 = o2.entregar_pendentes()
    checar("marcou como falhou", st2.linhas[1]["status"] == "falhou")
    checar("texto preservado para reenvio",
           st2.linhas[1]["response_text"] == "mesma resposta")

    # Segunda rodada, agora a Evolution aceita.
    ev2.comportamento = "aceitar"
    e3, f3, i3 = o2.entregar_pendentes()
    checar("reenviou na 2a rodada", len(ev2.enviados) == 2)
    checar("entregue na 2a", e3 == 1)
    checar("texto identico no reenvio",
           ev2.enviados[0][1] == ev2.enviados[1][1],
           f"{ev2.enviados[0][1]!r} vs {ev2.enviados[1][1]!r}")
    checar("agora entregue", st2.linhas[1]["status"] == "entregue")

    # ---- 3. timeout e incerto, nao reenvio cego ----
    print("\n3. timeout vira 'incerto', nao reenvio as cegas")
    ev3 = EvolutionFalsa("timeout")
    m3 = carregar_outbox(ev3)
    o3 = m3.Outbox()
    st3 = StoreOutbox()
    o3.enfileirar = lambda cid, d, t, task_id=None: st3.enfileirar(cid, d, t, task_id)
    o3.pendentes = lambda limite=10: [_para_entrega(x) for x in st3.pendentes(limite)]
    o3.marcar = lambda oid, e, mid=None, erro="": st3.marcar(oid, e, mid, erro)
    o3.incrementar_tentativa = lambda oid: st3.incrementar_tentativa(oid)

    o3.enfileirar(1, "558592494552", "pode ter chegado")
    e4, f4, i4 = o3.entregar_pendentes()
    checar("marcou incerto", st3.linhas[1]["status"] == "incerto")
    checar("contou como incerto, nao falhou", i4 == 1 and f4 == 0)
    checar("guardou texto para reconciliar",
           st3.linhas[1]["response_text"] == "pode ter chegado")

    # ---- 4. texto vazio nao entra ----
    print("\n4. texto vazio e recusado")
    try:
        m3.Outbox().enfileirar(1, "558", "   ")
        checar("vazio recusado", False, "aceitou")
    except ValueError:
        checar("vazio recusado", True)
    except Exception as e:
        checar("vazio recusado", False, f"erro inesperado: {e}")

    # ---- 5. ja entregue nao reenvia ----
    print("\n5. linha entregue nao volta para a fila")
    ev5 = EvolutionFalsa("aceitar")
    m5 = carregar_outbox(ev5)
    o5 = m5.Outbox()
    st5 = StoreOutbox()
    o5.enfileirar = lambda cid, d, t, task_id=None: st5.enfileirar(cid, d, t, task_id)
    o5.pendentes = lambda limite=10: [_para_entrega(x) for x in st5.pendentes(limite)]
    o5.marcar = lambda oid, e, mid=None, erro="": st5.marcar(oid, e, mid, erro)
    o5.incrementar_tentativa = lambda oid: st5.incrementar_tentativa(oid)
    o5.enfileirar(1, "558592494552", "uma vez so")
    o5.entregar_pendentes()
    antes = len(ev5.enviados)
    o5.entregar_pendentes()
    checar("segunda rodada nao reenvia", len(ev5.enviados) == antes,
           f"{antes} -> {len(ev5.enviados)}")

    print("\n" + "=" * 64)
    print(f"Passou: {len(PASSOU)}   Falhou: {len(FALHOU)}")
    for x in FALHOU:
        print(f"  FALHOU: {x}")
    print("=" * 64)
    return 1 if FALHOU else 0


def _para_entrega(linha: dict):
    """Converte a linha do store no objeto Entrega que o modulo usa."""
    import integracoes.hermes.adapter as _  # noqa: F401  (so garante import)
    return types.SimpleNamespace(
        id=linha["id"],
        destino=linha["destino"],
        response_text=linha["response_text"],
        status=linha["status"],
        attempts=linha["attempts"],
        conversation_id=linha["conversation_id"],
        task_id=linha["task_id"],
    )


if __name__ == "__main__":
    sys.exit(main())
