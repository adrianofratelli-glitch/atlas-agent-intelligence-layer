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

## A2 — explicabilidade (`scoreDetails`)

### Iteração 1 — 2026-10-16

| Etapa | Resultado |
|---|---|
| `tests/test_memory_explain_trace.py` (bloco do trace, formato inesperado, todos os modos rotulados) | 3 OK |
| `frontend/tests/memory-explain.test.mjs` (parse defensivo no inspector) | 3 OK (frontend 8/8) |
| LIVE: turno real `eval.mem.ana` no app em `POC_test` | `memory_retrieval` no evento e em `POC_test.agent_traces`; fato certo em 1º, rank 1 nas duas pernas, contribuição 1/61 cada |
| Playwright visual | 3/3 (o inspector só aparece depois de um turno; snapshots inalterados) |

- **O que falhou:** nada nesta fase.
- **Observação de UX (para A7):** a coluna MongoDB fica vazia por 15,8 s durante o turno; os
  eventos do SSE só viram uma linha de status e o trace inteiro aparece no fim. Página com
  1.376 px numa tela de 900.

### Iteração 2 — 2026-10-16

- **O que falhou:** Playwright visual `tab3-agent` (4% dos pixels).
- **Causa raiz:** dado de teste, não código: o usuário `eval.mem.ana`, inserido em
  `POC_test.app_users` para o turno LIVE, virou uma pílula a mais na fileira de usuários.
- **Correção:** usuário removido do banco de teste depois da verificação LIVE.
- **Resultado:** visual 3/3. A2 verde.

## A3 — `$rerank` (Public Preview nos `rerank-3*`)

### Iteração 1 — 2026-10-16

| Etapa | Resultado |
|---|---|
| Unit/adversarial (rerank rodou, rejeitado = no-op + cache de capability) | em `test_memory_retrieval.py` (A1), OK |
| Caos LIVE `memory_rerank_rejected` | PASS |
| Varredura LIVE: RRF app, `$rankFusion`, `+ $rerank` lite pool 20/40/80, `rerank-2.5`, `rerank-3-lite` | 7 relatórios em `eval/reports/memory-recall-a3-*`, 0 violações |

- **O que falhou:** nada quebrou; a decisão é de produto.
- **Resultado medido:** rerank sobe o MRR (0,918 → 0,951–0,972) por +~200 ms p50/p95; pool
  20/40/80 idêntico (≤ 38 fatos por tenant); `rerank-2.5` derruba recall@5 para 0,972; os 2.5
  pioram superseded (0,90 → 0,70–0,75). Recall@5 já é 1,0 sem rerank.
- **Decisão:** desligado no seed (ADR-009); `$rerank` não se aplica ao cache semântico (limiar,
  não ranking). Config de teste restaurada para a do seed ao fim da varredura.

## A4 — Change Streams invalidando o cache semântico

### Iteração 1 — 2026-10-16

| Etapa | Resultado |
|---|---|
| `tests/test_cache_invalidation.py` (relevância, área, hash da FAQ, evento torto, fuzz, worker, singleton) | 15 OK |
| `_shared` worker (history lost, token recusado, token do cursor antes de reabrir) | 59 testes do módulo |
| Caos LIVE `cs_*` (1ª rodada) | 3/5 |

- **Falhou:** `cs_invalid_resume_token` — o servidor devolve **code 15 (Overflow)** para token
  malformado, não 260; o worker tratava como transitório e ficava em backoff eterno.
  **Correção (`_shared`):** recusa não transitória ao abrir COM token = token inútil →
  `on_history_lost` + reabre sem token.
- **Falhou:** `cs_sigkill_resume` — (a) teste: o filho anunciava "SAVED" antes de gravar o token
  e misturava logs no stdout; (b) causa real: o `timeoutMS=15000` (CSOT) do cliente do app vale
  para o `watch`; stream ocioso caía com `ExecutionTimeout` a cada ~18 s e um evento no meio da
  reabertura se perdia. **Correção:** cliente dedicado sem CSOT + `max_await_time_ms=5000`; o
  worker salva o `postBatchResumeToken` antes de reabrir; partida sem token faz reconcile.
- **Falhou (timing de teste):** `cs_invalidation_live` com cliente novo (abre em > 1,5 s);
  teste passa a esperar `state == running`.

### Iteração 2 — 2026-10-16

| Etapa | Resultado |
|---|---|
| Caos LIVE `cs_*` | 5/5 (lag 0,9–0,95 s) |
| App real em POC_test: lifespan, `/api/health`, update de persona | invalidação em 1,02 s; área vizinha preservada; auditoria em `cache_invalidations`; resume do token na partida; shutdown limpo |
| Preflight (POC_test, app rodando) | "worker vivo (running, eventos 3, lag 0.94s)" |
| Regressão: `_shared` 272 · multiagente 523/50 skip · unittest 219 · frontend 8 · caos 32/32 · visual 3/3 · e2e 1/1 | verde |
| Snapshot das 42 coleções da demo antes/depois | **sem diferença** |

## A5 — Queryable Encryption (standalone)

### Iteração 1 — 2026-10-16

| Etapa | Resultado |
|---|---|
| Dependência | `pymongo[encryption]==4.19.0` → pymongocrypt 1.21.2 / libmongocrypt 1.21.0 (`backend/requirements-qe.txt`); sem `crypt_shared` |
| `scripts/qe_sensitive_profile.py` LIVE (POC_test) | ciphertext nos 3 campos, igualdade CPF/telefone, tenant errado vazio, faixa correta, resumo sem CPF |
| `tests/test_qe_profile.py` offline | 5 OK (3 LIVE pulados) |
| `LIVE=1 tests/test_qe_profile.py` | 7 OK, 1 erro |

- **Falhou:** `test_demo_report` — `Mismatched keyId for field 'cpf'` (code 6726301).
- **Causa raiz:** o cliente com auto-decryption guarda o `encryptedFields` em cache; o segundo
  `reset()` recriou a collection com DEKs novas e o cliente velho inseriu com as keyIds antigas.
- **Correção:** `reset()` recria o cliente da aplicação.

### Iteração 2 — 2026-10-16

| Etapa | Resultado |
|---|---|
| `LIVE=1 tests/test_qe_profile.py` (inclui operador `$ne`, vazio, invisível, 5.000 chars, faixa saturada, 15 consultas simultâneas) | 8 OK |
