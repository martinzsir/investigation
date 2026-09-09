import { api } from '../client'

// 代码逃生舱（server/app/routers/escape_hatch.py + worker/escape_hatch.py 契约）。
// 仅生成文本代码桩（不写盘、不注册、不执行）；无角色门槛（登录即可，P2 页面）。

export type ExtType = 'function' | 'value_type' | 'clean_rule' | 'side_effect'

export interface StubFile {
  path: string
  content: string
}

export interface GenerateBody {
  ext_type: ExtType
  name: string // 1–64
  description?: string // 0–500
}

export interface GenerateResult {
  files: StubFile[]
  registration_points: string[]
}

export interface HatchStats {
  items: { ext_type: string; count: number }[]
  total: number
}

export const escapeHatchApi = {
  /** POST /escape-hatch/generate */
  async generate(body: GenerateBody): Promise<GenerateResult> {
    const { data } = await api.post<GenerateResult>('/escape-hatch/generate', body)
    return data
  },

  /** GET /escape-hatch/stats —— 全局聚合最近 1000 条事件（无分页/无租户过滤） */
  async stats(): Promise<HatchStats> {
    const { data } = await api.get<HatchStats>('/escape-hatch/stats')
    return data
  },
}
