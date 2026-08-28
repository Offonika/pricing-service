import { Buffer } from 'node:buffer'
import { resolve } from 'node:path'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.API_PROXY_TARGET || 'http://127.0.0.1:18080'
  const proxyHeaders: Record<string, string> = {}

  if (env.API_BASIC_USER && env.API_BASIC_PASSWORD) {
    proxyHeaders.Authorization = `Basic ${Buffer.from(`${env.API_BASIC_USER}:${env.API_BASIC_PASSWORD}`).toString('base64')}`
  }

  return {
    plugins: [react()],
    base: './',
    build: {
      rollupOptions: {
        input: {
          main: resolve(process.cwd(), 'index.html'),
          logistics: resolve(process.cwd(), 'logistics.html'),
        },
      },
    },
    server: {
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          headers: proxyHeaders,
        },
      },
    },
  }
})
