import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const apiProxy = {
  '/api': `http://localhost:${process.env.BACKEND_PORT || 8010}`,
};

export default defineConfig({
  plugins: [react()],
  define: { global: 'globalThis' },
  resolve: {
    alias: {
      buffer: require.resolve('buffer/'),
      events: require.resolve('events/'),
      process: require.resolve('process/browser'),
      stream: require.resolve('stream-browserify'),
    },
  },
  server: {
    port: 5183,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: { port: 5183, strictPort: true, proxy: apiProxy },
});
