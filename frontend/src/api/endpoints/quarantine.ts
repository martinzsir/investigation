import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 隔离区 + 清洗留痕（server/app/routers/quality.py 契约）。
// GET /cases/{cid}/quarantine?reason=&page=&page_size=      隔离行（服务端分页，page_size≤50）
// GET /cases/{cid}/clean-trace?object=&page=&page_size=     清洗前后对比（按对象/属性聚合）

export type QuarantineReason = 'cast_error' | 'null_value' | 'dedup' | 'other'

export const QUARANTINE_REASONS: Array<{ value: QuarantineReason; label: string }> = [
  { value: 'cast_error', label: '类型转换失败' },
  { value: 'null_value', label: '空值拒绝' },
  { value: 'dedup', label: '去重丢弃' },
  { value: 'other', label: '其他' },
]

export interface QuarantineItem {
  object: string
  property: string
  rule: string
  src_column: string
  reason: QuarantineReason | string
  source_table: string
  samples_masked: string[]
  name_value: string
  quarantined_at: string
}

export interface QuarantinePage {
  items: QuarantineItem[]
  total: number
  /** 四类原因计数（不受 reason 过滤影响） */
  stats: Partial<Record<QuarantineReason, number>> & Record<string, number>
  page: number
  page_size: number
  /** 零隔离时的显式文案（红线五："本次装载无数据被丢弃"） */
  empty_message?: string
}

export interface CleanTraceItem {
  object: string
  property: string
  rules: string[]
  rows_before: number
  rows_after: number
  dropped_rows: number
  rate: number
  samples_masked: string[]
  source: 'build' | 'rescan' | string
  created_at: string
}

export interface CleanTracePage {
  items: CleanTraceItem[]
  total: number
  page: number
  page_size: number
}

export interface PageQuery {
  page?: number
  pageSize?: number
}

export const quarantineApi = {
  /** GET /cases/{cid}/quarantine */
  async list(caseId: string, query: PageQuery & { reason?: string } = {}): Promise<QuarantinePage> {
    const params = new URLSearchParams()
    if (query.reason) params.set('reason', query.reason)
    if (query.page) params.set('page', String(query.page))
    if (query.pageSize) params.set('page_size', String(query.pageSize))
    const qs = params.toString()
    const res = await api.get<QuarantinePage>(
      `/cases/${encodeURIComponent(caseId)}/quarantine${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/clean-trace */
  async cleanTrace(caseId: string, query: PageQuery & { object?: string } = {}): Promise<CleanTracePage> {
    const params = new URLSearchParams()
    if (query.object) params.set('object', query.object)
    if (query.page) params.set('page', String(query.page))
    if (query.pageSize) params.set('page_size', String(query.pageSize))
    const qs = params.toString()
    const res = await api.get<CleanTracePage>(
      `/cases/${encodeURIComponent(caseId)}/clean-trace${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
