// Visual regression for the static chrome of the four tabs.
// Dynamic regions (cluster counts, JSON from Atlas, sessions) are masked,
// so the test guards layout rather than data.
import { test, expect } from '@playwright/test';

const TABS = [
  { index: 0, name: 'tab1-schema-flexivel' },
  { index: 1, name: 'tab2-model-swap' },
  { index: 2, name: 'tab3-agent' },
];

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
});

for (const tab of TABS) {
  test(`layout of ${tab.name}`, async ({ page }) => {
    await page.locator('.nav-pill').nth(tab.index).click();
    await page.waitForTimeout(1500); // fade-in + initial card load

    await expect(page).toHaveScreenshot(`${tab.name}.png`, {
      fullPage: false,
      mask: [
        page.locator('.stat-bar'),          // counts change with usage
        page.locator('.json-scroll'),       // live Atlas documents
        page.locator('.chat-box'),          // chat history
        page.locator('.status-pill'),       // connection state
        page.locator('.dim.mono'),          // session _ids
      ],
      animations: 'disabled',
    });
  });
}
