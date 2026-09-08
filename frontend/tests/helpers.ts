import type {
  HttpTransport,
  RawResponse,
  StreamHandle,
  StreamRequest,
  TransportRequest,
} from '../src/api/transport/types'

export interface FakeRoute {
  match: (req: TransportRequest) => boolean
  respond: (req: TransportRequest) => RawResponse | Promise<RawResponse>
}

/** 测试假传输层：记录调用、按路由匹配应答（client 单测不依赖网络） */
export class FakeTransport implements HttpTransport {
  readonly kind = 'fetch' as const
  calls: TransportRequest[] = []

  constructor(private readonly routes: FakeRoute[]) {}

  async request(req: TransportRequest): Promise<RawResponse> {
    this.calls.push(req)
    const route = this.routes.find((r) => r.match(req))
    if (!route) throw new Error(`no route: ${req.method} ${req.path}`)
    return route.respond(req)
  }

  stream(_req: StreamRequest): StreamHandle {
    return { close() {} }
  }
}

export function okEnvelope(data: unknown): RawResponse {
  return { status: 200, data: { ok: true, data } }
}

export function errEnvelope(status: number, code: string, message: string): RawResponse {
  return { status, data: { ok: false, error: { code, message } } }
}
