import test from 'node:test'
import assert from 'node:assert/strict'
import { parseInline } from '../src/richText.js'

test('bold and links become segments, never HTML', () => {
  const segs = parseInline('Previsão: **sol** — fonte: https://ex.com/a. <script>x</script>')
  assert.deepEqual(segs.map((s) => s.type), ['text', 'bold', 'text', 'link', 'text'])
  assert.equal(segs[1].text, 'sol')
  assert.equal(segs[3].href, 'https://ex.com/a')
  assert.ok(segs[4].text.includes('<script>'))
})

test('unbalanced asterisks stay as text', () => {
  assert.deepEqual(parseInline('2 ** 3'), [{ type: 'text', text: '2 ** 3' }])
})
