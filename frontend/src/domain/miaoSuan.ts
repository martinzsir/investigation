// 庙算工作台（FE-P-011）纯函数：双轨覆盖 diff、热力色阶、候补池隔离。
// 红线二（FE-T-014）：候补池 candidates 是派生只读视图，注入前后正式线索的
// 交叉等级快照必须逐键不变——applyCandidates 结构上不回写等级表。
import type {
  CoverageCard,
  HypothesisCandidate,
  HypothesesDto,
  RestrictedClue,
} from '../api/endpoints/research'

export interface TrackGap {
  track: 'declared' | 'empirical'
  missing: string[]
  severity: string | null
  reason: string | null
}

/** 双轨缺口合并：声明轨 vs 实证轨各自 missing 维度 */
export function dualTrackGaps(coverage: HypothesesDto['coverage']): {
  declared: TrackGap
  empirical: TrackGap
} {
  const toGap = (track: 'declared' | 'empirical', cards: CoverageCard[]): TrackGap => ({
    track,
    missing: cards.flatMap((c) => c.missing ?? []),
    severity: cards.find((c) => c.severity)?.severity ?? null,
    reason: cards.find((c) => c.reason)?.reason ?? null,
  })
  return {
    declared: toGap('declared', coverage.declared ?? []),
    empirical: toGap('empirical', coverage.empirical ?? []),
  }
}

/** 双轨缺口数（声明/实证），用于顶部对比条 */
export function gapCounts(coverage: HypothesesDto['coverage']): { declared: number; empirical: number } {
  const g = dualTrackGaps(coverage)
  return { declared: g.declared.missing.length, empirical: g.empirical.missing.length }
}

/**
 * 热力色阶：count → 0..4 五档（accent 单色渐变，零依赖，D2 口径）。
 * 返回 css 级别名（heat-0..heat-4），组件按 tokens 着色。
 */
export function heatLevel(count: number, max: number): number {
  if (!Number.isFinite(count) || count <= 0 || max <= 0) return 0
  const ratio = count / max
  if (ratio >= 0.75) return 4
  if (ratio >= 0.5) return 3
  if (ratio >= 0.25) return 2
  return 1
}

/** 热力矩阵最大值（色阶归一用） */
export function heatMax(counts: number[][]): number {
  return counts.reduce((m, row) => Math.max(m, ...row), 0)
}

export interface LevelSnapshot {
  [clueId: string]: string | null
}

/**
 * FE-T-014 红线二：候补池注入。
 * 正式线索等级快照按原值冻结返回；candidates 只进独立候补区，
 * 结构上不允许回写/升格等级表（候补 candidate 类型本身无 cross_level 字段）。
 */
export function applyCandidates(
  levels: LevelSnapshot,
  candidates: HypothesisCandidate[],
): { levels: LevelSnapshot; candidates: HypothesisCandidate[] } {
  // 浅拷贝冻结：等级表只读出、不写回
  const frozen: LevelSnapshot = { ...levels }
  return { levels: frozen, candidates: [...candidates] }
}

/** 断言用：candidate 不得携带任何升格/改级字段（后端派生口径保证，前端红线复测） */
export function candidateHasNoLevelMutation(c: HypothesisCandidate): boolean {
  const forbidden = ['cross_level', 'new_level', 'level_up', 'promoted', 'upgrade']
  const keys = Object.keys(c as unknown as Record<string, unknown>)
  return !forbidden.some((k) => keys.includes(k))
}

/** restricted 灰显条目（内间线索无权：只露 clue_id + 原因） */
export function restrictedList(restricted: RestrictedClue[] | undefined): RestrictedClue[] {
  return restricted ?? []
}

/** 候补区与正式区视觉隔离标记（虚线隔离 class 断言用） */
export const CANDIDATE_ZONE_CLASS = 'candidate-zone'
