import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import { taskEvents as streamTaskEvents, type TaskStreamHandlers } from '../sse'
import type { StreamHandle } from '../transport/types'
import { isTerminal, type TaskRow, type TaskStatus, type TaskStats } from '../../domain/task'

// 任务端点（server/app/routers/tasks.py 契约）。
// 列表服务端分页：GET /tasks?case_id=&status=&task_type=&page=&page_size=
//   status 支持逗号分隔多值，'active'=排队+运行；返回 {items,total,page,page_size,stats}。
// SSE：GET /tasks/{tid}/events（Bearer，事件体 task_dto snake_case）。

export type { TaskRow, TaskStatus }

export interface CreateTaskBody {
  task_type: string
  idem_key?: string
  params?: Record<string, unknown>
}

export interface TaskListQuery {
  /** 状态过滤：'active'（排队+运行）或单个状态；逗号分隔多值 */
  status?: string
  taskType?: string
  page?: number
  pageSize?: number
}

export interface TaskListResult {
  items: TaskRow[]
  total: number
  page: number
  page_size: number
  /** 该案件全状态计数（不受 status/task_type 过滤影响） */
  stats: TaskStats
}

export const tasksApi = {
  /** POST /cases/{cid}/tasks 🔒 入队（API 不执行；幂等键冲突返回既有任务） */
  async create(caseId: string, body: CreateTaskBody): Promise<TaskRow> {
    const res = await api.post<TaskRow>(
      `/cases/${encodeURIComponent(caseId)}/tasks`,
      body,
      { idempotencyAction: `task:${body.task_type}:${body.idem_key ?? 'na'}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /tasks —— 服务端分页 + 状态/类型过滤 */
  async list(caseId: string, query: TaskListQuery = {}): Promise<TaskListResult> {
    const params = new URLSearchParams({ case_id: caseId })
    if (query.status) params.set('status', query.status)
    if (query.taskType) params.set('task_type', query.taskType)
    if (query.page) params.set('page', String(query.page))
    if (query.pageSize) params.set('page_size', String(query.pageSize))
    const res = await api.get<TaskListResult>(`/tasks?${params.toString()}`)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /tasks/{tid} */
  async get(taskId: string): Promise<TaskRow> {
    const res = await api.get<TaskRow>(`/tasks/${encodeURIComponent(taskId)}`)
    return res.data
  },

  /** POST /tasks/{tid}/cancel 🔒 仅 PENDING 可取消（RUNNING/终态 409）；创建人或 admin */
  async cancel(taskId: string, reason = ''): Promise<{ task_id: string; status: string }> {
    const res = await api.post<{ task_id: string; status: string }>(
      `/tasks/${encodeURIComponent(taskId)}/cancel`,
      { reason },
      { idempotencyAction: `task-cancel:${taskId}` },
    )
    return res.data
  },

  /**
   * POST /tasks/{tid}/retry 🔒 仅 FAILED/CANCELLED 可重试；创建人或 admin。
   * 返回的是新任务（新 id、PENDING、空幂等键），旧任务保留终态留痕。
   */
  async retry(taskId: string): Promise<TaskRow> {
    const res = await api.post<TaskRow>(
      `/tasks/${encodeURIComponent(taskId)}/retry`,
      {},
      { idempotencyAction: `task-retry:${taskId}` },
    )
    noteDataVersion(res.data.case_id, res.dataVersion)
    return res.data
  },

  /** GET /tasks/{tid}/events（SSE 订阅，自动重连由 transport 内建） */
  stream(taskId: string, handlers: TaskStreamHandlers): StreamHandle {
    return streamTaskEvents(taskId, handlers)
  },
}

/** 轮询等待选项（短频写场景：DISPOSE/VERIFY 秒级 FIFO） */
export interface WaitTerminalOptions {
  /** 两次轮询间隔（默认 500ms；首查在入队后立即执行，不空等） */
  intervalMs?: number
  /** 最长等待（默认 30s）；超时抛 TASK_WAIT_TIMEOUT */
  timeoutMs?: number
  /** 组件卸载时中断等待 */
  signal?: AbortSignal
}

export class TaskWaitTimeoutError extends Error {
  constructor(taskId: string) {
    super(`TASK_WAIT_TIMEOUT：任务 ${taskId} 等待终态超时`)
    this.name = 'TaskWaitTimeoutError'
  }
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('aborted', 'AbortError'))
      return
    }
    const t = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = (): void => {
      clearTimeout(t)
      reject(new DOMException('aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * 轮询等待任务终态（SUCCEEDED/FAILED/CANCELLED）。
 *
 * 纪律：202 只代表「已入队」，刷新读面/提示成功必须以本函数返回的终态任务为
 * 完成信号（REQ-V-008 闭环）——禁止在入队后立即按成功处理，否则 Worker 异步
 * 失败（VERIFY_PENDING/ACTION_REJECTED 等）对用户不可见且读到旧 state。
 * 终态权威行包含 error_code/error_message，由调用方分支展示。
 */
export async function waitForTerminal(
  taskId: string,
  opts: WaitTerminalOptions = {},
): Promise<TaskRow> {
  const interval = opts.intervalMs ?? 500
  const deadline = Date.now() + (opts.timeoutMs ?? 30_000)
  for (;;) {
    const t = await tasksApi.get(taskId)
    if (isTerminal(t.status)) return t
    if (Date.now() >= deadline) throw new TaskWaitTimeoutError(taskId)
    await sleep(Math.min(interval, deadline - Date.now()), opts.signal)
  }
}
