import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
// Vite env is compile-time input. The test uses the same transport with demo defaults.
const source = (await readFile(new URL('../src/api.js', import.meta.url), 'utf8')).replaceAll('import.meta.env', '({})')
globalThis.localStorage = { getItem: () => '', setItem() {} }
const { api } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
async function deadlineTest(stallBody) {
 const saved = { fetch: globalThis.fetch, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout }
 let sent = 0, cleared = 0, budget = 0
 globalThis.setTimeout = (fn, ms) => { budget = ms; queueMicrotask(fn); return 123 }
 globalThis.clearTimeout = () => { cleared++ }
 globalThis.fetch = async (_url, options) => {
  sent++
  const stalled = () => new Promise((resolve, reject) => {
   if (options.signal?.aborted) return reject(new DOMException('aborted', 'AbortError'))
   options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  })
  return stallBody ? { ok: true, json: stalled } : stalled()
 }
 try {
  await assert.rejects(Promise.race([api.health(), new Promise((_, reject) => saved.setTimeout(() => reject(new Error('deadline not scheduled')), 100))]), e => e.message !== 'deadline not scheduled')
  assert.equal(sent, 1)
  assert.equal(cleared, 1)
  assert.ok(budget > 0 && budget <= 300000)
 } finally { Object.assign(globalThis, saved) }
}
test('unresponsive headers end within a finite deadline without retry', () => deadlineTest(false))
test('deadline also covers a response body that never finishes', () => deadlineTest(true))
