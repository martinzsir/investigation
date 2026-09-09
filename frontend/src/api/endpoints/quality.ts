import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { TaskRow } from '../../domain/task'

// 质量检查（server/app/routers/quality.py W-P-010 契约）。
// POST /cases/{cid}/quality-checks         🔒 触发四扫描（202 任务；进行中幂等回跳）
// GET  /cases/{cid}/quality-checks/latest   最近一次报告（无报告 {available:false}）

export type QualityCategory = 'compliance' | 'freshness' | 'sensitive' | 'unit'
export type QualityMode = 'deterministic' | 'heuristic'
/** ok=通过 / warn=确定性超期 / suggest=启发式建议（封顶不阻断，红线六）/ block=确定性违规（红线七） */
export type QualitySeverity = 'ok' | 'warn' | 'suggest' | 'block'

export interface QualityCheck {
  category: QualityCategory
  mode: QualityMode
  rule_id?: string
  obj: string
  prop: string
  severity: QualitySeverity
  count?: number
  message: string
  samples_masked: string[]
}

export interface QualitySummary {
  total: number
  passed: number
  warnings: number
  violations: number
}

export type QualityReport =
  | { available: false }
  | {
      available: true
      check_id: string
      created_at: string
      created_by: string
      data_version: number
      summary: QualitySummary
      checks: QualityCheck[]
    }

export const qualityApi = {
  /** POST /cases/{cid}/quality-checks 🔒 触发扫描（返回任务；SSE 订阅复用 tasksApi.stream） */
  async runCheck(caseId: string): Promise<TaskRow> {
    const res = await api.post<TaskRow>(
      `/cases/${encodeURIComponent(caseId)}/quality-checks`,
      {},
      { idempotencyAction: 'quality-check' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/quality-checks/latest */
  async latest(caseId: string): Promise<QualityReport> {
    const res = await api.get<QualityReport>(
      `/cases/${encodeURIComponent(caseId)}/quality-checks/latest`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
