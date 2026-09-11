import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { VerifyItemsPage } from '../../domain/verify'

// REQ-V-005 核查工作区端点（server/app/routers/clues.py 契约）：
// 读同步返回清单/进度；写一律 202 入队 TASK_VERIFY（FIFO、不产 DuckDB 版本文件），
// operator 由会话快照，请求体不含身份。

/** 202 任务回执（TaskRow 子集；进度走任务 SSE） */
export interface VerifyTaskResult {
  id: string
  status: string
  task_type: string
  [key: string]: unknown
}

export interface VerifyTransitionBody {
  next_status: string
  /** 终态裁决（已证实/已查否）必填；无法核实可空 */
  conclusion?: string
  /** 仅采纳建议项（建议→待核查）有效：改写建议文本，其余迁移服务端忽略 */
  text?: string
}

const base = (caseId: string, clueId: string) =>
  `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}/verify-items`

export const verifyApi = {
  /** GET .../verify-items —— 纯读 state 真值；工作区未初始化 available:false */
  async list(caseId: string, clueId: string): Promise<VerifyItemsPage> {
    const res = await api.get<VerifyItemsPage>(base(caseId, clueId))
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../verify-items —— 人工添加核查项（结构化构造器同通道） */
  async add(caseId: string, clueId: string, text: string): Promise<VerifyTaskResult> {
    const res = await api.post<VerifyTaskResult>(
      base(caseId, clueId),
      { text },
      { idempotencyAction: `verify-add:${clueId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../verify-items/{itemId}/transitions —— 裁决/采纳/忽略/重开 */
  async transition(
    caseId: string,
    clueId: string,
    itemId: string,
    body: VerifyTransitionBody,
  ): Promise<VerifyTaskResult> {
    const res = await api.post<VerifyTaskResult>(
      `${base(caseId, clueId)}/${encodeURIComponent(itemId)}/transitions`,
      body,
      { idempotencyAction: `verify:${itemId}:${body.next_status}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
