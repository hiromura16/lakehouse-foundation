import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The gateway serves dist and /api from one origin. No credential-bearing
// .env file, CDN, arbitrary proxy target or public development bind is used.
export default defineConfig({
  envDir: false,
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true },
  preview: { host: '127.0.0.1', port: 5173, strictPort: true },
  build: { target: 'es2022', sourcemap: false },
});
