import { fetchEventSource } from '@microsoft/fetch-event-source'
import { getToken } from '../token'
import type {
  HttpTransport,
  RawResponse,
  StreamHandle,
  StreamRequest,
  TimeoutKind,
  TransportRequest,
} from './types'

/** FE-I-004 超时分层：普通 15s / 上传 120s / 跨案 60s */
export const TIMEOUT_MS: Record<TimeoutKind, number> = {
  default: 15_000,
  upload: 120_000,
  cross: 60_000,
}

/** D8：基址 /api/v1 发布即冻结；环境变量仅限特殊部署覆盖 */
export const BASE_URL = import.meta.env.VITE_API_BASE ?? '/api/v1'

function buildUrl(path: string, query?: TransportRequest['query']): string {
  const url = BASE_URL + path
  if (!query) return url
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined) qs.set(k, String(v))
  }
  const s = qs.toString()
  return s ? `${url}?${s}` : url
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json', ...extra }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

/** 形态一（私有化主力）：同源 fetch；SSE 经 fetch-event-source（D7，禁原生 EventSource） */
export class FetchTransport implements HttpTransport {
  readonly kind = 'fetch' as const

  async request(req: TransportRequest): Promise<RawResponse> {
    const hasBody = req.body !== undefined
    const headers = authHeaders(req.headers)
    if (hasBody && !(req.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json'
    }
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS[req.timeoutKind ?? 'default'])
    try {
      const res = await fetch(buildUrl(req.path, req.query), {
        method: req.method,
        headers,
        body: hasBody
          ? req.body instanceof FormData
            ? req.body
            : JSON.stringify(req.body)
          : undefined,
        signal: req.signal ?? ctrl.signal,
      })
      let data: unknown = null
      if (req.responseType === 'blob') {
        // 二进制下载（案件包 zip）：成功取 Blob；非 2xx 回退 JSON 信封取错误文案
        if (res.ok) {
          data = await res.blob()
        } else {
          try {
            data = await res.json()
          } catch {
            data = null
          }
        }
      } else {
        try {
          data = await res.json()
        } catch {
          data = null
        }
      }
      return { status: res.status, data }
    } finally {
      clearTimeout(timer)
    }
  }

  stream(req: StreamRequest): StreamHandle {
    const ctrl = new AbortController()
    let stop = false
    void fetchEventSource(buildUrl(req.path), {
      method: 'GET',
      headers: authHeaders(req.headers),
      signal: ctrl.signal,
      // 后台标签页也保持连接（任务进度不丢）
      openWhenHidden: true,
      onopen: async (res) => {
        if (!res.ok) {
          // 401 会话失效：不重试，交由上层统一处理
          if (res.status === 401) stop = true
          throw new Error(`SSE HTTP ${res.status}`)
        }
      },
      onmessage: (msg) => {
        req.handlers.onEvent({
          event: msg.event,
          data: msg.data,
          lastEventId: msg.id || undefined,
        })
      },
      // 不抛出 = 交给库内指数退避重连（FE-I-006）；401 置 stop 后抛出终止
      onerror: (err) => {
        req.handlers.onError?.(err)
        if (stop) throw err
      },
      onclose: () => {
        req.handlers.onClose?.()
      },
    })
    return { close: () => ctrl.abort() }
  }
}
