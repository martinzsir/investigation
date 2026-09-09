// 数据画像领域逻辑（FE-P-007；后端 /profiles 待补，当前走 MSW mock，非红线可 mock）。
// 画像分/空值率色阶为纯函数；页面空态「尚未接入数据源」。
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
export function severityBand(sev: 'high' | 'medium' | 'low' | string): Band {
  if (sev === 'high') return 'error'
  if (sev === 'medium') return 'warn'
  return 'ok'
}

// ---------- 画像 DTO（与 mock 信封同构；后端补端点后对齐） ----------

export interface ProfileColumn {
  object: string
  attribute: string
  value_type: string
  null_rate: number
  distinct_count: number
  mixed_type?: boolean
  score: number
  issues: string[]
}

export interface ProfileVariant {
  canonical: string
  group: string
  variants: string[]
  distribution: Array<{ value: string; count: number }>
}

export interface ProfileDeduction {
  scope: string
  ref: string
  code: string
  reason: string
  severity: 'high' | 'medium' | 'low'
}

export interface ProfileData {
  overall_score: number
  source_count: number
  issue_count: number
  aligned_entities: number
  columns: ProfileColumn[]
  variants: ProfileVariant[]
  deductions: ProfileDeduction[]
}
