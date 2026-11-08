/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

// The browser always calls the API on its own origin (/api/v1): the dev and preview servers
// proxy /api to the FastAPI server, and in a deployment nginx does (architecture section 15).
// So the API needs no CORS, and no API address is ever taken from user input.
// SITESCOPE_API_URL configures the proxy only; it is not a VITE_ variable, so it never reaches
// the JavaScript bundle.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'SITESCOPE_');
  const apiTarget = env.SITESCOPE_API_URL ?? 'http://127.0.0.1:8000';
  const proxy = { '/api': { target: apiTarget, changeOrigin: false } };

  return {
    plugins: [react(), tailwindcss()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
    build: { target: 'es2022' },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/tests/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      restoreMocks: true,
      unstubGlobals: true,
    },
  };
});
