import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type {
  VerifyDraftProposal,
  VerifyItemsPage,
  VerifyRequestsPage,
} from '../../domain/verify'

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

/** REQ-V-019 草案端点响应（degraded/blocked 均 200，按 ok 字段分流） */
export interface VerifyDraftResult {
  ok: boolean
  /** 能力闸/选档 off（零模型调用） */
  degraded?: boolean
  /** 授权/端点闸拦截 */
  blocked?: boolean
  /** local | cloud | off */
  mode?: string
  model?: string | null
  /** degraded 原因（off 档） */
  reason?: string
  /** blocked/调用失败原因 */
  error?: string
  proposals?: VerifyDraftProposal[]
  dropped?: Array<{ index: number; reason: string }>
  duplicates?: number
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

  /**
   * REQ-V-019 POST .../verify-items/draft —— LLM 核查方向草案（同步；
   * 人显式点击触发）。degraded/blocked 也是 200：按返回的 ok/degraded/mode
   * 渲染档位提示，不当 5xx 报错。草案只落提案队列（shadow），成功后
   * 不自动刷新核查项列表（未成项）。
   */
  async draft(caseId: string, clueId: string): Promise<VerifyDraftResult> {
    const res = await api.post<VerifyDraftResult>(
      `${base(caseId, clueId)}/draft`,
      {},
    )
    return res.data
  },

  /**
   * REQ-V-017 POST .../verify-items/{itemId}/replay —— 一键复跑回填
   * （202 入队 TASK_VERIFY op=replay；只读 Function 结果回填 replay_json，
   * 不改状态/结论）。复跑允许重复执行：不传固定 idempotencyAction（服务端
   * 自产秒级时间戳 idem_key），同秒重试幂等、跨秒可再跑。
   */
  async replay(
    caseId: string,
    clueId: string,
    itemId: string,
  ): Promise<VerifyTaskResult> {
    const res = await api.post<VerifyTaskResult>(
      `${base(caseId, clueId)}/${encodeURIComponent(itemId)}/replay`,
      {},
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}

// REQ-V-013 调取清单台账（案件级 GET + 202 入队，同上方纪律）：
// 创建 POST /cases/{cid}/clues/{clueId}/verify-requests（op=add_request）；
// 迁移 POST /cases/{cid}/verify-requests/{requestId}/transitions
// （发起/回执登记/关闭；超期徽标为服务端读面派生，前端只透传不计算）。

/** REQ-V-013 创建调取请求体（target/material 必填，其余可空） */
export interface VerifyRequestCreateBody {
  target: string
  material: string
  legal_instrument?: string
  handler?: string
  /** YYYY-MM-DD 或空 */
  due_date?: string
  note?: string
  /** 关联核查项（可空） */
  item_id?: string
}

export const requestApi = {
  /** GET /cases/{caseId}/verify-requests?clue_id= —— 台账（overdue 派生列） */
  async list(caseId: string, clueId?: string): Promise<VerifyRequestsPage> {
    const qs = clueId ? `?clue_id=${encodeURIComponent(clueId)}` : ''
    const res = await api.get<VerifyRequestsPage>(
      `/cases/${encodeURIComponent(caseId)}/verify-requests${qs}`)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../clues/{clueId}/verify-requests —— 发起调取登记（202） */
  async create(
    caseId: string,
    clueId: string,
    body: VerifyRequestCreateBody,
  ): Promise<VerifyTaskResult> {
    const res = await api.post<VerifyTaskResult>(
      `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}/verify-requests`,
      body,
      { idempotencyAction: `verify-req-create:${clueId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../verify-requests/{requestId}/transitions —— 发起/回执登记/关闭（202） */
  async transition(
    caseId: string,
    requestId: string,
    nextStatus: string,
  ): Promise<VerifyTaskResult> {
    const res = await api.post<VerifyTaskResult>(
      `/cases/${encodeURIComponent(caseId)}/verify-requests/${encodeURIComponent(requestId)}/transitions`,
      { next_status: nextStatus },
      { idempotencyAction: `verify-req-t:${requestId}:${nextStatus}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
