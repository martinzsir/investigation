import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// S5 通用 schema 驱动表单后端契约（server/app/routers/ontology_generic.py）。
// 🔴 R4：提案无任何自动发布路径；发布仅 publishProposal 一个人工入口。
//    E4-1 未看影响面发布被后端拒；E4-3 影响面基准变化发布被后端拒。
//    R6：llm_policy 只读（writable=false / form_enabled=false），永不渲染表单。

export type ImpactStatus = 'complete' | 'partial' | 'failed'
export type Certainty = 'certain' | 'uncertain'
export type CategoryStatus = 'ok' | 'unavailable'
export type ImpactCategoryKey = 'clues' | 'rules' | 'views' | 'tables' | 'functions'
export type ProposalStatus =
  | 'draft'
  | 'impact_ready'
  | 'published'
  | 'discarded'

export interface ImpactChange {
  kind: string
  ref: string
  label: string
}

export interface ImpactItem {
  category: string
  kind: string
  id: string
  title: string
  certainty: Certainty
  refs: string[]
  via: string
  status: string | null
  /** R2：处置真值为「已固证/已立案」的线索单独标记 */
  solidified: boolean
}

export interface ImpactCategory {
  status: CategoryStatus
  items: ImpactItem[]
}

export interface DynamicRef {
  file: string
  token: string
  ref: string
  reason: string
}

export interface ImpactFailure {
  category: string
  reason: string
}

export interface StandardLayer {
  layer: 'shared' | 'industry' | null
  affected_cases: number
  industry: string | null
}

export interface ImpactResult {
  file: string
  status: ImpactStatus
  changes: ImpactChange[]
  categories: Record<ImpactCategoryKey, ImpactCategory>
  dynamic_refs: DynamicRef[]
  failures: ImpactFailure[]
  totals: Record<string, number>
  coverage_note: string
  standard_layer?: StandardLayer
}

export interface GenericFileDto {
  file: string
  writable: boolean
  /** false = 永不生成表单（llm_policy，R6），只做只读 JSON 展示 */
  form_enabled: boolean
  schema: JsonSchema | null
  doc: Record<string, unknown>
}

export interface ProposalDto {
  proposal_id: string
  file: string
  status: ProposalStatus
  reason: string
  author: string
  created_at: string
  updated_at: string
  published_at: string | null
  published_by: string | null
  discarded_by: string | null
  impact: ImpactResult | null
}

export interface PublishResult {
  published: boolean
  proposal_id: string
  ontology_version: string
  needs_rebuild: boolean
}

/** draft-07 子集（本仓 19 个 schema 实际用到的关键字） */
export interface JsonSchema {
  type?: string | string[]
  const?: unknown
  enum?: unknown[]
  properties?: Record<string, JsonSchema>
  patternProperties?: Record<string, JsonSchema>
  required?: string[]
  items?: JsonSchema
  additionalProperties?: boolean | JsonSchema
  oneOf?: JsonSchema[]
  anyOf?: JsonSchema[]
  pattern?: string
  minLength?: number
  minimum?: number
  minItems?: number
  description?: string
  title?: string
  default?: unknown
  [k: string]: unknown
}

export const ontologyGenericApi = {
  /** GET /cases/{cid}/ontology/files/{name} */
  async getFile(caseId: string, name: string): Promise<GenericFileDto> {
    const res = await api.get<GenericFileDto>(
      `/cases/${encodeURIComponent(caseId)}/ontology/files/${encodeURIComponent(name)}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../impact 影响面预览（不落任何状态，查看权限=案件可访问） */
  async previewImpact(
    caseId: string,
    file: string,
    doc: Record<string, unknown>,
  ): Promise<{ impact: ImpactResult; fingerprint: string }> {
    const res = await api.post<{ impact: ImpactResult; fingerprint: string }>(
      `/cases/${encodeURIComponent(caseId)}/ontology/impact`,
      { file, doc },
    )
    return res.data
  },

  /** POST .../proposals ① 提出（草稿，不生效；理由必填，后端强制） */
  async createProposal(
    caseId: string,
    file: string,
    doc: Record<string, unknown>,
    reason: string,
  ): Promise<ProposalDto> {
    const res = await api.post<ProposalDto>(
      `/cases/${encodeURIComponent(caseId)}/ontology/proposals`,
      { file, doc, reason },
      { idempotencyAction: `ontology-proposal-create-${file}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET .../proposals/{pid} */
  async getProposal(caseId: string, pid: string): Promise<ProposalDto> {
    const res = await api.get<ProposalDto>(
      `/cases/${encodeURIComponent(caseId)}/ontology/proposals/${encodeURIComponent(pid)}`,
    )
    return res.data
  },

  /** POST .../proposals/{pid}/impact ② 服务端计算并挂载影响面（客户端不可自报） */
  async proposalImpact(caseId: string, pid: string): Promise<ProposalDto> {
    const res = await api.post<ProposalDto>(
      `/cases/${encodeURIComponent(caseId)}/ontology/proposals/${encodeURIComponent(pid)}/impact`,
      undefined,
      { idempotencyAction: `ontology-proposal-impact-${pid}` },
    )
    return res.data
  },

  /** POST .../proposals/{pid}/publish ③ 🔴 唯一人工发布入口 */
  async publishProposal(caseId: string, pid: string): Promise<PublishResult> {
    const res = await api.post<PublishResult>(
      `/cases/${encodeURIComponent(caseId)}/ontology/proposals/${encodeURIComponent(pid)}/publish`,
      undefined,
      { idempotencyAction: `ontology-proposal-publish-${pid}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../proposals/{pid}/discard 废弃（本体不变，UC-S5-18） */
  async discardProposal(caseId: string, pid: string): Promise<ProposalDto> {
    const res = await api.post<ProposalDto>(
      `/cases/${encodeURIComponent(caseId)}/ontology/proposals/${encodeURIComponent(pid)}/discard`,
      undefined,
      { idempotencyAction: `ontology-proposal-discard-${pid}` },
    )
    return res.data
  },
}
