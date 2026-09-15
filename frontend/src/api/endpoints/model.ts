import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 对象模型设计器（server/app/routers/model_designer.py 契约）。
// GET/PUT /cases/{cid}/objects 🔒  整体替换 objects 数组（值类型 9 种、kind entity/event）
// GET/PUT /cases/{cid}/links    🔒  整体替换 links 数组（端点引用已声明对象、间类五间）
// POST   /cases/{cid}/validate      整包 load_pack 校验（不写盘）

/** 值类型白名单（core.ontology TYPE_NAMES，9 种） */
export const VALUE_TYPES = [
  'string', 'integer', 'decimal', 'date', 'boolean',
  'timestamp', 'duration_days', 'enum', 'json',
] as const
export type ValueType = (typeof VALUE_TYPES)[number]

/** 属性值映射形态（REQ-D-013/016）：键仅 type/composite/data_element，未知键后端 fail-closed */
export interface PropertySpec {
  type?: ValueType
  composite?: boolean
  data_element?: string
}

/** 五间（生间/反间/因间/死间/内间） */
export const FIVE_JIAN = ['生间', '反间', '因间', '死间', '内间'] as const

/** link 基数（S2 F3 新增声明字段；空 = 未声明，兼容历史数据） */
export const CARDINALITIES = ['one_to_one', 'one_to_many', 'many_to_many'] as const
export type Cardinality = (typeof CARDINALITIES)[number]
export const CARDINALITY_LABEL: Record<Cardinality, string> = {
  one_to_one: '一对一',
  one_to_many: '一对多',
  many_to_many: '多对多',
}

export interface ObjectType {
  name: string
  pk?: string
  kind: 'entity' | 'event'
  name_property?: string
  jian?: string
  /** 属性 → 值类型字符串或映射 {type|composite|data_element}（与 core.ontology_loader 同口径） */
  properties: Record<string, ValueType | PropertySpec>
  [k: string]: unknown
}

export interface LinkType {
  name: string
  from_obj: string
  to_obj: string
  jian?: string
  [k: string]: unknown
}

export interface SaveBody {
  /** 变更理由（FE-T-012，落审计链 note） */
  reason?: string
}

export const modelApi = {
  /** GET /cases/{cid}/objects */
  async listObjects(caseId: string): Promise<{ objects: ObjectType[]; pack: string }> {
    const res = await api.get<{ objects: ObjectType[]; pack: string }>(
      `/cases/${encodeURIComponent(caseId)}/objects`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/objects 🔒 整体替换；校验失败 400 不落盘 */
  async saveObjects(caseId: string, objects: ObjectType[], reason?: string): Promise<{ saved: number; change_level?: string }> {
    const body: SaveBody & { objects: ObjectType[] } = { objects }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: number; change_level?: string }>(
      `/cases/${encodeURIComponent(caseId)}/objects`,
      body,
      { idempotencyAction: 'model-objects-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/links */
  async listLinks(caseId: string): Promise<{ links: LinkType[]; pack: string }> {
    const res = await api.get<{ links: LinkType[]; pack: string }>(
      `/cases/${encodeURIComponent(caseId)}/links`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/links 🔒 整体替换 */
  async saveLinks(caseId: string, links: LinkType[], reason?: string): Promise<{ saved: number; change_level?: string }> {
    const body: SaveBody & { links: LinkType[] } = { links }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: number; change_level?: string }>(
      `/cases/${encodeURIComponent(caseId)}/links`,
      body,
      { idempotencyAction: 'model-links-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/validate 整包校验（合法 {valid:true}；不合法 400 错误原文） */
  async validate(caseId: string): Promise<{ valid: boolean; pack: string }> {
    const res = await api.post<{ valid: boolean; pack: string }>(
      `/cases/${encodeURIComponent(caseId)}/validate`,
      {},
    )
    return res.data
  },
}
