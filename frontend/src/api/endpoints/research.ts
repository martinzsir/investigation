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

export const researchApi = {
  async hypotheses(caseId: string): Promise<HypothesesDto> {
    const res = await api.get<HypothesesDto>(
      `/cases/${encodeURIComponent(caseId)}/hypotheses`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
