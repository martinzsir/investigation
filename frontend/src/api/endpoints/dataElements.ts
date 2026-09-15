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

/** 行业层整包：额外带行业名（pack_meta.json industry；无行业层为 null） */
export interface IndustryDataElementsDoc extends DataElementsDoc {
  industry: string | null
}

/** 表单枚举（S3-F1，后端单一事实源：GET /data-elements/enums） */
export interface DataElementEnums {
  types: string[]
  checksums: string[]
  clean_rules: string[]
  masks: string[]
}

/** 引用位置（objects.json 的 data_element 绑定，E1-3/UC-S3-6） */
export interface DataElementRefLocation {
  object: string
  property: string
}

export interface DataElementReferences {
  references: Array<{ object: string; property: string; data_element: string }>
  by_element: Record<string, DataElementRefLocation[]>
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

  /** GET /cases/{cid}/data-elements-shared —— 全域层（_shared）只读；S2 建模器 F2 分组下拉 */
  async listShared(caseId: string): Promise<DataElementsDoc> {
    const res = await api.get<DataElementsDoc>(
      `/cases/${encodeURIComponent(caseId)}/data-elements-shared`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/data-elements-industry —— 行业层（_industry/<行业>）只读；S2 分组下拉第三层 */
  async listIndustry(caseId: string): Promise<IndustryDataElementsDoc> {
    const res = await api.get<IndustryDataElementsDoc>(
      `/cases/${encodeURIComponent(caseId)}/data-elements-industry`,
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

  /** GET /cases/{cid}/data-elements/enums —— 表单枚举（S3-F1 单一事实源） */
  async enums(caseId: string): Promise<DataElementEnums> {
    const res = await api.get<DataElementEnums>(
      `/cases/${encodeURIComponent(caseId)}/data-elements/enums`,
    )
    return res.data
  },

  /** GET /cases/{cid}/data-elements/references —— 引用扫描（E1-3 删除警告） */
  async references(caseId: string): Promise<DataElementReferences> {
    const res = await api.get<DataElementReferences>(
      `/cases/${encodeURIComponent(caseId)}/data-elements/references`,
    )
    return res.data
  },

  /** PUT /cases/{cid}/data-elements-shared 🔒 全域层整包（本体管理员；R3 影响所有案件） */
  async saveShared(caseId: string, doc: DataElementsDoc, reason?: string): Promise<{ updated: boolean }> {
    const body: Record<string, unknown> = { ...doc }
    if (reason) body.reason = reason
    const res = await api.put<{ updated: boolean }>(
      `/cases/${encodeURIComponent(caseId)}/data-elements-shared`,
      body,
      { idempotencyAction: 'data-elements-shared-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/data-elements-industry 🔒 行业层整包（本体管理员；R3 同全域层） */
  async saveIndustry(caseId: string, doc: DataElementsDoc, reason?: string): Promise<{ updated: boolean; industry?: string }> {
    const body: Record<string, unknown> = { ...doc }
    if (reason) body.reason = reason
    const res = await api.put<{ updated: boolean; industry?: string }>(
      `/cases/${encodeURIComponent(caseId)}/data-elements-industry`,
      body,
      { idempotencyAction: 'data-elements-industry-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
