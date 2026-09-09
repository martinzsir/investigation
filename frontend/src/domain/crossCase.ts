// 跨案件查询（FE-P-015）纯函数：全有或全无闸门、SQL 模板、参数钳制。
// 红线：前端以 GET /cases（租户过滤后）为授权全集做 N/M 预判；
// 后端 403 为最终防线。前端代码自身不拼业务 SQL——模板只产出跨案
// UNION ALL 骨架并注入 source_case 字面量列，自由 SQL 由用户书写。

/** case_id 合法字符（ATTACH 别名 case_<id> 拼接安全前提） */
export function isSafeCaseId(id: string): boolean {
  return /^[A-Za-z0-9_-]+$/.test(id)

}

export interface AuthzMatrix {
  /** 选择的案件中有权（在本租户案件列表内）的 */
  authorized: string[]
  /** 选择的案件中无权的（denied chips 可一键移除） */
  denied: string[]
  /** 有权数 M */
  m: number
  /** 选择总数 N */
  n: number
}

/** N/M 授权计算：selected ∩ ownedIds */
export function authorizeSelection(selected: string[], ownedIds: string[]): AuthzMatrix {
  const owned = new Set(ownedIds)
  const seen = new Set<string>()
  const authorized: string[] = []
  const denied: string[] = []
  for (const id of selected) {
    if (seen.has(id)) continue
    seen.add(id)
    if (owned.has(id)) authorized.push(id)
    else denied.push(id)
  }
  return { authorized, denied, m: authorized.length, n: selected.length }
}

/**
 * 全有或全无闸门：可执行 ⇔ 无无权案件 且 去重后案件数 ≥ 2（后端 min_length=2）。
 */
export function canExecute(authz: AuthzMatrix): boolean {
  return authz.denied.length === 0 && authz.authorized.length >= 2
}

/** 执行按钮禁用原因文案（'' = 可执行） */
export function executeBlockReason(authz: AuthzMatrix): string {
  if (authz.denied.length > 0) return `存在 ${authz.denied.length} 个无权案件，请移除后重试`
  if (authz.authorized.length < 2) return '至少选择 2 个案件'
  return ''
}

/** case_ids 入参校验：2–50 且不重复 */
export function caseIdsError(ids: string[]): string {
  const uniq = new Set(ids)
  if (ids.length < 2) return '至少选择 2 个案件'
  if (ids.length > 50) return '最多 50 个案件'
  if (uniq.size !== ids.length) return '案件不得重复'
  if (ids.some((i) => !isSafeCaseId(i))) return '案件编号含非法字符（仅字母数字_-）'
  return ''
}

/** reason 校验：非空且 ≤500 */
export function reasonError(reason: string): string {
  const v = reason.trim()
  if (!v) return '查询事由必填（审计留痕）'
  if (v.length > 500) return '事由最长 500 字'
  return ''
}

export const MAX_ROWS = { def: 1000, min: 1, max: 10000 }
export const TIMEOUT_MS = { def: 30000, min: 100, max: 600000 }

export function clampMaxRows(n: number): number {
  if (!Number.isFinite(n)) return MAX_ROWS.def
  return Math.min(MAX_ROWS.max, Math.max(MAX_ROWS.min, Math.floor(n)))
}

export function clampTimeoutMs(n: number): number {
  if (!Number.isFinite(n)) return TIMEOUT_MS.def
  return Math.min(TIMEOUT_MS.max, Math.max(TIMEOUT_MS.min, Math.floor(n)))
}

/**
 * 跨案同对象查询模板：各案件库挂为 case_<id>，UNION ALL 并注入
 * source_case 字面量列（结果每行标来源案件；自由 SQL 需自行 select 来源列）。
 * table 须为语义表名（obj_* / lnk_*），仅允许标识符字符。
 */
export function buildUnionTemplate(caseIds: string[], table: string): string {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(table)) {
    throw new Error(`非法表名：${table}`)
  }
  const safe = caseIds.filter(isSafeCaseId)
  return safe
    .map((cid) => `SELECT '${cid}' AS source_case, t.* FROM case_${cid}.${table} t`)
    .join('\nUNION ALL\n')
}

/** 结果表是否含 source_case 列（无则不伪造，前端提示自由 SQL 需自带来源列） */
export function hasSourceCaseColumn(rows: Record<string, unknown>[]): boolean {
  return rows.length > 0 && Object.prototype.hasOwnProperty.call(rows[0], 'source_case')
}

/** 模板选项（语义层常用表） */
export const TEMPLATE_TABLES: { value: string; label: string }[] = [
  { value: 'obj_person', label: '人员对象 obj_person' },
  { value: 'obj_organization', label: '单位对象 obj_organization' },
  { value: 'obj_transaction', label: '交易事件 obj_transaction' },
  { value: 'obj_call', label: '通话事件 obj_call' },
  { value: 'lnk_transfers', label: '资金关系 lnk_transfers' },
]
