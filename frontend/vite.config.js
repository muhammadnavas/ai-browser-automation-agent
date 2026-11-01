import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const frontendDir = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        ws: true,
      },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
      '/reports': 'http://127.0.0.1:8000',
      '/screenshots': 'http://127.0.0.1:8000',
    }
  },
  build: {
    outDir: resolve(frontendDir, 'dist'),
    emptyOutDir: true
  }
});
