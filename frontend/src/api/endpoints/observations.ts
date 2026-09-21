// 观察档案（镜头产出；server/app/routers/observations.py 契约）。
//
// 观察 ≠ 线索：**镜头只摆出数据的某种结构，不下"这是异常"的判断**。
// 分界线不在确定性（规则同样 deterministic），而在**有没有常态基线**：
//   规则 = 跟常态中位数比 → 回答"算不算异常"（判定，可证伪）
//   镜头 = 结构过滤     → 回答"结构存不存在"（观测，不是命题）
// 故观察单列一套端点，不进处置清单、不占看板计数。
//
// GET /cases/{cid}/observations              列表（镜头/主体/处置态筛选）
// GET /cases/{cid}/observations/{obs_id}     详情（判据 + 证伪 + 事实明细）

import { api } from '../client'
import { noteDataVersion } from '../query-keys'

/** 事实明细（人能读的事件摘要；判据的支撑行） */
export interface ObservationFact {
  date: string | null
  offset_days: number | null
  type: string | null
  src_object: string | null
  role: string | null
  brief: string | null
  event_pk: string | null
}

/** 处置态：正兵对观察的操作（跨版本持久，落 state.sqlite） */
export type ObservationDisposition = '未认领' | '已认领' | '已归档' | '已提升'

export interface ObservationItem {
  observation_id: string
  skill_id: string
  lens_name: string
  title: string
  subject: string
  project: string
  /** 判据：陈述观测（不作定性） */
  basis: string
  /** 证伪条件：什么情况下这不成立 */
  falsification: string
  facts_count: number
  evidence_count: number
  degraded: boolean
  degraded_reason: string
  param_source: string
  created_at: string
  disposition: ObservationDisposition
  note: string
  operator: string
  promoted_clue_id: string
  promoted_hypothesis: string
  /** 认领过但已不在当前版本（重扫后靶心/数据变了）——不隐藏，显式标注 */
  stale?: boolean
}

export interface ObservationStats {
  total: number
  unclaimed: number
  claimed: number
  archived: number
  promoted: number
  by_lens: Record<string, number>
  stale: number
}

export interface ObservationListResult {
  version: number
  observations: ObservationItem[]
  /** 跨版本遗留：认领过但不在当前版本档案里 */
  stale: ObservationItem[]
  stats: ObservationStats
  page: number
  page_size: number
  total: number
}

export interface ObservationDetailResult {
  observation_id: string
  skill_id: string
  lens_name: string
  title: string
  subject: string
  project: string
  basis: string
  falsification: string
  claims: string[]
  facts: ObservationFact[]
  evidence_refs: Record<string, unknown>[]
  detail: Record<string, unknown>
  param_source: string
  degraded: boolean
  degraded_reason: string
  created_at: string
  disposition: ObservationDisposition
  note: string
  operator: string
  promoted_clue_id: string
  promoted_hypothesis: string
}

/** 假设选项（本体声明，换本体自动跟随；不硬编码 H1..Hn） */
export interface HypothesisChoice {
  id: string
  description: string
  falsification: string
  dimension: string[]
}

export const observationsApi = {
  /** GET /cases/{cid}/observations —— 镜头/主体/处置态筛选 + 统计 */
  async list(
    caseId: string,
    params: {
      skill?: string
      subject?: string
      disposition?: string
      page?: number
      page_size?: number
    } = {},
  ): Promise<ObservationListResult> {
    const q = new URLSearchParams()
    if (params.skill) q.set('skill', params.skill)
    if (params.subject) q.set('subject', params.subject)
    if (params.disposition) q.set('disposition', params.disposition)
    if (params.page) q.set('page', String(params.page))
    if (params.page_size) q.set('page_size', String(params.page_size))
    const qs = q.toString()
    const res = await api.get<ObservationListResult>(
      `/cases/${encodeURIComponent(caseId)}/observations${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/observations/{obs_id} —— 详情 */
  async detail(caseId: string, observationId: string): Promise<ObservationDetailResult> {
    const res = await api.get<ObservationDetailResult>(
      `/cases/${encodeURIComponent(caseId)}/observations/${encodeURIComponent(observationId)}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * POST /cases/{cid}/observations/{obs_id}/promote —— 提升为线索。
   *
   * hypothesis **必填**：线索是待证明的命题，不指定假设就没有证伪目标，
   * 进处置流程只能空转。后端 400 拒绝空假设，前端同步禁用提交。
   */
  async promote(
    caseId: string,
    observationId: string,
    body: { hypothesis: string; note?: string; parent_clue_id?: string },
  ): Promise<{ task_id: string } & Record<string, unknown>> {
    const res = await api.post<{ task_id: string } & Record<string, unknown>>(
      `/cases/${encodeURIComponent(caseId)}/observations/${encodeURIComponent(observationId)}/promote`,
      body,
      { idempotencyAction: `obs-promote:${observationId}:${body.hypothesis}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST —— 认领/归档（不改变观察性质；提升必须走 promote） */
  async setDisposition(
    caseId: string,
    observationId: string,
    disposition: '已认领' | '已归档' | '未认领',
    note?: string,
  ): Promise<{ task_id: string } & Record<string, unknown>> {
    const res = await api.post<{ task_id: string } & Record<string, unknown>>(
      `/cases/${encodeURIComponent(caseId)}/observations/${encodeURIComponent(observationId)}/disposition`,
      { disposition, note: note ?? '' },
      { idempotencyAction: `obs-disp:${observationId}:${disposition}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET —— 提升时的假设下拉选项（本体声明） */
  async hypotheses(caseId: string): Promise<{ hypotheses: HypothesisChoice[] }> {
    const res = await api.get<{ hypotheses: HypothesisChoice[] }>(
      `/cases/${encodeURIComponent(caseId)}/observations/hypothesis-choices`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
