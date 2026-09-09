import { getTransport } from './transport'
import type { StreamHandle } from './transport/types'
import type { TaskRow } from '../domain/task'

// FE-I-006 SSE 客户端（决策 2）：Bearer 鉴权与普通端点同源，fetch-event-source
// 消费（@microsoft/fetch-event-source，禁原生 EventSource、不用 Cookie）。
// Last-Event-ID 重放与指数退避重连由 transport 层内建（fetch.transport.stream）。
// 事件体 = 后端 task_dto（asdict(TaskRow)，snake_case），事件名 progress/terminal/error。

/** SSE 事件帧解析后的任务快照（即 TaskRow 直出） */
export type TaskEvent = TaskRow

/**
 * 解析一帧 SSE data（任务对象 JSON）。
 * 字段按后端 TaskRow snake_case：progress_pct/progress_stage/progress_label/
 * progress_detail（纯文本）/error_code/error_message。
 */
export function parseTaskEvent(raw: string): TaskEvent {
  const d = JSON.parse(raw) as Partial<TaskRow>
  return {
    id: String(d.id ?? ''),
    case_id: String(d.case_id ?? ''),
    task_type: String(d.task_type ?? ''),
    params: (d.params ?? {}) as Record<string, unknown>,
    status: (d.status as TaskRow['status']) ?? 'PENDING',
    progress_pct: typeof d.progress_pct === 'number' ? d.progress_pct : 0,
    progress_stage: String(d.progress_stage ?? ''),
    progress_label: String(d.progress_label ?? ''),
    progress_detail: String(d.progress_detail ?? ''),
    retry_count: typeof d.retry_count === 'number' ? d.retry_count : 0,
    max_retries: typeof d.max_retries === 'number' ? d.max_retries : 3,
    idem_key: String(d.idem_key ?? ''),
    created_at: String(d.created_at ?? ''),
    updated_at: String(d.updated_at ?? ''),
    started_at: String(d.started_at ?? ''),
    finished_at: String(d.finished_at ?? ''),
    error_code: String(d.error_code ?? ''),
    error_message: String(d.error_message ?? ''),
    created_by: String(d.created_by ?? ''),
  }
}

export interface TaskStreamHandlers {
  /** 任务快照（progress / terminal 帧均触发；终态由调用方按 status 判定） */
  onTask: (t: TaskEvent) => void
  /** error 事件帧（任务不存在等） */
  onErrorEvent?: (message: string) => void
  /** 传输层错误/断线（库内自动重连；>3s 提示由调用方计时） */
  onTransportError?: (err: unknown) => void
  onClose?: () => void
}

/**
 * 订阅任务进度流。
 * @param taskId 任务 ID
 * @param handlers 事件回调
 */
export function taskEvents(taskId: string, handlers: TaskStreamHandlers): StreamHandle {
  return getTransport().stream({
    path: `/tasks/${encodeURIComponent(taskId)}/events`,
    handlers: {
      onEvent: (msg) => {
        // error 事件帧：data 为 {message}
        if (msg.event === 'error') {
          try {
            const d = JSON.parse(msg.data) as { message?: string }
            handlers.onErrorEvent?.(d.message ?? '任务错误')
          } catch {
            handlers.onErrorEvent?.('任务错误')
          }
          return
        }
        try {
          handlers.onTask(parseTaskEvent(msg.data))
        } catch {
          // 非 JSON 事件帧（心跳等）忽略
        }
      },
      onError: handlers.onTransportError,
      onClose: handlers.onClose,
    },
  })
}
