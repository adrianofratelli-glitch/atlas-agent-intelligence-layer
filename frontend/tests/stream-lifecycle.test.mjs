import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
let source = (await readFile(new URL('../src/api.js', import.meta.url), 'utf8')).replaceAll('import.meta.env', '({})')
source = source.replace(/from (["'])axios\1/g, `from '${new URL('../node_modules/axios/index.js', import.meta.url).href}'`)
const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
const encoder = new TextEncoder()
test('terminal SSE result closes its body and releases the browser connection', async () => {
 const original=globalThis.fetch;let cancelled=false
 const response=new Response(new ReadableStream({ start(c){c.enqueue(encoder.encode('event: result\r\ndata: {"ok":true}\r\n\r\n'))},cancel(){cancelled=true} }));globalThis.fetch=async()=>response
 try { assert.deepEqual(await api.api.agentRunStream({}),{ok:true}); assert.equal(cancelled,true);assert.equal(response.body.locked,false) }finally{globalThis.fetch=original}
})

test('agent stream waits for a pending identity switch before sending its token', async () => {
  const original = globalThis.fetch
  const sent = []
  let releaseLogin
  globalThis.fetch = async (path, opts = {}) => {
    if (path === '/api/auth/token') {
      await new Promise((r) => { releaseLogin = r })
      return new Response(JSON.stringify({ access_token: 'tok-marina' }), { status: 200 })
    }
    sent.push(opts.headers?.Authorization ?? null)
    return new Response(new ReadableStream({ start(c) { c.enqueue(encoder.encode('event: result\ndata: {"ok":true}\n\n')); c.close() } }))
  }
  try {
    api.setAuthToken('tok-adriano')
    const login = api.api.login('marina')
    const run = api.api.agentRunStream({ user_key: 'marina' })
    await new Promise((r) => setTimeout(r, 20))
    assert.equal(sent.length, 0, 'o turno não pode sair antes do login novo')
    releaseLogin()
    await login
    assert.deepEqual(await run, { ok: true })
    assert.deepEqual(sent, ['Bearer tok-marina'])
  } finally { globalThis.fetch = original }
})
