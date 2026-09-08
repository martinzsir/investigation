import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { ClueStatus } from '../../domain/clue'

// W-023/024 审计链时间线 / 完整性自检（server/app/routers/audit.py 契约）。
// 审计链只读、不可改不可删；身份取会话主体，operator 参数仅作筛选。

export interface AuditItem {
  seq: number
  event_id: string
  case_id: string
  occurred_at: string
  /** 真实姓名（会话主体快照，非账号 ID） */
  operator: string
  /** 派生标签：disposal/proposal/parameter_set/generic */
  action: string
  status_from?: ClueStatus | null
  status_to?: ClueStatus | null
  legal_basis?: string | null
  note?: string | null
  ontology_version: string
  rule_version?: string
  function_version?: string
  source_row_ids?: string[]
  /** 双源拼接标记：state=活链 / version=DuckDB 历史链 */
  chain_source?: 'state' | 'version'
}

export interface AuditPage {
  items: AuditItem[]
  total: number
  page: number
  page_size: number
}

export interface AuditVerifyDto {
  chain_ok: boolean
  expected_count: number
  actual_count: number
  broken_links: unknown[]
  missing_fields?: string[]
  disposal_events?: number
  empty_chain?: boolean
  cross_check?: {
    disposal_events: number
    persisted_non_pending: number
    actions_applied: number
    consistent: boolean
  }
  chain_source?: 'state' | 'version'
}

export interface AuditQuery {
  operator?: string
  action?: string
  clue_id?: string
  from_ts?: string
  to_ts?: string
  page?: number
  page_size?: number
}

export const auditApi = {
  /** GET /cases/{cid}/audit —— 时间线（双源归并，按时间升序） */
  async timeline(caseId: string, q: AuditQuery = {}): Promise<AuditPage> {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(q)) {
      if (v !== undefined && v !== '' && v !== null) params.set(k, String(v))
    }
    const qs = params.toString()
    const res = await api.get<AuditPage>(
      `/cases/${encodeURIComponent(caseId)}/audit${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/audit/verify —— 完整性自检（空链 chain_ok=false，红线 FE-T-015） */
  async verify(caseId: string): Promise<AuditVerifyDto> {
    const res = await api.post<AuditVerifyDto>(
      `/cases/${encodeURIComponent(caseId)}/audit/verify`,
      {},
    )
    return res.data
  },
}
