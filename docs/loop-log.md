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

## A1 — helpers no `_shared` + `$rankFusion` na memória longa

### Iteração 1 — 2026-10-16

| Etapa | Resultado |
|---|---|
| `_shared` 0.3.0 `atlas_retrieval` (unit + adversarial + fuzz) | 55 testes; suíte do `_shared` 268 verdes |
| `tests/test_memory_retrieval.py` (cascata, adversarial, fuzz, sem pov-shared, concorrência) | 21 OK |
| `tests/test_tenant_filter.py` (guard cobre `$rankFusion`, `$search`, `$rerank`) | 8 OK |
| LIVE `memory_recall_eval.py` (config padrão: 20 por perna, analyzer padrão) | recall@5 0,917 · MRR 0,780 · p50 753 ms — **pior que o baseline** |

- **O que falhou (1):** fuzz do `_shared` — `RerankConfig.from_doc` com `pool` < 5 deixava `top_n`
  default (5) maior que `pool`. **Correção:** `top_n = min(pool, ...)`.
- **O que falhou (2):** recall@5 1,0 → 0,917 e MRR 0,859 → 0,780 com o `$rankFusion`.
- **Causa raiz:** profundidade por perna, não o servidor: RRF na aplicação a 20 candidatos dá o
  MESMO 0,917; fuzzy não muda nada (com e sem: 0,917/0,782). O índice `agent_memory_bm25` usava o
  analyzer padrão, e as stopwords ("que", "eu", "não", "meu") casavam em quase todo fato; com 20
  candidatos esse ruído lexical entrava na fusão à frente do fato que só a perna semântica achava.
- **Correção:** `fact` com `lucene.portuguese` no `seed.py` (e o seed passa a ATUALIZAR o índice
  BM25 existente; antes só imprimia "já existe"), aplicado só no `POC_test` (READY em 57 s);
  varredura de profundidade/peso: 6–8 por perna = recall@5 1,0 e MRR 0,918–0,919; 10 → 0,972;
  20 → 0,917. Default 8 (> `RELEVANT_LIMIT`, como pedido), pesos 1/1, gravado em
  `ai_brain.memory_retrieval_config` pelo seed (`$setOnInsert`, preserva ajuste ao vivo).
  `FusionConfig.from_doc` ganhou `default_branch` (aditivo).
- **Achados adversariais corrigidos antes de virarem bug:** `user_key` não-string (`{"$ne": null}`
  viraria filtro que casa todos os tenants nas três leituras) agora levanta `TenantFilterMissing`
  antes de qualquer query; pergunta truncada em 2.000 caracteres.

### Iteração 2 — 2026-10-16 (config medida)

| Etapa | Resultado |
|---|---|
| LIVE `memory_recall_eval.py --label a1-rankfusion` | recall@5 **1,000** · MRR **0,918** (base 0,859) · p50 **740 ms** (base 1.267) · p95 907 ms (base 1.340) · 4,25 round-trips em 2 ondas paralelas (base 4,49 sequenciais) · 0 violações |
| Caos LIVE novos: fusão rejeitada, `$rerank` rejeitado, BM25 BUILDING, vetor ausente, maxTimeMS, mongot fora | 6/6 PASS (fallback esperado, fato certo mantido, 0 vazamento conferido no banco) |
| Concorrência LIVE: 36 `load_relevant` simultâneos, 3 tenants (inclui vazio) | 0 erros, 0 vazamentos; 15,6 s no total (fila do pool de conexões, não correção) |
| Multiagente `pytest -q` contra pov-shared 0.3.0 | 523 passed, 50 skipped |

- **Pendência para a demo (não executada):** o índice `POC.agent_memory_bm25` da demo segue com o
  analyzer padrão até um reset controlado (`ALLOW_DEMO_DB_WRITE=1 seed.py`) ou
  `updateSearchIndex`. Com o analyzer antigo e 8 por perna a medição em `POC_test` deu recall@5
  0,972 (varredura `b8w05`), ainda acima do 0,917 do default de 20.
