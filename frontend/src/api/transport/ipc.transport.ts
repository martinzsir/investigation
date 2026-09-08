import type {
  HttpTransport,
  RawResponse,
  StreamHandle,
  StreamRequest,
  TransportRequest,
} from './types'

// 形态二/三（Electron IPC 桥）占位实现：接口先立、renderer 未来零改动（D6 / FE-I-013）。
// Electron 壳属 MVP 后期立壳（FE-B-007/008、FE-T-023），MVP-0 不交付；此处仅保证
// window.sunzi 注入时可被选中，Web 形态回退 fetch，业务代码零平台分支。

interface SunziBridge {
  request(req: TransportRequest): Promise<RawResponse>
  stream(req: StreamRequest): StreamHandle
}

declare global {
  interface Window {
    sunzi?: SunziBridge
  }
}

export class IpcTransport implements HttpTransport {
  readonly kind = 'ipc' as const

  private readonly bridge: SunziBridge

  constructor() {
    const bridge = window.sunzi
    if (!bridge) {
      throw new Error('Electron IPC 桥未注入（window.sunzi 不存在）；Web 形态请使用 FetchTransport')
    }
    this.bridge = bridge
  }

  request(req: TransportRequest): Promise<RawResponse> {
    return this.bridge.request(req)
  }

  stream(req: StreamRequest): StreamHandle {
    return this.bridge.stream(req)
  }
}
