/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// FE-B-001 离线构建：base='/'、sourcemap 关闭；ECharts/G6/Monaco 自 MVP-2 起
// 在 manualChunks 单独分包并懒加载（MVP-0 未引入，占位注释锁定该决策）。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      // FE-B-002：dev 经代理同源访问后端（生产 Nginx 同源托管 dist，CORS 默认关闭）
      '/api/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router', 'pinia'],
          'naive-ui': ['naive-ui'],
        },
      },
    },
  },
  test: {
    environment: 'happy-dom',
    include: ['tests/**/*.spec.ts'],
  },
})
