import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 权限与字段遮蔽（server/app/routers/access_config.py + views.py 契约）。
// GET/PUT /cases/{cid}/policies 🔒 三数组整体替换；未声明对象 fail-closed；保存即生效
// GET/PUT /cases/{cid}/views    🔒 角色视图整体替换；base_object/properties 须已声明

export type MaskMode = 'partial' | 'full' | 'none'

export interface ObjectPolicy {
  object: string
  roles: string[]
  min_clearance: number
}

export interface LinkPolicy {
  link: string
  roles: string[]
  min_clearance: number
}

export interface PropertyPolicy {
  object: string
  property: string
  default: 'allow' | 'deny'
  allow_roles?: string[]
  mask?: MaskMode
}

export interface PoliciesDoc {
  object_policies: ObjectPolicy[]
  link_policies: LinkPolicy[]
  property_policies: PropertyPolicy[]
  pack?: string
}

export interface PoliciesSaveBody extends PoliciesDoc {
  /** 变更理由（FE-T-012，落审计链 note） */
  reason?: string
}

export interface ViewDef {
  name: string
  base_object: string
  properties: string[]
  roles: string[]
  description?: string
}

export const policiesApi = {
  /** GET /cases/{cid}/policies */
  async get(caseId: string): Promise<PoliciesDoc> {
    const res = await api.get<PoliciesDoc>(
      `/cases/${encodeURIComponent(caseId)}/policies`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/policies 🔒 三数组整体替换；校验失败 400 不落盘 */
  async save(caseId: string, doc: Omit<PoliciesDoc, 'pack'>, reason?: string): Promise<{ saved: boolean }> {
    const body: PoliciesSaveBody = { ...doc }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: boolean }>(
      `/cases/${encodeURIComponent(caseId)}/policies`,
      body,
      { idempotencyAction: 'policies-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/views */
  async listViews(caseId: string): Promise<{ views: ViewDef[]; pack: string }> {
    const res = await api.get<{ views: ViewDef[]; pack: string }>(
      `/cases/${encodeURIComponent(caseId)}/views`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/views 🔒 整体替换 */
  async saveViews(caseId: string, views: ViewDef[], reason?: string): Promise<{ saved: number }> {
    const body: { views: ViewDef[]; reason?: string } = { views }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: number }>(
      `/cases/${encodeURIComponent(caseId)}/views`,
      body,
      { idempotencyAction: 'views-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
