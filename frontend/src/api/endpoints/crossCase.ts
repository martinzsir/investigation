import { api } from '../client'

// 跨案件查询（server/app/routers/cross_case.py + backend_cross.py 契约）。
// 红线：全有或全无鉴权（任一案件无权 → 整体 403，ATTACH 前拒绝）；
// SQL 首词白名单 SELECT/WITH/PRAGMA + READ_ONLY ATTACH 双保险；结果强制 LIMIT。
// ATTACH 别名：case_<case_id>（如 case_c1.obj_person）；来源案件列须 SQL 自带。

export interface CrossCaseQueryBody {
  case_ids: string[] // 2–50 且不重复
  sql: string
  reason: string // 1–500
  max_rows?: number // 1–10000，默认 1000
  timeout_ms?: number // 100–600000，默认 30000
}

export interface CrossCaseQueryResult {
  rows: Record<string, unknown>[]
  total: number
  /** 已授权案件列表（全有或全无通过后回显） */
  case_ids: string[]
}

export interface CrossCaseHistoryItem {
  id: number
  ts: string
  case_ids: string[]
  sql: string
  reason: string
  result_rows: number
}

export interface CrossCaseHistoryPage {
  items: CrossCaseHistoryItem[]
  total: number
  page: number
  page_size: number
}

export const crossCaseApi = {
  /** POST /cross-case/query 🔒 全有或全无；超时分层 cross（60s）；只读不携幂等键 */
  async query(body: CrossCaseQueryBody): Promise<CrossCaseQueryResult> {
    const { data } = await api.post<CrossCaseQueryResult>('/cross-case/query', body, {
      timeoutKind: 'cross',
    })
    return data
  },

  /** GET /cross-case/history —— 仅本 operator 记录，服务端分页（协议同 tasks，C1） */
  async history(page = 1, pageSize = 50): Promise<CrossCaseHistoryPage> {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    })
    const { data } = await api.get<CrossCaseHistoryPage>(
      `/cross-case/history?${params.toString()}`,
    )
    return data
  },
}
