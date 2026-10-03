"""Ambiente padrao dos testes.

`Config.carregar()` exige variaveis reais e encerra o processo (SystemExit)
quando faltam. Sem estes defaults, `pytest` puro falhava em
test_fechamento.py::testar_receptor e ::testar_worker, e quem rodava os
testes precisava exportar cinco variaveis na mao.

`setdefault` nunca sobrescreve: se o ambiente ja tem um valor (CI, .env
carregado), ele vale. Os valores abaixo sao ficticios e nao falam com nada.
"""
import os

for chave, valor in {
    "EVOLUTION_API_URL": "http://evolution.invalid",
    "EVOLUTION_API_KEY": "chave-de-teste",
    "SUPABASE_URL": "http://supabase.invalid",
    "SUPABASE_SERVICE_ROLE_KEY": "chave-de-teste",
    "WHATSAPP_NUMERO_AUTORIZADO": "558592494552",
    "WORKI_WEBHOOK_SECRET": "segredo-de-teste",
}.items():
    os.environ.setdefault(chave, valor)
