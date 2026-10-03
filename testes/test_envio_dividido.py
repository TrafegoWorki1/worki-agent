"""Envio de resposta formatada e dividida (integracoes/evolution/cliente.py).

Garantias:
1. texto curto: uma unica chamada, ja convertido para o dialeto do WhatsApp;
2. texto longo: varias chamadas, em ordem, e o id devolvido e o da primeira;
3. a primeira parte falhou: devolve o estado dela e NAO envia o resto
   (nada saiu, pode reenviar tudo);
4. uma parte seguinte falhou: devolve ENTREGUE e para (reenviar tudo repetiria
   o comeco para o usuario);
5. o reconciliador compara com o texto JA formatado: sem isso, "**negrito**"
   virava "*negrito*" no WhatsApp, o prefixo nao batia e a resposta seria
   reenviada em duplicata.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.evolution import cliente  # noqa: E402

E = cliente.Estado
CHAT = "5585@s.whatsapp.net"


def _gravar_envios(monkeypatch, estados=None):
    """Troca o envio real por um gravador. `estados`: um por chamada, em ordem."""
    chamadas = []
    fila = list(estados or [])

    def falso(destino, texto, timeout=45):
        chamadas.append((destino, texto))
        estado = fila.pop(0) if fila else E.ENTREGUE
        return estado, (f"id{len(chamadas)}" if estado == E.ENTREGUE else None)

    monkeypatch.setattr(cliente, "_enviar_uma", falso)
    monkeypatch.setattr(cliente.time, "sleep", lambda s: None)
    monkeypatch.setattr(cliente, "_limite", lambda: 700)
    return chamadas


def _texto_longo():
    return "\n\n".join(f"Paragrafo {i}: " + " ".join(["palavra"] * 40) for i in range(1, 7))


def test_texto_curto_vai_numa_chamada_ja_convertido(monkeypatch):
    chamadas = _gravar_envios(monkeypatch)
    estado, mid = cliente.enviar_texto(CHAT, "Faço **bem** isso")
    assert (estado, mid) == (E.ENTREGUE, "id1")
    assert [t for _, t in chamadas] == ["Faço *bem* isso"]


def test_texto_longo_vira_varias_mensagens_em_ordem(monkeypatch):
    chamadas = _gravar_envios(monkeypatch)
    estado, mid = cliente.enviar_texto(CHAT, _texto_longo())
    assert estado == E.ENTREGUE and mid == "id1"
    assert len(chamadas) > 1
    assert all(len(t) <= 700 for _, t in chamadas)
    assert "\n\n".join(t for _, t in chamadas) == _texto_longo()
    assert chamadas[0][1].startswith("Paragrafo 1:")


def test_primeira_parte_falhou_nao_envia_o_resto(monkeypatch):
    chamadas = _gravar_envios(monkeypatch, [E.FALHOU])
    estado, mid = cliente.enviar_texto(CHAT, _texto_longo())
    assert (estado, mid) == (E.FALHOU, None)
    assert len(chamadas) == 1


def test_primeira_parte_incerta_nao_envia_o_resto(monkeypatch):
    chamadas = _gravar_envios(monkeypatch, [E.INCERTO])
    estado, _ = cliente.enviar_texto(CHAT, _texto_longo())
    assert estado == E.INCERTO
    assert len(chamadas) == 1


def test_parte_seguinte_falhou_devolve_entregue_e_para(monkeypatch):
    chamadas = _gravar_envios(monkeypatch, [E.ENTREGUE, E.FALHOU])
    estado, mid = cliente.enviar_texto(CHAT, _texto_longo())
    assert (estado, mid) == (E.ENTREGUE, "id1")
    assert len(chamadas) == 2  # parou na que falhou, nao repetiu a primeira


def test_texto_vazio_nao_envia_nada(monkeypatch):
    chamadas = _gravar_envios(monkeypatch)
    assert cliente.enviar_texto(CHAT, "   ") == (E.FALHOU, None)
    assert chamadas == []


def _mensagens_do_chat(corpo):
    return [{"key": {"fromMe": True, "id": "x"}, "message": {"conversation": corpo}}]


def test_reconciliador_acha_o_texto_ja_formatado(monkeypatch):
    # No WhatsApp ficou "*Resumo*" (convertido); no banco esta "**Resumo**".
    monkeypatch.setattr(cliente, "_limite", lambda: 1500)
    monkeypatch.setattr(cliente, "listar_mensagens",
                        lambda limite=20: _mensagens_do_chat("*Resumo honesto do que eu faço*\n\n• ler codigo"))
    saida = {"texto": "**Resumo honesto do que eu faço**\n\n- ler codigo"}
    assert cliente.reconciliar(saida) == "entregue"


def test_reconciliador_continua_dizendo_falhou_quando_nao_saiu(monkeypatch):
    monkeypatch.setattr(cliente, "_limite", lambda: 1500)
    monkeypatch.setattr(cliente, "listar_mensagens",
                        lambda limite=20: _mensagens_do_chat("outra conversa qualquer"))
    assert cliente.reconciliar({"texto": "**Resumo honesto do que eu faço**"}) == "falhou"


def test_reconciliador_usa_a_primeira_parte_de_texto_dividido(monkeypatch):
    monkeypatch.setattr(cliente, "_limite", lambda: 700)
    primeira = cliente.formato.preparar(_texto_longo(), 700)[0]
    monkeypatch.setattr(cliente, "listar_mensagens", lambda limite=20: _mensagens_do_chat(primeira))
    assert cliente.reconciliar({"texto": _texto_longo()}) == "entregue"
