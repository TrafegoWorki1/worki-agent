"""A limpeza do stdout do Hermes preserva paragrafos e remove so o ruido.

Bug corrigido: o padrao de ruido tinha `\\s*$`, que casa com linha vazia, e
`_limpar` apagava todas as linhas em branco da resposta. Os paragrafos
chegavam ao WhatsApp encostados, numa parede de texto.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integracoes.hermes.adapter import _limpar  # noqa: E402


def test_preserva_linhas_em_branco_entre_paragrafos():
    bruto = "Primeiro paragrafo.\n\nSegundo paragrafo.\n\nTerceiro."
    assert _limpar(bruto) == bruto


def test_remove_so_o_ruido_de_terminal():
    bruto = "↪ restored workspace dir: C:\\x\n│ banner\n\nA resposta.\n\nOutra linha."
    assert _limpar(bruto) == "A resposta.\n\nOutra linha."


def test_colapsa_tres_ou_mais_quebras_em_duas():
    assert _limpar("a\n\n\n\n\nb") == "a\n\nb"


def test_tira_espaco_no_fim_das_linhas_e_nas_pontas():
    assert _limpar("\n\n  a  \nb   \n\n") == "a\nb"


def test_vazio():
    assert _limpar("") == ""
    assert _limpar("   \n\n  ") == ""
