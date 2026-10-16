# ADR-009: `$rerank` na memória longa — implementado, medido e desligado por padrão

**Status:** Aceito
**Data:** 2026-10-16
**Contexto do cliente/conta:** PoV Intelligence Layer (memória de longo prazo)

## Contexto
O `$rerank` reordena documentos com os modelos de reranking da Voyage AI, nativo no Atlas
(https://www.mongodb.com/docs/vector-search/query/aggregation-stages/rerank/). Os modelos
`rerank-3` e `rerank-3-lite` estão em **Public Preview** (MongoDB 9.0+, cluster dedicado);
não é GA e não é apresentado como tal. O recurso está habilitado no projeto.

Fatos da documentação e da sonda (`docs/capability-probe-2026-10.md`, 9.0.4):
- `model` é obrigatório (sem default), `numDocsToRerank` ≤ 1000, o campo de `path` precisa
  existir em todo documento.
- Não pode ficar dentro de input pipeline do `$rankFusion` (servidor: code 12108704). Vem depois.
- Substitui `$meta: "score"`: o score da fusão é copiado para `fusion_score` antes do estágio.
- Modelo inexistente = erro da Voyage (code 65169).

## Decisão
1. **Implementado e governado por documento.** `ai_brain.memory_retrieval_config.rerank`
   (`enabled`, `model`, `pool`, `top_n`), lido a cada turno. Pipeline: `$rankFusion` →
   `$limit pool` → `$addFields fusion_score/score_details` → `$set` de guarda → `$rerank` →
   `rerank_score` → `$limit top_n`, montado por `atlas_retrieval.build_rerank_stages`.
2. **Fallback no-op.** Estágio rejeitado → a fusão roda sem rerank, `mode =
   "rerank_unavailable"`, o trace e o inspector dizem "indisponível, ordem da fusão mantida".
   A recusa fica lembrada 5 min (`CapabilityCache`). Caos LIVE: `memory_rerank_rejected`.
3. **Desligado no seed (`enabled: false`).** O ganho existe, mas é de ORDEM dentro do bloco de
   5 fatos, não de quais fatos entram: recall@5 já é 1,0 sem rerank. Pagar +200 ms e uma chamada
   à Voyage por turno para reordenar o mesmo conjunto não se justifica na demo.
4. **Cache semântico: não se aplica.** O cache decide por LIMIAR do top-1
   (`hit_threshold` calibrado sobre `vectorSearchScore`), não por ranking. O `rerank_score` tem
   outra escala (exigiria recalibrar) e somaria ~200 ms justamente no caminho cuja tese é "HIT
   em centenas de ms, sem LLM". Fica fora.

### Medição (`backend/scripts/memory_recall_eval.py`, `eval/memory_recall.json`, POC_test)

3 repetições por consulta; latência do retrieval inteiro (as duas ondas), cliente → Atlas.
Nas variantes com rerank, `per_branch_limit = pool` (a fusão precisa entregar `pool` candidatos).

| Variante | recall@5 | MRR | p50 | p95 | MRR paráfrase | MRR superseded | Round-trips |
|---|---|---|---|---|---|---|---|
| RRF na aplicação (8 por perna) | 1,000 | 0,918 | 726 ms | 761 ms | 0,870 | 0,900 | 5,21 |
| `$rankFusion` (8 por perna, **default**) | 1,000 | 0,918 | 733 ms | 915 ms | 0,870 | 0,900 | 4,25 |
| `$rankFusion` + `$rerank` `rerank-2.5-lite`, pool 20 | 1,000 | 0,951 | 937 ms | 1.124 ms | 0,974 | 0,750 | 4,33 |
| idem, pool 40 | 1,000 | 0,951 | 939 ms | 1.102 ms | 0,974 | 0,750 | 4,33 |
| idem, pool 80 | 1,000 | 0,951 | 935 ms | 1.112 ms | 0,974 | 0,750 | 4,33 |
| `rerank-2.5`, pool 20 | **0,972** | 0,958 | 924 ms | 1.103 ms | 1,000 | 0,700 | 4,35 |
| `rerank-3-lite` (Public Preview), pool 20 | 1,000 | **0,972** | 927 ms | 1.090 ms | 0,974 | 0,900 | 4,35 |

Leitura:
- Pool 20/40/80 dá o mesmo resultado: os tenants têm ≤ 38 fatos ativos, não há candidato além
  de 20. A diferença só apareceria com memórias de centenas de fatos.
- `rerank-2.5` perde um caso de recall@5; os modelos 2.5 pioram as consultas "superseded"
  (pergunta citando o valor antigo: o reranker prefere um distrator parecido ao fato vigente).
- `rerank-3-lite` é o único que melhora sem piorar nada, e está em Preview.
- RRF na aplicação e `$rankFusion` empatam em qualidade e no p50; o servidor faz um round-trip
  a menos e dá o explain sem código nosso. O p95 do servidor foi maior nesta rodada (915 ms vs
  761 ms) — variação de rede/servidor, registrada.

## Alternativas consideradas
- **Ligar `rerank-3-lite` no seed.** Melhor MRR, mas Preview na demo de cliente e +194 ms p50
  sem mudar o conjunto de fatos do prompt. Disponível com um `update_one`.
- **`$rerank` no catálogo de produtos** (busca do agente). Fora do escopo desta fase; é onde o
  ganho de ordem do top-N aparece para o cliente. Candidato natural para a próxima rodada.

## Consequências
- Ligar: `db.memory_retrieval_config.updateOne({_id: "default"}, {$set: {"rerank.enabled": true,
  "rerank.model": "rerank-3-lite"}})` — vale no turno seguinte; o inspector marca "Public Preview".
- O preflight confere o modelo configurado quando o rerank está ligado (aviso, não falha).
- Recalibrar a decisão se a memória por usuário crescer: com centenas de fatos o recall@5 deixa
  de ser 1,0 e o rerank passa a mudar o CONJUNTO, não só a ordem.
