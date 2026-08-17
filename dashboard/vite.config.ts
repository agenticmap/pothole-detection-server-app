import { defineConfig } from 'vite';

// The dashboard is served from the FastAPI origin in production. In dev we proxy
// rather than lean on CORS, so both environments are same-origin: an Authorization
// header makes a request non-simple, which would otherwise cost an OPTIONS
// preflight *per tile URL* — and MapLibre requests a lot of tile URLs.
// Declared rather than pulling in @types/node for one lookup in a config file.
declare const process: { env: Record<string, string | undefined> };

/** Point the dev proxy at a server on another port: VITE_API_TARGET=http://127.0.0.1:8010 */
const apiTarget = process.env['VITE_API_TARGET'] ?? 'http://127.0.0.1:8000';

export default defineConfig({
  base: '/dashboard/',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: false },
      '/health': { target: apiTarget, changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
