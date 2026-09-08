import { api } from '../client'
import { noteDataVersion } from '../query-keys'

export interface CaseDto {
  id: string
  tenant_id: string
  name: string
  status: string
  pack_id: string
  pack_snapshot_at: string
  created_at: string
  created_by: string
}

/** FE-C-027 数据源：诊断分级计数（dashboard.py by_severity） */
export interface DashboardDto {
  by_severity?: { info?: number; warning?: number; critical?: number }
  [key: string]: unknown
}

export const casesApi = {
  /** GET /cases → 案件 dto 数组 */
  async list(): Promise<CaseDto[]> {
    const { data } = await api.get<CaseDto[]>('/cases')
    return data
  },

  async dashboard(caseId: string): Promise<DashboardDto> {
    const res = await api.get<DashboardDto>(`/cases/${encodeURIComponent(caseId)}/dashboard`)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
