import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Development: Vite serves the app on 5173 and proxies /api to the backend,
// so the browser sees one origin. Production: `npm run build` writes dist/,
// which the backend serves itself; no proxy involved.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
})
