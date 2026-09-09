import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { TaskRow } from '../../domain/task'

// 规则工坊（server/app/routers/rule_workshop.py 契约）。
// GET  /cases/{cid}/rules        规则列表 + function_catalog（函数目录无独立端点）
// PUT  /cases/{cid}/rules/{rid}  🔒 仅 rule_text/params/enabled 三字段可改；reason 落审计链
// POST /cases/{cid}/rules/draft  🔒 LLM 守卫层（默认 503 LLM_DISABLED / 无通道 503）

/** 规则声明（rules.json；function/stage/hit_when 等结构字段只读） */
export interface Rule {
  id: string
  title?: string
  rule_text: string
  function: string
  params?: Record<string, unknown>
  enabled?: boolean
  stage?: string
  hit_when?: string
  jian_types?: string[]
  dimension?: string[]
  [k: string]: unknown
}

export interface RuleListResult {
  rules: Rule[]
  /** 函数白名单目录（工坊下区挂参只读参考） */
  function_catalog: string[]
  pack: string
}

/** PUT 可改字段（结构字段提交即 400） */
export interface RuleEditBody {
  rule_text?: string
  params?: Record<string, unknown>
  enabled?: boolean
  /** 变更理由（FE-T-012：params/enabled 危险项必填，落审计链 note） */
  reason?: string
}

export interface RuleEditResult {
  rule_id: string
  changed: Array<'rule_text' | 'params' | 'enabled'>
  /** params/enabled 变更自动入队 RESCAN；纯 rule_text 修订为 null */
  rescan_task: TaskRow | null
}

export interface RuleDraftBody {
  question: string
  model_output?: Record<string, unknown> | null
  model?: string
}

export interface RuleDraftResult {
  rule_text: string
  review_status: string
  persisted: boolean
  redacted_question?: string
  redaction?: unknown
  model?: string
  note?: string
}

export const rulesApi = {
  /** GET /cases/{cid}/rules */
  async list(caseId: string): Promise<RuleListResult> {
    const res = await api.get<RuleListResult>(
      `/cases/${encodeURIComponent(caseId)}/rules`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/rules/{rid} 🔒 偏将及以上；校验失败 400 不落盘 */
  async update(caseId: string, ruleId: string, body: RuleEditBody): Promise<RuleEditResult> {
    const res = await api.put<RuleEditResult>(
      `/cases/${encodeURIComponent(caseId)}/rules/${encodeURIComponent(ruleId)}`,
      body,
      { idempotencyAction: `rule-edit:${ruleId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/rules/draft 🔒 LLM 守卫（503=未启用/无通道，产物永不落盘） */
  async draft(caseId: string, body: RuleDraftBody): Promise<RuleDraftResult> {
    const res = await api.post<RuleDraftResult>(
      `/cases/${encodeURIComponent(caseId)}/rules/draft`,
      body,
    )
    return res.data
  },
}
