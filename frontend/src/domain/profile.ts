// 数据画像领域逻辑（FE-P-007）。
// 后端 GET /cases/{cid}/profiles 已实现（profiles_view.assemble_profiles），
// 返回六层报告结构（l1_l2 / l3 / l4 / l5）；前端适配层从此结构派生 UI 数据。
import type { Band } from './review'

/** 画像分色阶：≥90 绿 / 70–89 琥珀 / <70 红（进度条与指标卡共用） */
export function scoreBand(score: number): Band {
  if (score >= 90) return 'ok'
  if (score >= 70) return 'warn'
  return 'error'
}

/** 列级空值率色阶：>20% 红 / >5% 琥珀 / 否则正常 */
export function nullRateBand(rate: number): Band {
  if (rate > 0.2) return 'error'
  if (rate > 0.05) return 'warn'
  return 'ok'
}

/** 扣分项严重度 → 语义色档 */
export function severityBand(sev: 'high' | 'medium' | 'low' | 'block' | 'warn' | string): Band {
  if (sev === 'high' || sev === 'block') return 'error'
  if (sev === 'medium' || sev === 'warn') return 'warn'
  return 'ok'
}

// ---------- 后端原始响应 DTO（与 profiles_view.assemble_profiles 同构） ----------

/** 后端 l1_l2 单项（列层/值层画像） */
export interface ProfileL1L2Item {
  obj: string
  prop: string
  declared_type: string
  connectable: boolean
  materialized_object: boolean
  materialized_prop: boolean
  status: 'ok' | 'unmaterialized_object' | 'missing_column'
  value_profile?: {
    row_count: number
    non_null: number
    null_rate: number
    distinct: number
    samples: string[]
  }
  clean?: {
    object: string
    property: string
    dropped_rows: number
    rows_before: number
    rule: string | null
    sample_masked: string[]
  }
  compliance?: {
    rate: number
    element: string
    violations: number
    checked: number
  }
  // string connectable 属性扩展字段
  mixed?: boolean
  type_dist?: Record<string, number>
  landing_suggestions?: string[]
  needs_confirmation?: boolean
  variants?: { rule: number; alias: number }
  composite_suspect?: { count: number; samples: string[]; suggestion: string }
}

/** 后端 l5 质量分 */
export interface ProfileL5 {
  score: number
  score_range: [number, number]
  deductions: ProfileDeduction[]
  reviewable: boolean
  weights: Record<string, Record<string, number>>
  note: string
}

/** 后端 l3 指标项 */
export interface ProfileL3Item {
  obj: string
  prop: string
  metric?: string
  [key: string]: unknown
}

/** 后端完整画像响应 */
export interface ProfileResponse {
  available: boolean
  note?: string
  // available=true 时：
  derived?: boolean
  focus?: string[]
  anchor_date?: string | null
  pack?: string
  l0?: string
  l1_l2?: ProfileL1L2Item[]
  l3?: ProfileL3Item[]
  l4?: Record<string, unknown>
  l5?: ProfileL5
  compliance?: Record<string, unknown> | null
  params?: {
    window_days: number
    anchor_date: string | null
    focus_entities: string[]
  }
  health?: Record<string, unknown>
}

/** 扣分项（后端 l5.deductions 元素） */
export interface ProfileDeduction {
  scope: string
  ref: string
  code: string
  reason: string
  severity: 'block' | 'warn' | string
  points: number
}

// ---------- 前端适配层（从后端结构派生 UI 数据） ----------

/** 属性画像行（适配后） */
export interface ProfileColumn {
  object: string
  attribute: string
  value_type: string
  null_rate: number
  distinct_count: number
  mixed_type: boolean
  status: string
  score: number
  issues: string[]
}

/** 变体摘要（适配后；后端只返回计数） */
export interface ProfileVariantSummary {
  object: string
  prop: string
  rule_count: number
  alias_count: number
  total: number
}

/** 适配后的画像数据（供 UI 直接消费） */
export interface ProfileData {
  overall_score: number
  source_count: number
  issue_count: number
  aligned_entities: number
  columns: ProfileColumn[]
  variants: ProfileVariantSummary[]
  deductions: ProfileDeduction[]
  available: boolean
  note?: string
}

/** 适配函数：后端 ProfileResponse → 前端 ProfileData */
export function adaptProfile(res: ProfileResponse): ProfileData {
  if (!res.available) {
    return {
      available: false,
      note: res.note ?? '尚未接入数据源',
      overall_score: 0,
      source_count: 0,
      issue_count: 0,
      aligned_entities: 0,
      columns: [],
      variants: [],
      deductions: [],
    }
  }

  const l1l2 = res.l1_l2 ?? []
  const l5 = res.l5
  const deductions = l5?.deductions ?? []

  // 按 ref 分组扣分，用于派生每列 issues 和 score
  const deductionsByRef = new Map<string, ProfileDeduction[]>()
  for (const d of deductions) {
    if (d.scope === 'prop') {
      const arr = deductionsByRef.get(d.ref) ?? []
      arr.push(d)
      deductionsByRef.set(d.ref, arr)
    }
  }

  const columns: ProfileColumn[] = l1l2.map((item) => {
    const key = `${item.obj}.${item.prop}`
    const propDeductions = deductionsByRef.get(key) ?? []
    const pointsLost = propDeductions.reduce((sum, d) => sum + Math.abs(d.points), 0)
    return {
      object: item.obj,
      attribute: item.prop,
      value_type: item.declared_type,
      null_rate: item.value_profile?.null_rate ?? 0,
      distinct_count: item.value_profile?.distinct ?? 0,
      mixed_type: item.mixed ?? false,
      status: item.status,
      score: Math.max(0, 100 - pointsLost),
      issues: propDeductions.map((d) => d.code),
    }
  })

  const variants: ProfileVariantSummary[] = l1l2
    .filter((item) => item.variants && (item.variants.rule > 0 || item.variants.alias > 0))
    .map((item) => ({
      object: item.obj,
      prop: item.prop,
      rule_count: item.variants!.rule,
      alias_count: item.variants!.alias,
      total: item.variants!.rule + item.variants!.alias,
    }))

  return {
    available: true,
    overall_score: l5?.score ?? 0,
    source_count: new Set(l1l2.filter((i) => i.materialized_object).map((i) => i.obj)).size,
    issue_count: deductions.length,
    aligned_entities: res.focus?.length ?? 0,
    columns,
    variants,
    deductions,
    note: res.note,
  }
}
