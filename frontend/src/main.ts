import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import { cssVariables } from './design/tokens'
import { router } from './router'
import { useAuthStore } from './stores/auth'
import { useCaseStore } from './stores/case'

// FE-I-002 字体本地化：@fontsource 本地打包，运行时零外网请求
import '@fontsource/noto-sans-sc/400.css'
import '@fontsource/noto-sans-sc/500.css'
import '@fontsource/noto-sans-sc/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './styles/base.css'

async function bootstrap(): Promise<void> {
  // 契约先行 MSW 兜底（VITE_USE_MSW=1 时启用；真实联调不开启）
  if (import.meta.env.VITE_USE_MSW === '1') {
    const { worker } = await import('../mocks/browser')
    await worker.start({ onUnhandledRequest: 'bypass' })
  }

  // FE-D-011：CSS 变量由 tokens.ts 单一事实源派生；FE-D-009 默认 compact
  for (const [k, v] of Object.entries(cssVariables)) {
    document.documentElement.style.setProperty(k, v)
  }
  document.documentElement.dataset.density = 'compact'

  const app = createApp(App)
  app.use(createPinia())
  app.use(router)

  // 401 全局钩子：清会话 → /login 保留来源路径（FE-I-005 / D9）
  setUnauthorizedHandler(() => {
    const auth = useAuthStore()
    auth.clearSession()
    useCaseStore().clearAll()
    const current = router.currentRoute.value
    if (current.path !== '/login') {
      void router.push({ path: '/login', query: { redirect: current.fullPath } })
    }
  })

  app.mount('#app')
}

void bootstrap()
