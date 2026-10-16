# Loop de testes — capabilities do Atlas (2026-10)

Registro de cada iteração do protocolo de teste em loop da branch `feat/atlas-capabilities-2026-10`
(unit offline → adversarial → propriedade/fuzz → LIVE em `POC_test` → caos → concorrência →
medição contra o baseline → regressão). Todo write real roda em `POC_test`/`ai_brain_test`;
nenhuma escrita nos bancos da demo.

## A0 — baseline (sem mudar código de produção)

### Iteração 1 — 2026-10-16

| Etapa | Resultado |
|---|---|
| Sonda de capability | `docs/capability-probe-2026-10.md`: 9.0.4, `$rankFusion` + `scoreDetails` OK, `$rerank` OK (4 modelos), `$rerank` dentro do `$rankFusion` rejeitado, `pymongocrypt` ausente |
| `unittest discover -s tests` (POC_test) | 172 OK, 10 skipped, 5 s |
| `scripts/preflight.sh` (demo, só leitura + 1 chamada de LLM) | PRONTO com 2 ressalvas não bloqueantes (TRACE_SINK=off, Langfuse indisponível), exit 2 |
| `CHAOS=1 LIVE=1 scripts/chaos_suite.py` | 20/20 PASS |
| Playwright visual (app em POC_test) | 3/3 |
| Playwright e2e (app em POC_test, turnos reais) | 1/1 |
| `scripts/memory_recall_eval.py --label baseline` | recall@5 1,000 · MRR 0,859 · p50 1.267 ms · p95 1.340 ms · 4,49 round-trips/consulta · 0 violações |

- **O que falhou:** Playwright visual na 1ª execução (3/3 vermelhos).
- **Causa raiz:** ambiente, não app: o Chromium headless do Playwright 1.60 não estava instalado.
- **Correção:** `npx playwright install chromium`. Nenhum snapshot alterado.
- **Achado de dataset:** a 1ª versão do `eval/memory_recall.json` (18 fatos/tenant) deu recall@5 1,0
  e MRR 0,919 — fácil demais para separar variantes. A v2 acrescenta ~20 distratores por tenant
  (terceiros, `PED-2014` vs `PED-2041`, mesmo tema); recall@5 continua 1,0 (o `voyage-4` acha o
  fato), e o MRR (0,859; paráfrase 0,759) passa a ser a métrica que discrimina.
- **Achado de código (para A1):** o docstring de `memory.load_relevant` diz "duas buscas em
  paralelo", mas o código aguarda o vetor e só então o BM25; mais `count_documents` antes e a
  leitura dos recentes depois = 4 round-trips sequenciais (~1,27 s no p50 desta máquina).
- **Resultado:** A0 verde.
