// 质量检查报告领域逻辑（FE-P-009）：severity 分级、色阶、阻断判定、启发式封顶。
// 纯函数、确定性可测。
// 红线六：heuristic 结果封顶 suggest（黄建议），永不出 block/红阻断；
// 红线七：deterministic 违规 = block（红），必须处置而非仅提示。
import type { QualityCategory, QualityCheck, QualityReport, QualitySeverity } from '../api/endpoints/quality'

export const SEVERITY_ORDER: QualitySeverity[] = ['block', 'warn', 'suggest', 'ok']

export const SEVERITY_LABEL: Record<QualitySeverity, string> = {
  block: '违规',
  warn: '警告',
  suggest: '建议',
  ok: '通过',
}

/** 色阶（与既有报告色阶约定一致：红=block / 橙=warn / 黄=suggest / 绿=ok） */
export const SEVERITY_COLOR: Record<QualitySeverity, string> = {
  block: '#d93026',
  warn: '#e8830c',
  suggest: '#c9a227',
  ok: '#2e7d32',
}

export const CATEGORY_LABEL: Record<QualityCategory, string> = {
  compliance: '合规一致性',
  freshness: '数据时效性',
  sensitive: '敏感面暴露',
  unit: '单位一致性',
}

/** 按 severity 分组（保持报告内顺序） */
export function groupBySeverity(checks: QualityCheck[]): Record<QualitySeverity, QualityCheck[]> {
  const g: Record<QualitySeverity, QualityCheck[]> = { block: [], warn: [], suggest: [], ok: [] }
  for (const c of checks) g[c.severity].push(c)
  return g
}

/** 是否存在确定性阻断（红线七：block 必须处置） */
export function hasBlocking(report: Extract<QualityReport, { available: true }>): boolean {
  return report.checks.some((c) => c.severity === 'block')
}

/** 启发式越界自检（红线六：heuristic 不得出现 block/warn；返回越界项） */
export function heuristicCeilingViolations(checks: QualityCheck[]): QualityCheck[] {
  return checks.filter((c) => c.mode === 'heuristic' && (c.severity === 'block' || c.severity === 'warn'))
}

export type HealthLevel = 'pass' | 'warn' | 'block' | 'empty'

/** 总健康度：block>0 → block；warn>0 → warn；否则 pass；无报告 → empty */
export function healthLevel(report: QualityReport | null): HealthLevel {
  if (!report || !report.available) return 'empty'
  const s = report.summary
  if (s.violations > 0) return 'block'
  if (s.warnings > 0) return 'warn'
  return 'pass'
}

export const HEALTH_LABEL: Record<HealthLevel, string> = {
  pass: '全部通过',
  warn: '存在警告',
  block: '存在违规',
  empty: '尚未检查',
}
