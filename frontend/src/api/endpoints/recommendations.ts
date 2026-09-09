import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { TaskRow } from '../../domain/task'
import type { DeDecision, DeReco } from '../../domain/recommend'

// 接入建议确认端点（server/app/routers/research.py de-recommendations）。
// 红线：推荐为待核实草案；adopt/reject 只记 state + 审计，永不自动改 bindings。

export const recommendationsApi = {
  /** GET /cases/{cid}/de-recommendations —— 推荐列表（待核实/采纳/驳回） */
  async list(caseId: string): Promise<{ items: DeReco[]; total: number }> {
    const res = await api.get<{ items: DeReco[]; total: number }>(
      `/cases/${encodeURIComponent(caseId)}/de-recommendations`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/de-recommendations（202）—— 对暂存件生成推荐（同 upload 幂等） */
  async create(caseId: string, uploadId: string): Promise<TaskRow | { reused: boolean }> {
    const res = await api.post<TaskRow | { reused: boolean }>(
      `/cases/${encodeURIComponent(caseId)}/de-recommendations`,
      { upload_id: uploadId },
      { idempotencyAction: `de-reco:${uploadId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/de-recommendations/{rid}/decide（202）—— 采纳/驳回（入队裁决任务） */
  async decide(
    caseId: string,
    rid: string,
    decision: DeDecision,
    note = '',
  ): Promise<TaskRow> {
    const res = await api.post<TaskRow>(
      `/cases/${encodeURIComponent(caseId)}/de-recommendations/${encodeURIComponent(rid)}/decide`,
      { decision, note },
      { idempotencyAction: `de-decide:${rid}:${decision}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
