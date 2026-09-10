import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { TaskRow } from './tasks'

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

/** W-P-015 门户汇总：GET /cases/{cid}/summary */
export interface CaseSummaryDto {
  case: CaseDto
  data_version: number
  todos: {
    clues_pending: number
    review_pending: number
    anomalies_pending: number
  }
  health: {
    /** state.sqlite 审计链校验（无 state 视为 true） */
    chain_ok: boolean
    /** 诊断出现 critical */
    degraded: boolean
    diagnostics_warn: number
  }
  recent_tasks: TaskRow[]
}

export interface CreateCaseBody {
  case_id: string
  name: string
  pack_id?: string
}

export const casesApi = {
  /** GET /cases → 案件 dto 裸数组（无查询参数；状态/搜索筛选前端客户端做，B1） */
  async list(): Promise<CaseDto[]> {
    const { data } = await api.get<CaseDto[]>('/cases')
    return data
  },

  /** POST /cases 建案（登录即可；重复 409、pack 不存在 404） */
  async create(body: CreateCaseBody): Promise<CaseDto> {
    const { data } = await api.post<CaseDto>(
      '/cases',
      { pack_id: 'default', ...body },
      { idempotencyAction: `case-create:${body.case_id}` },
    )
    return data
  },

  /** GET /cases/{cid} */
  async get(caseId: string): Promise<CaseDto> {
    const { data } = await api.get<CaseDto>(`/cases/${encodeURIComponent(caseId)}`)
    return data
  },

  /** GET /cases/{cid}/summary —— 门户卡片汇总（待办三计数 + chain 健康点 + 最近任务） */
  async summary(caseId: string): Promise<CaseSummaryDto> {
    const res = await api.get<CaseSummaryDto>(
      `/cases/${encodeURIComponent(caseId)}/summary`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * POST /cases/{cid}/close 🔒 200（clearance≥2；侦查中→已结案）。
   */
  async close(caseId: string, reason = ''): Promise<CaseDto> {
    const { data } = await api.post<CaseDto>(
      `/cases/${encodeURIComponent(caseId)}/close`,
      { reason },
    )
    return data
  },

  /**
   * POST /cases/{cid}/archive 🔒 202（clearance≥2；已封存/非法迁移 409）。
   * 返 task_dto（ARCHIVE 任务，走任务中心 SSE）。
   */
  async archive(caseId: string, reason = ''): Promise<TaskRow> {
    const { data } = await api.post<TaskRow>(
      `/cases/${encodeURIComponent(caseId)}/archive`,
      { reason },
      { idempotencyAction: `case-archive:${caseId}` },
    )
    return data
  },

  async dashboard(caseId: string): Promise<DashboardDto> {
    const res = await api.get<DashboardDto>(`/cases/${encodeURIComponent(caseId)}/dashboard`)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
