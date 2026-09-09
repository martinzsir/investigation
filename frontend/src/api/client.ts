import { ApiError, type ErrorCode } from './errors'
import { getIdempotencyKey } from './idempotency'
import { getToken } from './token'
import { getTransport } from './transport'
import type { RawResponse, TransportRequest, TimeoutKind } from './transport/types'

/** 后端响应信封：{ok:true,data,data_version} / {ok:false,error:{code,message}} */
export interface Envelope<T> {
  ok: boolean
  data: T
  data_version?: number
  error?: { code: string; message: string; detail?: unknown }
}

export interface RequestOptions {
  timeoutKind?: TimeoutKind
  /** 写操作幂等动作名：传入则自动携带 Idempotency-Key（ConfirmDialog 生命周期内复用） */
  idempotencyAction?: string
  /** 登录/登出等端点：401 不触发全局跳转钩子 */
  skipAuthHook?: boolean
  signal?: AbortSignal
}

export interface Result<T> {
  data: T
  dataVersion?: number
}

type UnauthorizedHandler = (err: ApiError) => void
let onUnauthorized: UnauthorizedHandler | null = null

/** 401 全局钩子（main.ts 注册：清会话 → /login 保留来源路径） */
export function setUnauthorizedHandler(fn: UnauthorizedHandler | null): void {
  onUnauthorized = fn
}

const BACKEND_CODES: readonly string[] = [
  'UNAUTHORIZED',
  'FORBIDDEN',
  'NOT_FOUND',
  'VALIDATION',
  'CONFLICT',
  'DEGRADED_WRITE_REJECTED',
  'INTERNAL',
]

function codeOf(raw: string): ErrorCode {
  return BACKEND_CODES.includes(raw) ? (raw as ErrorCode) : 'INTERNAL'
}

export class ApiClient {
  async request<T>(req: TransportRequest & RequestOptions): Promise<Result<T>> {
    const headers: Record<string, string> = { ...req.headers }
    // 决策 2：普通端点与 SSE 同源 Bearer 鉴权；login 时无 token 天然不带
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
    if (req.method !== 'GET' && req.idempotencyAction) {
      headers['Idempotency-Key'] = getIdempotencyKey(req.idempotencyAction)
    }
    let raw: RawResponse
    try {
      raw = await getTransport().request({
        ...req,
        headers,
        timeoutKind: req.timeoutKind,
      })
    } catch {
      // 传输层异常（不可达/超时）：统一 NETWORK 态，不解读 httpStatus（FE-I-015）
      throw new ApiError('NETWORK', '网络异常或请求超时', 0)
    }

    const body = raw.data as Envelope<T> | null

    if (raw.status === 401) {
      const err = new ApiError(
        'UNAUTHORIZED',
        body?.error?.message ?? '未认证',
        401,
        body?.error?.detail,
      )
      if (!req.skipAuthHook) onUnauthorized?.(err)
      throw err
    }

    if (body && body.ok === true) {
      return { data: body.data, dataVersion: body.data_version }
    }

    const e = body?.error
    throw new ApiError(codeOf(e?.code ?? ''), e?.message ?? '请求失败', raw.status, e?.detail)
  }

  get<T>(path: string, opts: RequestOptions = {}): Promise<Result<T>> {
    return this.request<T>({ method: 'GET', path, ...opts })
  }

  /**
   * 二进制下载（案件包 zip 等 FileResponse）：成功返 Blob；
   * 非 2xx 回退 JSON 信封，按统一 ApiError 抛出（含 401 钩子）。
   */
  async getBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
    const headers: Record<string, string> = {}
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
    let raw: RawResponse
    try {
      raw = await getTransport().request({
        method: 'GET',
        path,
        headers,
        timeoutKind: opts.timeoutKind ?? 'upload',
        signal: opts.signal,
        responseType: 'blob',
      })
    } catch {
      throw new ApiError('NETWORK', '网络异常或下载超时', 0)
    }
    if (raw.status === 401) {
      const body = raw.data as Envelope<unknown> | null
      const err = new ApiError('UNAUTHORIZED', body?.error?.message ?? '未认证', 401)
      if (!opts.skipAuthHook) onUnauthorized?.(err)
      throw err
    }
    if (raw.status >= 200 && raw.status < 300 && raw.data instanceof Blob) {
      return raw.data
    }
    const body = raw.data as Envelope<unknown> | null
    const e = body?.error
    throw new ApiError(codeOf(e?.code ?? ''), e?.message ?? '下载失败', raw.status)
  }

  post<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<Result<T>> {
    return this.request<T>({ method: 'POST', path, body, ...opts })
  }

  put<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<Result<T>> {
    return this.request<T>({ method: 'PUT', path, body, ...opts })
  }

  delete<T>(path: string, opts: RequestOptions = {}): Promise<Result<T>> {
    return this.request<T>({ method: 'DELETE', path, ...opts })
  }
}

export const api = new ApiClient()
