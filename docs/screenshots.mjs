// Captures screenshots of the three POC tabs for the README.
// Usage: node docs/screenshots.mjs  (frontend on :5173 and backend on :8000)
import { chromium } from 'playwright';

const BASE = 'http://localhost:5183';
const OUT = new URL('./img/', import.meta.url).pathname;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto(BASE, { waitUntil: 'networkidle' });

const tab = (n) => page.locator('.nav-pill').nth(n);

// Tab 1 — Flexible schema (document loads on its own)
await tab(0).click();
await page.waitForTimeout(2500);
await page.screenshot({ path: OUT + 'tab1-schema-flexivel.png' });

// Tab 2 — Model swap: ask a question in the mini-chat
await tab(1).click();
await page.waitForTimeout(1500);
const chatInput = page.locator('input[type="text"]:visible').first();
await chatInput.fill('qual fone JBL você recomenda para home office?');
await chatInput.press('Enter');
await page.waitForTimeout(12000);
await page.screenshot({ path: OUT + 'tab2-model-swap.png' });

// Tab 3 — Agent: run a scenario through the MongoDB MCP Server, then wait until
// the replay reaches the final "Repetir" phase before capturing.
await tab(2).click();
await page.waitForTimeout(1000);
await page.locator('.agent-chip', { hasText: 'Trocar' }).click();
await page.waitForFunction(
  () => {
    const active = document.querySelector('.phase-card.active .phase-label');
    return active && active.textContent === 'Repetir';
  },
  { timeout: 90000 },
);
await page.waitForTimeout(600);
await page.screenshot({ path: OUT + 'tab3-agent.png', fullPage: true });

// Tab 3 — semantic cache HIT: ask a seeded FAQ so the green CACHE HIT flag,
// score and "served from MongoDB, no LLM" latency are all visible.
const agentInput = page.locator('input[type="text"]:visible').first();
await agentInput.fill('Qual o prazo pra trocar um produto que eu comprei?');
await agentInput.press('Enter');
await page.waitForFunction(
  () => {
    const active = document.querySelector('.phase-card.active .phase-label');
    return active && active.textContent === 'Repetir';
  },
  { timeout: 60000 },
);
await page.waitForTimeout(600);
await page.screenshot({ path: OUT + 'tab3-cache-hit.png' });

await browser.close();
console.log('Screenshots saved to docs/img/');
