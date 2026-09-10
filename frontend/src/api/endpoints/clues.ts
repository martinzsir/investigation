import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { ClueStatus, ClueAction, EvidenceItem } from '../../domain/clue'

// W-019/020 线索与处置（server/app/routers/clues.py 契约）。

/** 溯源行（FE-C-012）：行 URI + 原始字段全表 + 命中字段 */
export interface SourceRowDto {
  row_uri: string
  source?: string
  occurred_at?: string
  fields: Array<{
    name: string
    value: string
    hit?: boolean
    /** 遮蔽字段类型（手机/身份证） */
    mask?: 'phone' | 'idcard' | 'text'
    /** 字段可见策略：visible 明文 / masked 遮蔽 / denied 无权限渲染 ****（FE-C-010/015，红线⑥） */
    policy?: 'visible' | 'masked' | 'denied'
    denied_hint?: string
  }>
}

/** R13：单维度计分分解（原始值/权重/贡献分） */
export interface ScoreBasisTerm {
  raw: number
  weight: number
  contrib: number
}
/** R13：各维度 → 分解（confidence/jian_coverage/data_strength） */
export type ScoreBasis = Record<string, ScoreBasisTerm>

export interface ClueListItem {
  clue_id: string
  title: string
  /** 依据摘要副标题（方案二·B：规则名 + 依据摘要，零后端改动） */
  basis?: string
  skill_id?: string
  jian_types?: string[]
  assumption_chain?: unknown[]
  /** 级别/交叉等级 */
  level?: string
  /** 数据通道（资金/通讯/行为/关系/时间） */
  dimension?: string[]
  priority_rank?: number
  priority_score?: number
  /** R13：计分可解释三件套（旧产物可能缺省） */
  score_basis?: ScoreBasis | null
  score_formula?: string | null
  score_source?: string | null
  source_row_count?: number
  merged_from?: string[]
  status: ClueStatus
  note?: string
  operator?: string
  updated_at?: string
  status_source?: 'state' | 'artifact'
}

export interface ClueListPage {
  items: ClueListItem[]
  total: number
  page: number
  page_size: number
  artifact_version?: number | null
  available: boolean
  note?: string
  access_note?: string
}

export interface ClueDetail extends ClueListItem {
  source_rows: SourceRowDto[]
  /** B3：三栏证据（后端 evidence_builder 产出） */
  evidence?: EvidenceItem[]
  audit_log?: unknown[]
  detail?: Record<string, unknown>
  suppressed_log?: unknown[]
  decisions?: unknown[]
}

export interface ClueActionResult {
  task_id: string
  status: string
  task_type?: string
  [key: string]: unknown
}

export interface ClueListParams {
  status?: string
  level?: string
  dimension?: string
  jian?: string
  subject?: string
  page?: number
  page_size?: number
}

export const cluesApi = {
  /** GET /cases/{cid}/clues —— 只读已产出结果，不触发重扫（W-019 AC-6） */
  async list(caseId: string, params: ClueListParams = {}): Promise<ClueListPage> {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== '' && v !== null) q.set(k, String(v))
    }
    const qs = q.toString()
    const res = await api.get<ClueListPage>(
      `/cases/${encodeURIComponent(caseId)}/clues${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/clues/suppressed —— 被抑制记录（聚合 detail.suppressed_log，W-019） */
  async suppressed(caseId: string): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
    const res = await api.get<{ items: Array<Record<string, unknown>>; total: number }>(
      `/cases/${encodeURIComponent(caseId)}/clues/suppressed`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/clues/{clueId} */
  async detail(caseId: string, clueId: string): Promise<ClueDetail> {
    const res = await api.get<ClueDetail>(
      `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * POST /cases/{cid}/clues/{clueId}/actions 🔒（202 入队 DISPOSE）。
   * 写操作统一携带幂等键（FE-I-007）；operator 由会话快照，不取请求体。
   */
  async action(
    caseId: string,
    clueId: string,
    body: { action: ClueAction; note?: string; reason?: string; legal_basis?: string },
  ): Promise<ClueActionResult> {
    const res = await api.post<ClueActionResult>(
      `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}/actions`,
      body,
      { idempotencyAction: `clue-action:${clueId}:${body.action}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
