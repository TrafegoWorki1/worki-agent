# Laya — o que foi testado, o que funciona, o que não funciona

Registro do trabalho de instalar e testar o Laya (fork aberto do Jev da
TypeSafe) como ferramenta de classificação para o Hermes.

Data: 2026-10-02
Máquina: Windows 11, CPU, sem GPU usada
Executável: `laya.exe` 0.3.23 (uv tool, `laya[mcp]`)

---

## TL;DR

**Não usar o Laya checkpoint base para triar lead do Hunter.** Medido: 25%
de acerto (10/40) em 40 casos rotulados, com padrão de falha bem definido
(ver "Benchmark Hunter" abaixo). Os números de confiança não servem como
sinal de dúvida — ele erra com 0.908 de confiança.

O Laya está instalado, funcional e rápido (1,175 ms/msg, offline, custo
zero). O problema é qualidade no uso pretendido, não instalação.

---

## O que é

Modelo de decisão sem geração de texto: recebe um *state* (texto) e perguntas
tipadas (`choice` / `score` / `noul`), devolve probabilidades. É a alternativa
aberta ao Jev, que é API hospedada e paga da TypeSafe.

**Não estamos usando o Jev.** O Jev é hospedado, custa por chamada, e nunca
foi instalado aqui. O número 0.766 que circula e que o vídeo do YouTube cita
pertence ao checkpoint `laya-typed-decisions` (fine-tuned), não ao base.

---

## Instalação (o que ficou no disco)

### Checkpoints

| Checkpoint | Tamanho | Contexto | Uso |
|---|---|---|---|
| `convaiinnovations/laya` (inglês) | 842 MB | 512 tok | base |
| `convaiinnovations/laya/multilingual` | 644 MB | 1024 tok | **é o que responde pt-BR** |

Caminho: `C:\Users\Samsung\.cache\huggingface\hub\models--convaiinnovations--laya\`
(snapshot `c5d78730f3493e4fe16d61507ef4b78eef7318cf`)

O `laya-typed-decisions` (o único fine-tuned) **não** foi baixado.

### Hermes

MCP habilitado no `config.yaml` com 8 tools:

```yaml
mcp_servers:
  laya:
    command: C:/Users/Samsung/.local/bin/laya-mcp-server.exe
    env:
      LAYA_DEVICE: cpu
    connect_timeout: 300   # OBRIGATORIO: default 60 estoura
    timeout: 300
    enabled: true
