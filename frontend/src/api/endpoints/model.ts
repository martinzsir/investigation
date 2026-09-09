import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 对象模型设计器（server/app/routers/model_designer.py 契约）。
// GET/PUT /cases/{cid}/objects 🔒  整体替换 objects 数组（值类型 5 种、kind entity/event）
// GET/PUT /cases/{cid}/links    🔒  整体替换 links 数组（端点引用已声明对象、间类五间）
// POST   /cases/{cid}/validate      整包 load_pack 校验（不写盘）

/** 值类型白名单（core.ontology TYPE_NAMES） */
export const VALUE_TYPES = ['string', 'integer', 'decimal', 'date', 'boolean'] as const
export type ValueType = (typeof VALUE_TYPES)[number]

/** 五间（生间/反间/因间/死间/内间） */
export const FIVE_JIAN = ['生间', '反间', '因间', '死间', '内间'] as const

export interface ObjectType {
  name: string
  pk?: string
  kind: 'entity' | 'event'
  name_property?: string
  jian?: string
  /** 属性 → 值类型（string/integer/decimal/date/boolean） */
  properties: Record<string, string>
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
  async saveObjects(caseId: string, objects: ObjectType[], reason?: string): Promise<{ saved: number }> {
    const body: SaveBody & { objects: ObjectType[] } = { objects }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: number }>(
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
  async saveLinks(caseId: string, links: LinkType[], reason?: string): Promise<{ saved: number }> {
    const body: SaveBody & { links: LinkType[] } = { links }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: number }>(
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
