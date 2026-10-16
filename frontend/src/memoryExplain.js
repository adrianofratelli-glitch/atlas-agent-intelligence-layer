// "Por que este fato foi lembrado": normaliza o bloco `memory_retrieval` do evento de memória
// (backend/agent_graph.py:_memory_explain). Formato do scoreDetails não é garantido pela doc
// do $rankFusion: qualquer coisa inesperada vira "detalhe indisponível", nunca exceção.

const STRATEGY_LABEL = {
  rank_fusion: 'Fusão no servidor ($rankFusion)',
  'rank_fusion+rerank': '$rankFusion + $rerank',
  rerank_unavailable: '$rankFusion ($rerank indisponível)',
  app_rrf: 'RRF na aplicação (fallback)',
  vector: 'Só semântica (fallback)',
  lexical: 'Só lexical (fallback)',
  recent: 'Mais recentes (fallback)',
  all: 'Todos os fatos (poucos)',
};

const LEG_LABEL = { vector: 'semântica', bm25: 'lexical' };

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

export function strategyLabel(strategy) {
  return STRATEGY_LABEL[strategy] ?? 'estratégia desconhecida';
}

export function isFallback(strategy) {
  return !['rank_fusion', 'rank_fusion+rerank', 'all'].includes(strategy);
}

export function explainFact(fact) {
  const legs = Array.isArray(fact?.explain?.legs) ? fact.explain.legs : null;
  if (!legs || legs.length === 0) return { available: false, legs: [] };
  const rows = legs
    .filter((l) => l && typeof l.name === 'string')
    .map((l) => ({
      name: l.name,
      label: LEG_LABEL[l.name] ?? l.name,
      rank: Number.isInteger(l.rank) && l.rank > 0 ? l.rank : null,
      weight: num(l.weight),
      contribution: Math.max(0, num(l.contribution) ?? 0),
    }));
  if (rows.length === 0) return { available: false, legs: [] };
  const total = rows.reduce((a, r) => a + r.contribution, 0);
  return {
    available: true,
    legs: rows.map((r) => ({ ...r, share: total > 0 ? r.contribution / total : 0 })),
    total,
  };
}

export function summarize(block) {
  if (!block || typeof block !== 'object') return null;
  const facts = Array.isArray(block.facts) ? block.facts : [];
  const rerank = block.rerank && typeof block.rerank === 'object' ? block.rerank : null;
  return {
    strategy: block.strategy ?? null,
    label: strategyLabel(block.strategy),
    fallback: isFallback(block.strategy),
    rerank: rerank && rerank.enabled
      ? { status: rerank.status ?? 'off', model: rerank.model ?? '?', preview: !!rerank.preview }
      : null,
    fallbacks: Array.isArray(block.fallbacks) ? block.fallbacks.slice(0, 4).map(String) : [],
    searchMs: num(block.search_ms),
    weights: block.fusion?.weights ?? null,
    facts: facts.slice(0, 8).map((f) => ({
      id: String(f?.id ?? ''),
      text: String(f?.fact ?? ''),
      score: num(f?.score),
      rerankScore: num(f?.rerank_score),
      why: explainFact(f),
    })),
  };
}
