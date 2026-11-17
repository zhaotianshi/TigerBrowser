import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv, type ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'

// The team service validates the exact Host and Origin headers it was started
// with, so the dev proxy rewrites both to the service origin. The target is
// read from .env.local (VITE_TEAM_ORIGIN) to avoid hard-coding a machine
// address in source.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const teamOrigin = env.VITE_TEAM_ORIGIN || 'http://127.0.0.1:8766'
  const teamHost = teamOrigin.replace(/^https?:\/\//, '')
  const proxyToTeam: ProxyOptions = {
    target: teamOrigin,
    changeOrigin: true,
    ws: true,
    configure: (proxy) => {
      const rewrite = (proxyReq: any) => {
        proxyReq.setHeader('host', teamHost)
        proxyReq.setHeader('origin', teamOrigin)
      }
      proxy.on('proxyReq', rewrite)
      proxy.on('proxyReqWs', rewrite)
    },
  }

  return {
    plugins: [vue()],
    base: './',
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url))
      }
    },
    server: {
      port: 5173,
      proxy: {
        '/team/v1': proxyToTeam,
      }
    },
    build: {
      manifest: true,
      outDir: '../../pythonlib/camoufox/studio/team/web',
      emptyOutDir: true,
      assetsDir: 'assets',
      sourcemap: false
    }
  }
})
