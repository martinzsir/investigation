import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 庙算工作台（server/app/routers/research.py + hypotheses_view.py 契约）。
// GET /cases/{cid}/hypotheses —— 派生口径（derived:true），候补池不含升格字段、
// 不改变交叉等级（红线二）；内间线索对无权角色只给 {clue_id, reason}。

export interface CoverageCard {
  /** 缺口维度名（无缺口为 null） */
  dimension: string | null
  covered: number
  total: number
  missing: string[]
  reason: string | null
  severity: string | null
  created_at: string | null
}

export interface HypothesisCandidate {
  clue_id: string
  title: string
  jian_types: string[]
  level: string | null
  priority_score: number | null
  /** R13：计分可解释三件套（旧产物可能缺省） */
  score_basis?: import('./clues').ScoreBasis | null
  score_formula?: string | null
  score_source?: string | null
  reason: string
}

export interface RestrictedClue {
  clue_id: string
  reason: string
}

export interface HypothesesDto {
  available: boolean
  derived: true
  /** 双轨覆盖：声明（miaosuan:dimension）vs 实证（miaosuan:dimension:empirical） */
  coverage: {
    declared: CoverageCard[]
    empirical: CoverageCard[]
  }
  /** 五间 × 三级热力矩阵：counts[level][jian] */
  heatmap: {
    jians: string[]
    levels: string[]
    counts: number[][]
  }
  /** 候补池：仅待查线索 top20，无升格字段（FE-T-014 隔离） */
  candidates: HypothesisCandidate[]
  /** 无权查看的内间线索（不泄露内容） */
  restricted: RestrictedClue[]
}

/** 人工假设条目（P1：只持久化人工部分，自动假设保持派生） */
export interface ManualHypothesis {
  id: string
  description: string
  falsification: string
  evidence_needed: string[]
  data_sources: string[]
  procedure: string
  dimension: string[]
  jian_types: string[]
  source: string
  status: string
  created_by?: string
  created_at?: string
  updated_by?: string
  updated_at?: string
}

export interface ManualHypothesesDto {
  items: ManualHypothesis[]
  order: string[]
  audit: { action: string; detail: string; operator: string; ts: string }[]
}

export interface ManualHypothesisIn {
  description: string
  falsification: string
  evidence_needed?: string[]
  data_sources?: string[]
  procedure?: string
  dimension?: string[]
  jian_types?: string[]
}

/** 采样预演单项结果（P3） */
export interface PreflightResult {
  hypothesis_id: string
  sample_ratio: number
  sampled_rows: number
  hit_rows: number
  hit_rate: number
  /** 方向明确 / 方向存疑 / 方向否定 */
  verdict: string
  suggest: string
}

export interface SamplingDto {
  advisory_only: boolean
  note: string
  results: PreflightResult[]
  overall_verdict: string
  suggest: string
}

/** 判定 → 色调语义（方向否定要醒目，防止盲投全量） */
export function verdictTone(v: string): 'success' | 'warning' | 'error' {
  if (v.includes('否定')) return 'error'
  if (v.includes('存疑')) return 'warning'
  return 'success'
}

export const researchApi = {
  async hypotheses(caseId: string): Promise<HypothesesDto> {
    const res = await api.get<HypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ---- 人工假设 CRUD（P1）----
  async manualList(caseId: string): Promise<ManualHypothesesDto> {
    const res = await api.get<ManualHypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses/manual`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async manualAdd(caseId: string, body: ManualHypothesisIn):
    Promise<ManualHypothesesDto> {
    const res = await api.post<ManualHypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses/manual`, body)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async manualUpdate(caseId: string, hid: string, body: ManualHypothesisIn):
    Promise<ManualHypothesesDto> {
    const res = await api.put<ManualHypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses/manual/${hid}`, body)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async manualRemove(caseId: string, hid: string):
    Promise<ManualHypothesesDto> {
    const res = await api.delete<ManualHypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses/manual/${hid}`)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async manualReorder(caseId: string, order: string[]):
    Promise<ManualHypothesesDto> {
    const res = await api.post<ManualHypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses/manual/reorder`,
      { order })
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ---- 采样预演（P3）----
  async samplingPreflight(caseId: string, hypothesisIds: string[],
    sampleRatio = 0.01): Promise<SamplingDto> {
    const res = await api.post<SamplingDto>(
      `/cases/${encodeURIComponent(caseId)}/sampling/preflight`,
      { hypothesis_ids: hypothesisIds, sample_ratio: sampleRatio })
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
