"""Formatacao para WhatsApp: conversao de Markdown, divisao e deteccao de CJK."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.evolution import formato  # noqa: E402


def test_negrito_e_titulo_viram_dialeto_do_whatsapp():
    assert formato.para_whatsapp("Faço **bem** isso") == "Faço *bem* isso"
    assert formato.para_whatsapp("# Resumo honesto") == "*Resumo honesto*"
    assert formato.para_whatsapp("### Título 3") == "*Título 3*"


def test_marcadores_viram_bolinha():
    entrada = "- um\n* dois\n+ tres"
    assert formato.para_whatsapp(entrada) == "• um\n• dois\n• tres"


def test_numeracao_e_texto_com_hifen_no_meio_nao_mudam():
    assert formato.para_whatsapp("1. primeiro\n2. segundo") == "1. primeiro\n2. segundo"
    assert formato.para_whatsapp("guarda-chuva e pré-venda") == "guarda-chuva e pré-venda"


def test_link_markdown_vira_texto_com_url():
    assert formato.para_whatsapp("veja [a doc](https://x.com/a)") == "veja a doc (https://x.com/a)"


def test_bloco_de_codigo_fica_intacto():
    entrada = "antes **a**\n\n```\n**nao mexe**\n- nem isso\n```\n\ndepois **b**"
    saida = formato.para_whatsapp(entrada)
    assert "```\n**nao mexe**\n- nem isso\n```" in saida
    assert saida.startswith("antes *a*") and saida.endswith("depois *b*")


def test_preserva_paragrafos_e_colapsa_excesso():
    assert formato.para_whatsapp("a\n\n\n\n\nb") == "a\n\nb"
    assert formato.para_whatsapp("a\n\nb") == "a\n\nb"


def test_texto_curto_e_vazio():
    assert formato.preparar("Oi. Tô aqui.") == ["Oi. Tô aqui."]
    assert formato.preparar("") == []
    assert formato.preparar("   \n  ") == []


def test_divide_em_limite_de_paragrafo_sem_perder_nada():
    paragrafos = [f"Paragrafo {i}: " + " ".join(["palavra"] * 40) for i in range(1, 9)]
    texto = "\n\n".join(paragrafos)
    partes = formato.dividir(texto, 700)
    assert len(partes) > 1
    assert all(len(p) <= 700 for p in partes)
    # cada parte e feita de paragrafos inteiros, na ordem, sem perda
    assert "\n\n".join(partes) == texto


def test_paragrafo_gigante_quebra_em_fim_de_frase():
    texto = ("Esta e uma frase de teste. " * 120).strip()
    partes = formato.dividir(texto, 500)
    assert all(len(p) <= 500 for p in partes)
    assert all(p.endswith(".") for p in partes)
    assert " ".join(partes) == texto


def test_limite_zero_desliga_a_divisao():
    texto = "a\n\n" * 2000
    assert len(formato.dividir(texto, 0)) == 1


def test_bloco_de_codigo_nao_e_partido_se_cabe():
    codigo = "```\n" + "\n".join(f"linha {i}" for i in range(20)) + "\n```"
    texto = "Intro curta.\n\n" + codigo + "\n\n" + ("x " * 600)
    partes = formato.dividir(texto, 900)
    assert any(codigo in p for p in partes)


def test_deteccao_de_cjk():
    assert formato.tem_caracteres_cjk("uma限制 real")
    assert formato.tem_caracteres_cjk("em英文 mesmo")
    assert not formato.tem_caracteres_cjk("Tudo certo, só português. Ação, coração.")
    assert not formato.tem_caracteres_cjk("")
