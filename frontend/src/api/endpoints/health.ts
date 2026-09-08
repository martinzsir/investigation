import { api } from '../client'

export interface HealthInfo {
  status: string
  service: string
  version: string
  meta: string
}

export const healthApi = {
  /** FE-I-015：免认证探针（S5）。不可达返回 null，由 health store 映射全局异常态 */
  async probe(): Promise<HealthInfo | null> {
    try {
      const { data } = await api.get<HealthInfo>('/health', { skipAuthHook: true })
      return data
    } catch {
      return null
    }
  },
}
