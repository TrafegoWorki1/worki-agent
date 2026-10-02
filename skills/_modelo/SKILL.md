---
name: nome-da-skill
description: "QUANDO USAR. Uma frase com o gatilho e o que a skill faz."
version: 1.0.0
---

# Nome da skill

## Quando usar

Descreva o gatilho. O agente decide sozinho se esta skill se aplica — se a
descricao nao deixar isso claro, ele nao vai chamar.

Exemplo: "Usar quando o usuario pergunta como esta o grupo de lancamento
('como esta o grupo?'). Consulta os ultimos snapshots e responde com
numeros."

Nao usar para: (delimite o que esta skill NAO faz)

## Entradas

| Campo | Origem | Exemplo |
|---|---|---|
| `grupo_jid` | env `WHATSAPP_GRUPO_COMANDO_JID` | `5585...@g.us` |
| `janela` | parametro | `7d` |

## Passos

1. Verificar allowlist (AGENTS.md secao 2). Se nao autorizado, PARE.
2. Consultar...
3. Calcular...
4. Responder...

Cada passo numerado e verificavel. Se um passo falha, parar e reportar
qual falhou — nao seguir adiante com dado parcial.

## Ferramentas

| Ferramenta | Para que |
|---|---|
| `supabase` | leitura em `grupos_snapshots` |
| `curl` | API da Evolution, se necessario |

## Exige aprovacao?

SIM / NAO

Se SIM: qual palavra de confirmacao (`aprova`, `confirma`, `sobe`)?

Se NAO: por que e seguro fazer direto?

## Limites

- Nao leia mais de N registros por execucao: `<N>`
- Nao rode mais que 1 vez por `<periodo>`
- Custo maximo por execucao: `<valor>`

## Formato da resposta

WhatsApp — texto curto, sem markdown, sem emoji decorativo:

```
Grupo: 342 membros
Ultimas 24h: +7 (-2 saidas)
Comparado a 7d atras: +18
```

Terminal — texto puro, indentacao e linha em branco (markdown nao renderiza
no CLI):

```
Grupo: 342 membros (+7 em 24h, +18 em 7d)
Maior queda: 12 de marco, -23
```

## Testes

O comando que prova que a skill funciona:

```bash
# comando aqui
```

E o resultado esperado quando roda certo:

```
# saida esperada
```

**Teste que a skill PRECISA falhar:** descreva um caso que deve dar erro,
para saber que o teste nao esta verde por acidente.

```
# caso que deve falhar
```

## Limites conhecidos

Coisas que esta skill faz mal. Anote aqui — e o que evita o proxima
pessoa (ou o proximo agente) de confiar demais.

- `<exemplo: "acerta ate 3 dias de janela, errar com mais">`