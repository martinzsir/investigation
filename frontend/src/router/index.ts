import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    title?: string
    mvp?: number
    /** 平台页（23b/24/25）：不入侧栏（FE-C-024） */
    platform?: boolean
  }
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('../components/shell/AppShell.vue'),
      children: [
        { path: '', redirect: '/c/overview' },
        // 平台页占位（MVP-0 无业务页面；23b 案件门户 / 24 任务中心 / 25 系统设置不入侧栏）
        { path: 'cases', component: () => import('../views/PlaceholderView.vue'), meta: { title: '案件门户', mvp: 2, platform: true } },
        { path: 'tasks', component: () => import('../views/PlaceholderView.vue'), meta: { title: '任务中心', mvp: 3, platform: true } },
        { path: 'settings', component: () => import('../views/PlaceholderView.vue'), meta: { title: '系统设置', mvp: 5, platform: true } },
        // MVP-1 业务页
        { path: 'c/overview', component: () => import('../views/DashboardView.vue'), meta: { title: '治理仪表盘', mvp: 1 } },
        { path: 'c/clues', component: () => import('../views/ClueListView.vue'), meta: { title: '线索列表', mvp: 1 } },
        { path: 'c/clue/:clueId', component: () => import('../views/ClueDetailView.vue'), meta: { title: '线索详情', mvp: 1 } },
        { path: 'c/audit-chain', component: () => import('../views/AuditChainView.vue'), meta: { title: '审计链', mvp: 1 } },
        // MVP-2 业务页
        { path: 'c/board', component: () => import('../views/BoardView.vue'), meta: { title: '处置看板', mvp: 2 } },
        { path: 'c/verdict', component: () => import('../views/VerdictView.vue'), meta: { title: '实体裁决', mvp: 2 } },
        { path: 'c/profile', component: () => import('../views/ProfileView.vue'), meta: { title: '数据画像', mvp: 2 } },
        // 六分组业务占位（FE-C-026）
        { path: 'c/:section', component: () => import('../views/PlaceholderView.vue') },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.meta.public) {
    return auth.isAuthenticated ? { path: '/' } : true
  }
  if (!auth.isAuthenticated) {
    // 未认证跳登录，保留来源路径
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (!auth.me) {
    try {
      await auth.fetchMe()
    } catch {
      auth.clearSession()
      return { path: '/login' }
    }
  }
  return true
})
