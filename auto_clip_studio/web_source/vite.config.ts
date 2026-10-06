import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../web_dist',
    emptyOutDir: true,
  },
  server: {
    watch: {
      ignored: ['**/dist_app/**', '**/dist_installer/**', '**/build/**', '**/venv/**']
    },
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('error', (_err, _req, res: any) => {
            if (res && !res.headersSent && typeof res.writeHead === 'function') {
              res.writeHead(503, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({
                error: 'Backend API server on port 8000 is not running. Start the backend with `npm run dev` from auto_clip_studio/web_source or `python -m uvicorn chopster.auto_clip_studio.engine.main:app --host 127.0.0.1 --port 8000`.',
                status: 503
              }));
            }
          });
        }
      },
      '/docs': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/redoc': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/openapi.json': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    }
  }
})

