import { api } from '../client'

export interface SessionInfo {
  token: string
  expires_at: string
  operator: string
  role: string
  clearance: number
  tenant_id: string
  /** 平台管理员（is_admin=1 或 system 角色；W-024，后端 A2 增量字段） */
  is_admin: boolean
}

export interface MeInfo {
  operator: string
  role: string
  clearance: number
  tenant_id: string
  /** 平台管理员（与 login 同口径；设置页据此渲染锁定面板，不靠 403 探测） */
  is_admin: boolean
}

/**
 * FE-I-008：登录/登出/会话刷新。
 * 登录体只带凭据（operator/password）；其后一切业务请求绝不传 operator——
 * 服务端只取会话（红线，传了也被忽略）。role/clearance 取自 /auth/me。
 */
export const authApi = {
  async login(operator: string, password: string): Promise<SessionInfo> {
    const { data } = await api.post<SessionInfo>(
      '/auth/login',
      { operator, password },
      { skipAuthHook: true },
    )
    return data
  },

  async logout(): Promise<void> {
    await api.post('/auth/logout', undefined, { skipAuthHook: true })
  },

  async me(): Promise<MeInfo> {
    const { data } = await api.get<MeInfo>('/auth/me')
    return data
  },
}
