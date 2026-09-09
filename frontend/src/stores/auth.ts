import { defineStore } from 'pinia'
import { authApi, type MeInfo } from '../api/endpoints/auth'
import { clearToken, getToken, setToken } from '../api/token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken() ?? '',
    me: null as MeInfo | null,
  }),
  getters: {
    isAuthenticated: (s) => s.token !== '',
    operator: (s) => s.me?.operator ?? '',
    role: (s) => s.me?.role ?? '',
    clearance: (s) => s.me?.clearance ?? 0,
    tenantId: (s) => s.me?.tenant_id ?? '',
    /** 平台管理员（设置页管理面闸门；W-024，后端 /auth/me 透出） */
    isAdmin: (s) => s.me?.is_admin === true,
  },
  actions: {
    /** FE-I-008：登录成功 token 存内存 + sessionStorage（D9）；失败收敛统一文案（红线八） */
    async login(operator: string, password: string): Promise<boolean> {
      try {
        const s = await authApi.login(operator, password)
        this.token = s.token
        setToken(s.token)
        await this.fetchMe()
        return true
      } catch {
        this.clearSession()
        return false
      }
    },

    async fetchMe(): Promise<void> {
      this.me = await authApi.me()
    },

    async logout(): Promise<void> {
      try {
        await authApi.logout()
      } catch {
        // 会话已失效也照常清本地
      }
      this.clearSession()
    },

    clearSession(): void {
      this.token = ''
      this.me = null
      clearToken()
    },
  },
})
