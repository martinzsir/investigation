import { defineStore } from 'pinia'
import { healthApi } from '../api/endpoints/health'

export type HealthState = 'checking' | 'ok' | 'unreachable'

export const useHealthStore = defineStore('health', {
  state: () => ({
    state: 'checking' as HealthState,
    version: '',
    /** 元数据层降级（status==='degraded'）：全局条警示 */
    degraded: false,
  }),
  actions: {
    /** FE-I-015：启动/网络恢复时探 GET /api/v1/health（免认证）；
     *  不通 → 全局异常态"无法连接服务端"，不发业务请求 */
    async probe(): Promise<void> {
      this.state = 'checking'
      const info = await healthApi.probe()
      if (!info) {
        this.state = 'unreachable'
        this.version = ''
        this.degraded = false
        return
      }
      this.state = 'ok'
      this.version = info.version ?? ''
      this.degraded = info.status === 'degraded'
    },
  },
})
