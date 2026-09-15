import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 函数声明（server/app/routers/rule_workshop.py GET /cases/{cid}/functions 契约）。
// S3-F4 只读可视化：参数声明/返回类型/依赖；functions.json 无写路由（D7/E4-1），
// sql 实现文本不外曝。

/** 函数参数声明（string 类型必带 enum 白名单，防注入） */
export interface FunctionParam {
  type: 'integer' | 'decimal' | 'date' | 'boolean' | 'string'
  default?: unknown
  enum?: string[]
  description?: string
  [k: string]: unknown
}

/** 函数声明（functions.json；只读） */
export interface FunctionDecl {
  name: string
  title: string
  inputs: string[]
  output_type: string
  impl: 'sql' | 'py'
  parameters: Record<string, FunctionParam>
  impl_ref?: string
  description?: string
  /** R3 真实依赖声明（py 函数） */
  requires?: { objects?: string[]; links?: string[]; props?: Record<string, string[]> }
  [k: string]: unknown
}

export interface FunctionListResult {
  functions: FunctionDecl[]
  pack: string
}

export const functionsApi = {
  /** GET /cases/{cid}/functions —— 只读目录（无写路由） */
  async list(caseId: string): Promise<FunctionListResult> {
    const res = await api.get<FunctionListResult>(
      `/cases/${encodeURIComponent(caseId)}/functions`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
