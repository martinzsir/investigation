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
        // 平台页（23b 案件门户 / 24 任务中心 / 25 系统设置不入侧栏）
        { path: 'cases', component: () => import('../views/PortalView.vue'), meta: { title: '案件门户', mvp: 5, platform: true } },
        // MVP-3 任务中心（平台页，头部任务图标入口）
        { path: 'tasks', component: () => import('../views/TaskCenterView.vue'), meta: { title: '任务中心', mvp: 3, platform: true } },
        { path: 'settings', component: () => import('../views/SettingsView.vue'), meta: { title: '系统设置', mvp: 5, platform: true } },
        // MVP-1 业务页
        { path: 'c/overview', component: () => import('../views/DashboardView.vue'), meta: { title: '治理仪表盘', mvp: 1 } },
        { path: 'c/clues', component: () => import('../views/ClueListView.vue'), meta: { title: '线索列表', mvp: 1 } },
        { path: 'c/clue/:clueId', component: () => import('../views/ClueDetailView.vue'), meta: { title: '线索详情', mvp: 1 } },
        { path: 'c/audit-chain', component: () => import('../views/AuditChainView.vue'), meta: { title: '审计链', mvp: 1 } },
        // MVP-2 业务页
        { path: 'c/board', component: () => import('../views/BoardView.vue'), meta: { title: '处置看板', mvp: 2 } },
        { path: 'c/verdict', component: () => import('../views/VerdictView.vue'), meta: { title: '实体裁决', mvp: 2 } },
        { path: 'c/profile', component: () => import('../views/ProfileView.vue'), meta: { title: '数据画像', mvp: 2 } },
        // MVP-3 业务页（数据接入）
        { path: 'c/wizard', component: () => import('../views/IngestView.vue'), meta: { title: '接入向导', mvp: 3 } },
        { path: 'c/suggest', component: () => import('../views/SuggestView.vue'), meta: { title: '接入建议', mvp: 3 } },
        // MVP-4 业务页（研判模型：规则工坊/模型设计器/知识包/权限遮蔽）
        { path: 'c/rules', component: () => import('../views/RuleWorkshopView.vue'), meta: { title: '规则工坊', mvp: 4 } },
        { path: 'c/designer', component: () => import('../views/ModelDesignerView.vue'), meta: { title: '模型设计器', mvp: 4 } },
        { path: 'c/knowledge', component: () => import('../views/KnowledgeView.vue'), meta: { title: '知识包', mvp: 4 } },
        { path: 'c/masking', component: () => import('../views/PolicyMaskingView.vue'), meta: { title: '权限与遮蔽', mvp: 4 } },
        // MVP-4 业务页（数据治理：数据元/ETL/映射校验/质量检查/隔离区）
        { path: 'c/data-elements', component: () => import('../views/DataElementsView.vue'), meta: { title: '数据元', mvp: 4 } },
        { path: 'c/etl', component: () => import('../views/EtlPipelineView.vue'), meta: { title: 'ETL 管道', mvp: 4 } },
        { path: 'c/mapping', component: () => import('../views/MappingView.vue'), meta: { title: '映射校验', mvp: 4 } },
        { path: 'c/quality', component: () => import('../views/QualityView.vue'), meta: { title: '质量检查', mvp: 4 } },
        { path: 'c/quarantine', component: () => import('../views/QuarantineView.vue'), meta: { title: '隔离区', mvp: 4 } },
        // MVP-5 业务页（庙算/图谱/跨案/案件包/逃生舱）
        { path: 'c/miaosuan', component: () => import('../views/MiaoSuanView.vue'), meta: { title: '庙算工作台', mvp: 5 } },
        { path: 'c/graph', component: () => import('../views/GraphView.vue'), meta: { title: '知识图谱', mvp: 5 } },
        { path: 'c/cross-case', component: () => import('../views/CrossCaseView.vue'), meta: { title: '跨案件查询', mvp: 5 } },
        { path: 'c/package', component: () => import('../views/PackageView.vue'), meta: { title: '案件包', mvp: 5 } },
        { path: 'c/escape', component: () => import('../views/EscapeHatchView.vue'), meta: { title: '代码逃生舱', mvp: 5 } },
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
