import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { HealthSection } from '../../domain/clue'

// W-018 治理仪表盘 + W-022 异常通道（server/app/dashboard.py 契约）。

export interface DashboardDto {
  case_id: string
  health: HealthSection & { run_id?: string; 分类计数?: Record<string, number> }
  diagnostics: {
    total: number
    by_kind: Record<string, number>
    by_severity: { info?: number; warning?: number; critical?: number }
  }
  coverage?: {
    declared: Array<{ reason?: string; missing?: string[]; severity?: string }>
    empirical: Array<{ reason?: string; missing?: string[]; severity?: string }>
  }
  todo?: {
    disposal?: {
      available: boolean
      total: number
      by_status: Record<string, number>
      source?: 'state' | 'version'
    }
    review?: { available: boolean; pending: number; note?: string }
  }
  by_severity?: { info?: number; warning?: number; critical?: number }
  [key: string]: unknown
}

export interface AnomalyItem {
  clue_id: string
  title: string
  severity?: string
  diagnostic_ids?: string[]
  dimension?: string[]
  [key: string]: unknown
}

export const dashboardApi = {
  /** GET /cases/{cid}/dashboard —— 健康度/诊断/双覆盖/待办聚合 */
  async get(caseId: string): Promise<DashboardDto> {
    const res = await api.get<DashboardDto>(`/cases/${encodeURIComponent(caseId)}/dashboard`)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/anomalies —— 异常线索通道：级别恒「待核实」，不参与交叉升格 */
  async anomalies(caseId: string): Promise<{ items: AnomalyItem[]; total: number }> {
    const res = await api.get<{ items: AnomalyItem[]; total: number }>(
      `/cases/${encodeURIComponent(caseId)}/anomalies`,
    )
    return res.data
  },
}
