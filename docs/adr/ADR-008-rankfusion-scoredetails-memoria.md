# ADR-008: Memória longa com `$rankFusion` no servidor, `scoreDetails` e config viva

**Status:** Aceito
**Data:** 2026-10-16
**Contexto do cliente/conta:** PoV Intelligence Layer (aba Agente, memória de longo prazo)

## Contexto
`memory.load_relevant` fazia retrieval híbrido com RRF na aplicação: `count_documents`, depois
`$vectorSearch`, depois `$search` (o docstring dizia "em paralelo", o código aguardava um e
depois o outro) e por fim a leitura dos fatos mais recentes. Quatro round-trips sequenciais,
p50 de 1.267 ms nesta máquina (baseline em `eval/reports/memory-recall-baseline-2026-10-16.json`).
Ninguém conseguia responder "por que o agente lembrou ESTE fato", e pesos/profundidade estavam
no código.

Sonda no cluster (9.0.4, `docs/capability-probe-2026-10.md`) e documentação oficial:
- `$rankFusion` (https://www.mongodb.com/docs/manual/reference/operator/aggregation/rankfusion/)
  aceita `$vectorSearch` com `filter` e `$search` com `compound.filter` nas pernas, pesos em
  `combination.weights` e `scoreDetails: true`. Dentro da perna só valem estágios de seleção.
- O `$meta` que funciona é `scoreDetails`; `searchScoreDetails` (exemplo Node.js da doc) volta
  vazio. Perna sem o documento vem com `rank: "NA"` (a doc diz `N/A`). A doc avisa que o
  formato não é garantido.

## Decisão
1. **Um `$rankFusion` por turno.** Pernas `vector` (`db.tenant_vector_stage`, filtro
   `user_key` + `active`) e `bm25` (`$search` com `compound.filter` `user_key` + `active`).
   O isolamento é aplicado NO SERVIDOR nas duas pernas; nada é filtrado depois na aplicação.
   Construído por `atlas_retrieval.build_rank_fusion` (pov-shared 0.3.0), que recusa o que o
   servidor recusaria. `tests/test_tenant_filter.py` falha se alguém montar `$rankFusion`,
   `$rerank` ou o `$search` da memória por fora dos builders.
2. **Duas ondas paralelas.** 1ª onda (`asyncio.gather`): config viva, contagem e os fatos mais
   recentes (servem ao atalho "all", ao merge contra o lag do autoEmbed e ao fallback). 2ª
   onda: a fusão. 4,25 comandos por consulta, mas em 2 tempos de rede em vez de 4.
3. **BM25 em português, exato + fuzzy.** `agent_memory_bm25.fact` com `lucene.portuguese`
   (stopwords e stemming) e duas cláusulas `should`: termo exato (boost 2) e termo com
   `fuzzy.maxEdits` 1. O fuzzy (Damerau) faz "PED-2041" casar "PED-2014"; com as duas cláusulas
   o documento exato pontua nas duas e fica à frente.
4. **Config viva em `ai_brain.memory_retrieval_config`** ("agents are data"): `weights`,
   `per_branch_limit`, `num_candidates`, `fuzzy_max_edits`, `score_details`, `rerank`. Lida a
   cada turno; `update_one` vale no turno seguinte. Documento ausente ou malformado (peso
   negativo/NaN/texto, limite fora da faixa) → defaults seguros + aviso no trace
   (`FusionConfig.from_doc`, nunca levanta). Seed com `$setOnInsert` (preserva ajuste ao vivo).
5. **Profundidade medida, não chutada.** 8 candidatos por perna (> `RELEVANT_LIMIT` = 5), pesos
   1/1. Ver tabela abaixo.
6. **Explicabilidade.** `scoreDetails: true`; `atlas_retrieval.parse_score_details` converte em
   rank, peso, score e contribuição (`peso / (60 + rank)`) por perna. Vai no evento de memória
   do trace (`memory_retrieval`, persistido em `POC.agent_traces`) e no inspector da aba Agente
   ("Por que estes fatos foram lembrados"). Formato inesperado = `explain: null` / "detalhe
   indisponível", nunca erro.
7. **Fallback em cascata, sem flag** (`mode` sempre um valor de `memory.MODES`):
   `rank_fusion` → `app_rrf` (duas pernas REALMENTE em paralelo + `atlas_retrieval.app_rrf`, mesmo
   formato de explain) → `vector` ou `lexical` (uma perna fora) → `recent`. Estágio rejeitado
   fica fora por 5 min (`CapabilityCache`); timeout/rede não marcam, o turno seguinte tenta de
   novo. Sem pov-shared, o app sobe e a fusão roda no RRF local.
8. **Preservado:** quarentena anti-poisoning na leitura, merge dos 2 fatos mais novos, dedupe
   por `_id`, atalho "all". Endurecido: `user_key` não-string (`{"$ne": null}` casaria todos os
   tenants) é recusado antes de qualquer query; pergunta truncada em 2.000 caracteres.

### Medição (`backend/scripts/memory_recall_eval.py`, `eval/memory_recall.json`, POC_test)

41 consultas sintéticas (identificador exato, paráfrase, typo, superseded, outro tenant, tenant
vazio), ~20 distratores por tenant, 3 repetições.

| Variante | recall@5 | MRR | p50 | p95 | Violações |
|---|---|---|---|---|---|
| Baseline (RRF app sequencial, 5 por perna, analyzer padrão) | 1,000 | 0,859 | 1.267 ms | 1.340 ms | 0 |
| `$rankFusion`, 20 por perna, analyzer padrão | 0,917 | 0,780 | 753 ms | 916 ms | 0 |
| RRF app, 20 por perna, analyzer padrão | 0,917 | 0,780 | 729 ms | — | 0 |
| `$rankFusion`, 20 por perna, `lucene.portuguese` | 0,917 | 0,861 | 730 ms | — | 0 |
| `$rankFusion`, 10 por perna, `lucene.portuguese` | 0,972 | 0,891 | 716 ms | — | 0 |
| **`$rankFusion`, 8 por perna, `lucene.portuguese` (default)** | **1,000** | **0,918** | **740 ms** | **907 ms** | **0** |

Violações = fato de outro tenant, fato inactive/superseded, duplicata, acima do limite ou modo
desconhecido. A profundidade, não o servidor, explicava a queda: RRF na aplicação e no servidor
dão o mesmo número com a mesma profundidade.

## Alternativas consideradas
- **Manter RRF na aplicação (só paralelizar).** Mesma qualidade, mas dois rankings intermediários
  trafegam pela rede e o explain seria nosso, não do servidor. Fica como fallback.
- **`$scoreFusion`.** Normaliza scores em vez de posições; as escalas de `vectorSearchScore` e
  BM25 mudam com modelo/índice (ver README, "Thresholds are measured"). RRF por posição é o
  que já estava calibrado.
- **20 candidatos por perna (o "mais profundo é melhor").** Medido pior: o ruído lexical entra
  na fusão. Reavaliar quando a memória por usuário crescer para centenas de fatos.
- **`MongoDBStore` do LangGraph como memória longa.** Ver ADR-012.

## Consequências
- p50 do retrieval −42% (1.267 → 740 ms), MRR +0,059, recall@5 mantido em 1,0.
- O inspector mostra rank/peso/contribuição por perna e quando um fallback rodou.
- **Demo pendente:** `POC.agent_memory_bm25` da demo continua com o analyzer padrão até um reset
  controlado (`ALLOW_DEMO_DB_WRITE=1 seed.py`, que agora atualiza o índice) ou
  `updateSearchIndex`. Com o analyzer antigo e 8 por perna, recall@5 0,972 (medido no POC_test).
- Caos LIVE cobre fusão rejeitada, `$rerank` rejeitado, BM25 BUILDING, vetor ausente,
  maxTimeMS e mongot fora (`scripts/chaos_suite.py memory_*`).
- Dataset pequeno (≤ 38 fatos ativos por tenant) e escrito pela mesma família de modelo do
  agente: os números servem para comparar variantes, não para prometer qualidade em produção.