```

O `hermes mcp add` **não expõe** `connect_timeout` — só dá para setar com
`hermes config set mcp_servers.laya.connect_timeout 300`.

Conexão leva **~285 segundos** (carrega 1,5 GB de pesos na memória).
Reconecta a cada reload, então o custo é recorrente.

### GitHub

Duas contas, uma chave SSH cada (o GitHub identifica a conta pela chave):

- `github.com-trafego` → `~/.ssh/id_ed25519_trafego` → TrafegoWorki1
- `github.com` → `~/.ssh/id_ed25519` → WorkiDigital

Fork: github.com/TrafegoWorki1/laya
Clone: `C:\Users\Samsung\repos\laya` (com `upstream` → NandhaKishorM/laya)

---

## Armadilhas (custaram tempo — não repetir)

1. **Cliente HF trava baixando os pesos.** Parou em 4 MB e não avançou.
  worked around baixando `model.safetensors` com `curl` direto e colocando
   no snapshot. Mas aí o cache do HF não registra os arquivos e passa a
   reportar snapshot incompleto. **Solução final:** rodar a API Python uma
   vez **sem** `HF_HUB_OFFLINE` para o cache se auto-registrar.

2. **`HF_HUB_OFFLINE=1` causa erro** "cached snapshot is incomplete" mesmo
   com todos os arquivos no disco. Não usar.

3. **`laya_decide` (JSON Schema) é frágil.** Rejeitou `minimum`/`maximum`
   em campo numérico com `invalid_schema`. Use `laya_predict`, que tem o
   schema nativo tipado. Não insistir no `laya_decide`.

4. **Shape do `criteria` difere entre a lib Python e a tool MCP:**
   - `choice` → **objeto** `{label: descrição}`. Lista só funciona na API Python.
   - `score` → **lista simples** de strings. `[["a","b"]]` aninhado é rejeitado.
   - `noul` → sem `criteria`, ou `{"true": "...", "false": "..."}`.
   - Campo de texto é `instructions`, **não** `question`.
   - `state` no MCP é **objeto**, não string.

5. **Script de exemplo do repo está desatualizado** — usa `question` e
   `options`. Ler `laya/mcp/tools.py:validate_questions` e
   `laya/agent.py:_check_question` para o schema real.

6. **`hermes setup` é interativo** e quebra em shell sem console Windows
   (`NoConsoleScreenBufferError`). Para instalar `mcp` no venv do Hermes:
   ```
   uv pip install --python <venv>/Scripts/python.exe mcp
   ```
   (o venv não tem pip próprio)

7. **O agente não pode editar `config.yaml`** (bloqueado por segurança).
   Usar `hermes config set`.

8. **Checkpoint com temperatura inválida.** O base inglês avisa:
   `invalid temperatures ... using 0.5. Treat confidence as uncalibrated`.
   Saída do upstream, não é bug da instalação.

---

## Benchmark Hunter (o resultado que importa)

Arquivo: `~/AppData/Local/hermes/cache/scratch/laya_bench.py`
Resultado: `laya_bench_resultado.json` (mesma pasta)

**40 casos rotulados à mão, 6 classes** (etapas do roteiro Hunter + "fora"),
com 10 casos deliberadamente borderlines. Medido: **10/40 = 25,0%**,
1.175 ms/msg em CPU.

### O padrão da falha

| Etapa esperada | Acertos |
|---|---|
| 0 (fora/spam) | 3/5 |
| 1 (abordagem) | 3/5 |
| 2 (interesse) | 4/7 |
| 3 (conceito) | **0/8** |
| 4 (escopo) | **0/8** |
| 5 (agendamento) | **0/7** |

O modelo **não distingue as etapas 3, 4 e 5**. Tudo que deveria ser conceito,
escopo ou agendamento ele jogou para "interesse" (etapa 2). Acertou só as
etapas Easy, cujas categorias são genéricas o bastante para bater.

Isso confirma o que o README do projeto avisa: os checkpoints base ficam perto
de acaso fora dos 4 workflows em que foram fine-tuned (fatura, incidente de
segurança, atendimento, observabilidade). **Triagem comercial não é um deles.**

### Confiança não é sinal de dúvida

`"oi"` → errou como "fora" com **0.908** de confiança.
`"Quanto custa isso?"` → errou como "interesse" com 0.478.
Erros Jem crentes. Não usar `min_confidence` como filtro de segurança aqui.

### Caso que o modelo acerta

Classificação de billing/atendimento **simples** funciona bem:

| Pergunta | Resposta | Confiança |
|---|---|---|
| Qual departamento? (cobrança triplicada, Procon) | billing | 1.000 |
| Quer estorno? | sim | 0.999 |
| Urgência (escala 3 níveis) | 1.52 → "hoje" 0.535 | 0.535 |

Esse é o domínio `customer_service`, um dos fine-tuned. Coerente com o README.

---

## Recomendação

1. **Não usar o Laya base no Hunter.** 25% é pior que chute útil.
2. **Testar `laya-typed-decisions`** se quiser insistir — é o único fine-tuned
   e o que bate o Jev no benchmark. Mas os workflows dele (fatura, segurança,
   atendimento, observabilidade) não cobrem triagem comercial, então pode não
   resolver.
3. **Restringir a perguntas binárias** se for usar: "isso é spam?",
   "o lead mencionou dia e hora?". Não as 6 etapas de uma vez.
4. **Hunter continua com LLM.** O vídeo do autor chega à mesma conclusão: pra
   custo e latência, um LLM comum bate o classificador especializado.

---

## Como rodar o benchmark de novo

```bash
cd ~/AppData/Local/hermes/cache/scratch
LAYA_DEVICE=cpu ~/AppData/Roaming/uv/tools/laya/Scripts/python.exe laya_bench.py
```

Editar a lista `CASOS` e os rótulos para testar casos reais.
