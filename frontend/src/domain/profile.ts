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
  status?: string
  value?: number
  reason?: string
  [key: string]: unknown
}

/** 后端 l4 五间分布（reverse 元素） */
export interface ProfileJian {
  jian: string
  objects: string[]
  links: string[]
  declared: boolean
  has_materialized: boolean
}

/** 后端 l4 完整结构 */
export interface ProfileL4 {
  forward?: Record<string, { objects: string[]; links: string[] }>
  reverse?: ProfileJian[]
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
  l4?: ProfileL4
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

/** 属性画像行（适配后；含 L1/L2 列层/值层全字段） */
export interface ProfileColumn {
  object: string
  attribute: string
  value_type: string
  connectable: boolean
  status: string
  // 值层（value_profile）
  row_count: number
  non_null: number
  null_rate: number
  distinct_count: number
  samples: string[]
  // 清洗（clean）
  dropped_rows: number | null
  clean_rule: string | null
  // 合规（compliance）
  compliance_rate: number | null
  compliance_element: string | null
  // string 类型分析
  mixed_type: boolean
  landing: string[]
  needs_confirmation: boolean
  composite_suspect: number
  variants_rule: number
  variants_alias: number
  // 评分
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

/** L3 指标（适配后） */
export interface ProfileMetric {
  object: string
  prop: string
  metric: string
  status: string
  value: number | null
  reason: string
}

/** 适配后的画像数据（供 UI 直接消费） */
export interface ProfileData {
  available: boolean
  note?: string
  overall_score: number
  score_range: [number, number]
  reviewable: boolean
  source_count: number
  issue_count: number
  aligned_entities: number
  anchor_date: string | null
  window_days: number
  columns: ProfileColumn[]
  variants: ProfileVariantSummary[]
  metrics: ProfileMetric[]
  jians: ProfileJian[]
  deductions: ProfileDeduction[]
}

/** 属性行是否有问题（与画像表"问题"列口径一致：未物化/缺列/混装/复合/待确认/变体/扣分项） */
export function columnHasIssue(c: ProfileColumn): boolean {
  return c.status !== 'ok'
    || c.mixed_type
    || c.composite_suspect > 0
    || c.needs_confirmation
    || c.variants_rule + c.variants_alias > 0
    || c.issues.length > 0
}

/** 对象分组（属性画像表按对象折叠） */
export interface ProfileObjectGroup {
  object: string
  columns: ProfileColumn[]
  /** 已物化（status=ok）属性数 */
  live_count: number
  /** 问题属性数（columnHasIssue 口径） */
  issue_count: number
}

/**
 * 按对象分组属性行：保持对象首次出现顺序与组内属性顺序（后端 l1_l2 已按
 * 对象聚簇输出，此处不重排）。供属性画像表"同对象折叠、点开展开"。
 */
export function groupColumnsByObject(columns: ProfileColumn[]): ProfileObjectGroup[] {
  const order: string[] = []
  const map = new Map<string, ProfileColumn[]>()
  for (const c of columns) {
    let arr = map.get(c.object)
    if (!arr) {
      arr = []
      map.set(c.object, arr)
      order.push(c.object)
    }
    arr.push(c)
  }
  return order.map((object) => {
    const cols = map.get(object)!
    return {
      object,
      columns: cols,
      live_count: cols.filter((c) => c.status === 'ok').length,
      issue_count: cols.filter(columnHasIssue).length,
    }
  })
}

const METRIC_LABELS: Record<string, string> = {
  focus_hit_rate: '关注实体命中率',
  known_overlap_count: '与已知实体重合数',
  window_coverage: '时间窗口覆盖率',
  wan_integer_rate: '万元整数交易率',
}

export function metricLabel(metric: string): string {
  return METRIC_LABELS[metric] ?? metric
}

/** 适配函数：后端 ProfileResponse → 前端 ProfileData */
export function adaptProfile(res: ProfileResponse): ProfileData {
  if (!res.available) {
    return {
      available: false,
      note: res.note ?? '尚未接入数据源',
      overall_score: 0,
      score_range: [0, 0],
      reviewable: false,
      source_count: 0,
      issue_count: 0,
      aligned_entities: 0,
      anchor_date: null,
      window_days: 0,
      columns: [],
      variants: [],
      metrics: [],
      jians: [],
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
    const pointsLost = propDeductions.reduce((sum, d) => sum + Math.abs(d.points || 0), 0)
    const vp = item.value_profile
    return {
      object: item.obj,
      attribute: item.prop,
      value_type: item.declared_type,
      connectable: item.connectable,
      status: item.status,
      row_count: vp?.row_count ?? 0,
      non_null: vp?.non_null ?? 0,
      null_rate: vp?.null_rate ?? 0,
      distinct_count: vp?.distinct ?? 0,
      samples: vp?.samples ?? [],
      dropped_rows: item.clean?.dropped_rows ?? null,
      clean_rule: item.clean?.rule ?? null,
      compliance_rate: item.compliance?.rate ?? null,
      compliance_element: item.compliance?.element ?? null,
      mixed_type: item.mixed ?? false,
      landing: item.landing_suggestions ?? [],
      needs_confirmation: item.needs_confirmation ?? false,
      composite_suspect: item.composite_suspect?.count ?? 0,
      variants_rule: item.variants?.rule ?? 0,
      variants_alias: item.variants?.alias ?? 0,
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

  const metrics: ProfileMetric[] = (res.l3 ?? []).map((m) => ({
    object: m.obj,
    prop: m.prop,
    metric: m.metric ?? '',
    status: m.status ?? 'ok',
    value: typeof m.value === 'number' ? m.value : null,
    reason: (m.reason as string) ?? '',
  }))

  const jians: ProfileJian[] = res.l4?.reverse ?? []

  return {
    available: true,
    note: res.note ?? l5?.note,
    overall_score: l5?.score ?? 0,
    score_range: l5?.score_range ?? [l5?.score ?? 0, l5?.score ?? 0],
    reviewable: l5?.reviewable ?? false,
    source_count: new Set(l1l2.filter((i) => i.materialized_object).map((i) => i.obj)).size,
    issue_count: deductions.length,
    aligned_entities: res.focus?.length ?? 0,
    anchor_date: res.anchor_date ?? res.params?.anchor_date ?? null,
    window_days: res.params?.window_days ?? 0,
    columns,
    variants,
    metrics,
    jians,
    deductions,
  }
}
