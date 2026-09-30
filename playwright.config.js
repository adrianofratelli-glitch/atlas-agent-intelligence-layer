import { defineConfig, devices } from '@playwright/test';

// Prerequisite: the app is running (./start.sh) — frontend :5183 + backend :8010.
export default defineConfig({
  testDir: 'tests/visual',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    // Override with BASE_URL when the default port is taken by another app.
    baseURL: process.env.BASE_URL || 'http://localhost:5183',
    colorScheme: 'dark',
    trace: 'off',
  },
  expect: {
    toHaveScreenshot: {
      // tolerance for font antialiasing differences across machines
      maxDiffPixelRatio: 0.02,
    },
  },
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
});
