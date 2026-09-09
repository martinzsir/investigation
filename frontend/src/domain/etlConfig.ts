// ETL 管道配置领域逻辑（FE-P-007/008）：枚举校验、映射预检冲突归类、出路指引。
// 纯函数、确定性可测。
// 红线四：复合列/映射冲突只有诊断 + 出路（A 拆 source_sql / B 整列降级），
// 无"忽略继续"按钮、无写、不自动改配置。
import { CAST_ERROR_POLICIES, NULL_POLICIES } from '../api/endpoints/etl'
import type { ValidateConflict, ValidateConflictType } from '../api/endpoints/etl'

export const CAST_ERROR_LABEL: Record<string, string> = {
  fail: '整案 FAIL FAST（不落盘）',
  quarantine: '隔离问题行、余者入库',
}

/** 缺省（未声明）= 降级 NULL（静默降级留痕，不阻断） */
export const CAST_ERROR_DEFAULT_LABEL = '降级 NULL（留诊断）'

export const NULL_POLICY_LABEL: Record<string, string> = {
  allow: '允许空值',
  reject: '整案拒绝',
  quarantine: '隔离空值行',
}

export function isValidCastErrorPolicy(v: string): boolean {
  return (CAST_ERROR_POLICIES as readonly string[]).includes(v)
}

export function isValidNullPolicy(v: string): boolean {
  return (NULL_POLICIES as readonly string[]).includes(v)
}

export const CONFLICT_LABEL: Record<ValidateConflictType, string> = {
  one_to_one: '一对多映射（同一源列映射到多个属性）',
  unknown_prop: '目标属性未在对象上声明',
  missing_column: '源表缺列',
}

/** 出路指引（与后端 paths key 同构；冲突永远只给这两条路） */
export const PATH_GUIDE = {
  A_split_source_sql: '在 source_sql 中把复合表达式拆成多个独立源列（各自命名、各自映射）',
  B_degrade_column: '该列整列降级 NULL 并持续诊断（数据不进模型，仅留痕）',
} as const

export interface ConflictGroup {
  type: ValidateConflictType
  label: string
  items: ValidateConflict[]
}

/** 冲突按类型分组（表格分区渲染；类型顺序固定） */
export function groupConflicts(conflicts: ValidateConflict[]): ConflictGroup[] {
  const order: ValidateConflictType[] = ['one_to_one', 'unknown_prop', 'missing_column']
  return order
    .map((type) => ({
      type,
      label: CONFLICT_LABEL[type],
      items: conflicts.filter((c) => c.type === type),
    }))
    .filter((g) => g.items.length > 0)
}

/** 冲突统计文案 */
export function conflictSummary(conflicts: ValidateConflict[]): string {
  const g = groupConflicts(conflicts)
  if (g.length === 0) return '无冲突'
  return g.map((x) => `${x.label} ${x.items.length} 处`).join('；')
}

/** 复合列诊断行（composite_props 恒为后端诊断结果，前端只读） */
export interface CompositeDiagnostic {
  prop: string
  source_sql?: string
  [k: string]: unknown
}

export function compositeList(source: { composite_props?: unknown[] } | null | undefined): CompositeDiagnostic[] {
  const arr = source?.composite_props
  return Array.isArray(arr) ? (arr as CompositeDiagnostic[]) : []
}
