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

/** 证据详情双栏属性行（mock 扩展字段；后端 evidence 为聚合键值） */
export interface ReviewAttributeRow extends CompareRow {
  mask?: 'phone' | 'idcard' | 'text'
  policy?: 'visible' | 'masked' | 'denied'
}

export interface ReviewEvidence {
  candidate_id: string
  canonical_name: string
  variants: string[]
  confidence: number
  merge_reason: string
  evidence: ReviewCandidate['evidence']
  attributes?: ReviewAttributeRow[]
  /** LLM 判读参考（FE-C-023 三件套卡片；同源去重 FE-T-007 展示载体） */
  llm_inferences?: LlmInference[]
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

export const reviewApi = {
  /** GET /cases/{cid}/review/queue —— 待裁决候选队列（已裁决从 state.sqlite 排除） */
  async queue(caseId: string, page = 1, pageSize = 20): Promise<ReviewQueue> {
    const res = await api.get<ReviewQueue>(
      `/cases/${encodeURIComponent(caseId)}/review/queue?page=${page}&page_size=${pageSize}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/review/{rid}/evidence —— 双栏对比证据 */
  async evidence(caseId: string, rid: string): Promise<ReviewEvidence> {
    const res = await api.get<ReviewEvidence>(
      `/cases/${encodeURIComponent(caseId)}/review/${encodeURIComponent(rid)}/evidence`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
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
