import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { CompareRow, LlmInference } from '../../domain/review'

// W-021 实体裁决（server/app/routers/review.py 契约）。
// 红线：合并/驳回均为人工裁决——前端永不自动合并；驳回理由必填（后端 400 双保险）。

/** 队列候选（core/entity.py OrgCluster.to_dict()） */
export interface ReviewCandidate {
  entity_id: string
  canonical_name: string
  variants: string[]
  evidence: {
    common_credit_codes?: string[]
    common_legal_reps?: string[]
    common_addresses?: string[]
    common_accounts?: string[]
    common_source_rows?: string[]
  }
  confidence: number
  needs_review: boolean
  merge_reason: string
}

export interface ReviewQueue {
  items: ReviewCandidate[]
  total: number
  /** mock 扩展：历史裁决记录（后端暂无此端点时缺省） */
  history?: ReviewHistoryItem[]
  note?: string
}

/** 证据详情双栏属性行（适配后；后端 per_variant_attributes 派生） */
export interface ReviewAttributeRow extends CompareRow {
  mask?: 'phone' | 'idcard' | 'text'
  policy?: 'visible' | 'masked' | 'denied'
}

/** 后端 per_variant_attributes 原始行结构 */
interface BackendAttributeRow {
  label: string
  values: Record<string, string>
  basis: boolean
}

/** 后端原始证据响应（review_evidence 返回结构） */
export interface ReviewEvidenceRaw {
  candidate_id: string
  canonical_name: string
  variants: string[]
  confidence: number
  merge_reason: string
  evidence: ReviewCandidate['evidence']
  /** 后端 per_variant_attributes（W-021b） */
  attributes?: BackendAttributeRow[]
  /** mock 扩展：旧格式平坦行（mock 兼容） */
  attributeRows?: ReviewAttributeRow[]
  llm_inferences?: LlmInference[]
}

export interface ReviewEvidence {
  candidate_id: string
  canonical_name: string
  variants: string[]
  confidence: number
  merge_reason: string
  evidence: ReviewCandidate['evidence']
  /** 适配后的属性行（后端 evidence.common_* 派生，或 mock 直传） */
  attributes: ReviewAttributeRow[]
  /** LLM 判读参考（后端无此能力时为空数组） */
  llm_inferences: LlmInference[]
}

export interface ReviewHistoryItem {
  candidate_id: string
  canonical_name: string
  action: 'merge' | 'reject'
  confidence: number
  operator: string
  reason?: string
  occurred_at: string
}

export interface ReviewDecisionBody {
  action: 'review_merge' | 'review_reject'
  reason?: string
}

export interface ReviewDecisionResult {
  task_id: string
  action: string
  candidate_id: string
}

/** 后端 per_variant_attributes → 前端双栏对比行 */
function adaptAttributes(
  raw: BackendAttributeRow[] | undefined,
  variants: string[],
  ev: ReviewCandidate['evidence'],
): ReviewAttributeRow[] {
  // 优先用后端 per_variant_attributes（W-021b）
  if (raw && raw.length > 0) {
    // 取前两个变体做双栏对比（候选 vs 已归集）
    const leftName = variants[0] ?? '候选'
    const rightName = variants[1] ?? '已归集'
    // 敏感属性遮蔽
    const MASK_MAP: Record<string, 'text' | 'idcard'> = {
      '统一社会信用代码': 'text',
      '银行账号': 'text',
    }
    const POLICY_MAP: Record<string, 'visible' | 'masked'> = {
      '统一社会信用代码': 'masked',
      '银行账号': 'masked',
    }
    return raw.map((row) => ({
      label: row.label,
      left: row.values[leftName] ?? '—',
      right: row.values[rightName] ?? '—',
      basis: row.basis,
      mask: MASK_MAP[row.label],
      policy: POLICY_MAP[row.label],
    }))
  }
  // 回落：从 evidence.common_* 派生（共享值，left=right）
  if (!ev) return []
  const rows: ReviewAttributeRow[] = []
  const LABELS: Array<[keyof typeof ev, string, 'text' | 'idcard', 'visible' | 'masked']> = [
    ['common_credit_codes', '统一社会信用代码', 'text', 'masked'],
    ['common_legal_reps', '法定代表人', 'text', 'visible'],
    ['common_addresses', '注册地址', 'text', 'visible'],
    ['common_accounts', '共有账户', 'text', 'masked'],
  ]
  for (const [key, label, mask, policy] of LABELS) {
    const vals = ev[key]
    if (vals && vals.length > 0) {
      const joined = vals.join(' / ')
      rows.push({ label, left: joined, right: joined, mask, policy })
    }
  }
  return rows
}

/** 后端原始响应 → 前端消费结构 */
function adaptEvidence(raw: ReviewEvidenceRaw): ReviewEvidence {
  return {
    candidate_id: raw.candidate_id,
    canonical_name: raw.canonical_name,
    variants: raw.variants,
    confidence: raw.confidence,
    merge_reason: raw.merge_reason,
    evidence: raw.evidence,
    // mock 直传 attributeRows 优先（测试兼容），否则适配后端 attributes
    attributes: raw.attributeRows ?? adaptAttributes(raw.attributes, raw.variants, raw.evidence),
    llm_inferences: raw.llm_inferences ?? [],
  }
}

export const reviewApi = {
  /**
   * GET /cases/{cid}/review/queue —— 待裁决候选队列（服务端分页）。
   * 已裁决候选从 state.sqlite 排除。
   */
  async queue(caseId: string, page = 1, pageSize = 20): Promise<ReviewQueue> {
    const res = await api.get<ReviewQueue>(
      `/cases/${encodeURIComponent(caseId)}/review/queue?page=${page}&page_size=${pageSize}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * GET /cases/{cid}/review/history —— 已裁决记录（从 state.sqlite 读取）。
   * 前端 VerdictView 初始化时获取，裁决后乐观更新。
   */
  async history(caseId: string): Promise<{ items: ReviewHistoryItem[]; total: number }> {
    const res = await api.get<{ items: ReviewHistoryItem[]; total: number }>(
      `/cases/${encodeURIComponent(caseId)}/review/history`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/review/{rid}/evidence —— 双栏对比证据（适配 common_* → attributes） */
  async evidence(caseId: string, rid: string): Promise<ReviewEvidence> {
    const res = await api.get<ReviewEvidenceRaw>(
      `/cases/${encodeURIComponent(caseId)}/review/${encodeURIComponent(rid)}/evidence`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return adaptEvidence(res.data)
  },

  /**
   * POST /cases/{cid}/review/{rid}/decision 🔒（入队 TASK_REVIEW）。
   * 幂等键 review:{rid}:{action}（服务端同键去重）。
   */
  async decide(
    caseId: string,
    rid: string,
    body: ReviewDecisionBody,
  ): Promise<ReviewDecisionResult> {
    const res = await api.post<ReviewDecisionResult>(
      `/cases/${encodeURIComponent(caseId)}/review/${encodeURIComponent(rid)}/decision`,
      body,
      { idempotencyAction: `review-decision:${rid}:${body.action}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
