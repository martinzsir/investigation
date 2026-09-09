import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 数据元（server/app/routers/etl.py W-P-011 契约）。
// GET/PUT /cases/{cid}/data-elements 🔒 整包透传/回写 data_elements.json；
// reason 为审计链留痕字段，PUT 时由后端剥离、不落数据文件。

export interface DataElement {
  name: string
  /** 值类型：string/integer/decimal/date/boolean 等 */
  type: string
  length?: number
  format?: string
  checksum?: string
  sensitive?: boolean
  mask?: 'partial' | 'full' | 'none'
  /** 代码表枚举值 */
  enum?: string[]
  /** 枚举归属维度（关联 enum_space） */
  enum_space_dim?: string
  [k: string]: unknown
}

/** 整包：{elements: {DE_CODE: DataElement}, ...}（直出 data_elements.json） */
export type DataElementsDoc = {
  elements: Record<string, DataElement>
  [k: string]: unknown
}

export const dataElementsApi = {
  /** GET /cases/{cid}/data-elements */
  async get(caseId: string): Promise<DataElementsDoc> {
    const res = await api.get<DataElementsDoc>(
      `/cases/${encodeURIComponent(caseId)}/data-elements`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/data-elements 🔒 全量更新；reason 不落数据文件 */
  async save(caseId: string, doc: DataElementsDoc, reason?: string): Promise<{ updated: boolean }> {
    const body: Record<string, unknown> = { ...doc }
    if (reason) body.reason = reason
    const res = await api.put<{ updated: boolean }>(
      `/cases/${encodeURIComponent(caseId)}/data-elements`,
      body,
      { idempotencyAction: 'data-elements-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
