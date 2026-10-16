import test from 'node:test'
import assert from 'node:assert/strict'
import { explainFact, summarize, isFallback } from '../src/memoryExplain.js'

test('server scoreDetails becomes per-leg rank and share', () => {
  const why = explainFact({ explain: { legs: [
    { name: 'vector', rank: 1, weight: 1, contribution: 1 / 61 },
    { name: 'bm25', rank: null, weight: null, contribution: 0 },
  ] } })
  assert.equal(why.available, true)
  assert.equal(why.legs[0].label, 'semântica')
  assert.equal(why.legs[0].share, 1)
  assert.equal(why.legs[1].rank, null)
})

test('unexpected explain formats are "unavailable", never throw', () => {
  for (const bad of [null, undefined, {}, { explain: 'x' }, { explain: { legs: 'x' } },
    { explain: { legs: [null, 3, { rank: 1 }] } }]) {
    assert.equal(explainFact(bad).available, false)
  }
  assert.equal(summarize(null), null)
  assert.equal(summarize('lixo'), null)
})

test('summary flags fallbacks and rerank preview', () => {
  const s = summarize({ strategy: 'app_rrf', rerank: { enabled: true, status: 'unavailable', model: 'rerank-3', preview: true },
    fallbacks: ['rank_fusion unsupported: OperationFailure'], facts: [{ id: 'a', fact: 'x', score: NaN }] })
  assert.equal(s.fallback, true)
  assert.equal(s.rerank.preview, true)
  assert.equal(s.facts[0].score, null)
  assert.equal(isFallback('rank_fusion'), false)
  assert.equal(summarize({ strategy: 'novo' }).label, 'estratégia desconhecida')
})
