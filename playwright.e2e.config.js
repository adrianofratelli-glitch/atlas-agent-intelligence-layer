import { defineConfig, devices } from '@playwright/test';

// Roteiro funcional da demo contra a app rodando (./start.sh). Faz turnos REAIS
// (Atlas + gateway LLM): rode com o backend apontado para o banco de teste
// (MONGODB_DB=POC_test MONGODB_BRAIN_DB=ai_brain_test ./start.sh) após um reset.
export default defineConfig({
  testDir: 'tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 240_000,
  reporter: [['list']],
  use: {
    baseURL: process.env.BASE_URL || 'http://127.0.0.1:5183',
    colorScheme: 'dark',
    trace: 'off',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
