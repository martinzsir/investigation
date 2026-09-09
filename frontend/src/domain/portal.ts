// 案件门户（FE-P-023b）纯函数：客户端筛选/三态判定/建案表单校验/待办聚合。
// B1 口径：GET /cases 无查询参数，status 筛选 + q 搜索全部前端客户端做（数十量级）。
import type { CaseDto, CaseSummaryDto } from '../api/endpoints/cases'

export const CASE_STATUS = {
  draft: '待建案',
  active: '侦查中',
  closed: '已结案',
  archived: '已封存',
} as const

/** 状态筛选 tab：all + 状态机四态 */
export type StatusFilter = 'all' | string

export interface PortalFilters {
  status: StatusFilter
  q: string
}

/** 归档门槛：clearance≥2（require_analyst；偏将及以上） */
export function canArchive(clearance: number): boolean {
  return clearance >= 2
}

/** 客户端筛选：status 精确匹配 + q 匹配 id/name（大小写不敏感） */
export function filterCases(cases: CaseDto[], filters: PortalFilters): CaseDto[] {
  const q = filters.q.trim().toLowerCase()
  return cases.filter((c) => {
    if (filters.status !== 'all' && c.status !== filters.status) return false
    if (q && !(c.id.toLowerCase().includes(q) || c.name.toLowerCase().includes(q))) {
      return false
    }
    return true
  })
}

/** 门户三态：empty（无案件，引导新建）/ no-result（有案件但筛选为空）/ data */
export type PortalViewState = 'empty' | 'no-result' | 'data'

export function portalViewState(
  allCases: CaseDto[],
  filtered: CaseDto[],
  loading: boolean,
): PortalViewState {
  if (loading) return allCases.length === 0 ? 'empty' : 'data'
  if (allCases.length === 0) return 'empty'
  return filtered.length === 0 ? 'no-result' : 'data'
}

/** 建案 case_id 校验：1–64，字母/数字/下划线/中划线（'' = 合法） */
export function caseIdError(id: string): string {
  const v = id.trim()
  if (!v) return '案件编号不能为空'
  if (v.length > 64) return '案件编号最长 64 字符'
  if (!/^[A-Za-z0-9_-]+$/.test(v)) return '仅支持字母、数字、下划线、中划线'
  return ''
}

/** 建案 name 校验：1–128 非空 */
export function caseNameError(name: string): string {
  const v = name.trim()
  if (!v) return '案件名称不能为空'
  if (v.length > 128) return '案件名称最长 128 字符'
  return ''
}

export interface PortalTodos {
  clues: number
  review: number
  anomalies: number
}

/** 待办三计数聚合（Promise.all 并发 summary，A3 口径） */
export function sumTodos(summaries: CaseSummaryDto[]): PortalTodos {
  return summaries.reduce<PortalTodos>(
    (acc, s) => ({
      clues: acc.clues + (s.todos?.clues_pending ?? 0),
      review: acc.review + (s.todos?.review_pending ?? 0),
      anomalies: acc.anomalies + (s.todos?.anomalies_pending ?? 0),
    }),
    { clues: 0, review: 0, anomalies: 0 },
  )
}

/** 卡片健康点：chain 异常或 degraded → 告警 */
export function healthTone(summary: CaseSummaryDto | undefined): 'ok' | 'warn' | 'none' {
  if (!summary) return 'none'
  const { chain_ok, degraded } = summary.health ?? { chain_ok: true, degraded: false }
  if (!chain_ok || degraded) return 'warn'
  return 'ok'
}
