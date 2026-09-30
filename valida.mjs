import { chromium } from 'playwright'
const OUT = '/Users/adriano.fratelli/Documents/PoVs/iceberg-mongodb-lakehouse/docs/screenshots'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
const erros = []
page.on('pageerror', (e) => erros.push(e.message))
page.on('console', (m) => { if (m.type() === 'error') erros.push(m.text()) })

await page.goto('http://127.0.0.1:5250', { waitUntil: 'networkidle' })
await page.waitForTimeout(3000)
console.log('selo inicial:', (await page.locator('.pill').innerText()).trim())

// dispara o INSERT e recarrega imediatamente: o Mongo ja tem o pedido, o Iceberg ainda nao
await page.getByRole('button', { name: 'INSERT' }).click()
await page.waitForTimeout(4000)
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(6000)
const selo = (await page.locator('.pill').innerText()).trim()
const aviso = await page.locator('.notice').first().innerText().catch(() => '')
console.log('selo durante propagacao:', selo)
console.log('aviso:', aviso.replace(/\n/g, ' ').slice(0, 180))
await page.screenshot({ path: `${OUT}/../propagando.png` })

// agora espera refletir e captura o screenshot do ciclo
for (let i = 0; i < 30; i++) {
  await page.waitForTimeout(3000)
  const t = await page.locator('.timeline').innerText().catch(() => '')
  if (/refletido em/.test(t)) { console.log('propagou:', t.replace(/\n/g, ' | ')); break }
}
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(5000)
console.log('selo apos propagar:', (await page.locator('.pill').innerText()).trim())
await browser.close()
console.log(erros.length ? 'ERROS: ' + erros.join(' | ') : 'sem erros de console')
