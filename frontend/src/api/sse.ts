import { getTransport } from './transport'
import type { StreamHandle } from './transport/types'

// FE-I-006 SSE 客户端（D7）：Bearer 鉴权与普通端点同源，fetch-event-source 消费，
// 禁原生 EventSource、不用 Cookie。Last-Event-ID 重放与指数退避重连由 transport 层内建。

/** 决策 11：SSE 事件体 progress_pct/stage/label/detail */
export interface TaskProgressEvent {
  taskId: string
  stage: string
  pct: number
  label: string
  detail?: string
  status?: string
  /** SUCCEEDED/FAILED/CANCELLED 终态（FE-I-014：terminal 携带新 data_version 触发失效） */
  terminal: boolean
}

export function parseProgress(taskId: string, raw: string): TaskProgressEvent {
  const d = JSON.parse(raw) as Record<string, unknown>
  const status = typeof d.status === 'string' ? d.status : undefined
  return {
    taskId,
    stage: String(d.stage ?? ''),
    pct: typeof d.progress_pct === 'number' ? d.progress_pct : 0,
    label: String(d.label ?? d.stage_label ?? ''),
    detail: typeof d.detail === 'string' ? d.detail : undefined,
    status,
    terminal: status === 'SUCCEEDED' || status === 'FAILED' || status === 'CANCELLED',
  }
}

export function taskEvents(
  taskId: string,
  onEvent: (e: TaskProgressEvent) => void,
  onError?: (err: unknown) => void,
): StreamHandle {
  return getTransport().stream({
    path: `/tasks/${encodeURIComponent(taskId)}/events`,
    handlers: {
      onEvent: (msg) => {
        try {
          onEvent(parseProgress(taskId, msg.data))
        } catch {
          // 非 JSON 事件帧（心跳等）忽略
        }
      },
      onError,
    },
  })
}
