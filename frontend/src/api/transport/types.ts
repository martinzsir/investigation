export type RequestMethod = 'GET' | 'POST' | 'PUT' | 'DELETE'

/** 超时分层（FE-I-004）：普通 15s / 上传 120s / 跨案 60s */
export type TimeoutKind = 'default' | 'upload' | 'cross'

export interface TransportRequest {
  method: RequestMethod
  /** 相对 /api/v1 的路径，如 /auth/login */
  path: string
  query?: Record<string, string | number | boolean | undefined>
  body?: unknown
  headers?: Record<string, string>
  timeoutKind?: TimeoutKind
  signal?: AbortSignal
}

export interface RawResponse {
  status: number
  data: unknown
}

export interface SseMessage {
  event: string
  data: string
  lastEventId?: string
}

export interface StreamHandlers {
  onEvent: (msg: SseMessage) => void
  onError?: (err: unknown) => void
  onClose?: () => void
}

export interface StreamHandle {
  close(): void
}

export interface StreamRequest {
  path: string
  headers?: Record<string, string>
  handlers: StreamHandlers
}

/**
 * HttpTransport：Web fetch 与 Electron IPC 桥的双实现接口（D6 / FE-I-013）。
 * 分层纪律：任何一层不得出现 if (isElectron) 分支——启动时按 window.sunzi
 * 是否存在选择实现，业务代码零平台分支。
 */
export interface HttpTransport {
  readonly kind: 'fetch' | 'ipc'
  request(req: TransportRequest): Promise<RawResponse>
  stream(req: StreamRequest): StreamHandle
}
