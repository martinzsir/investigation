import { FetchTransport } from './fetch.transport'
import { IpcTransport } from './ipc.transport'
import type { HttpTransport } from './types'

let instance: HttpTransport | null = null

/** 启动时按 window.sunzi 是否存在选择 transport（D6：业务代码零平台分支） */
export function getTransport(): HttpTransport {
  if (!instance) {
    instance =
      typeof window !== 'undefined' && window.sunzi ? new IpcTransport() : new FetchTransport()
  }
  return instance
}

/** 仅供测试注入假传输层 */
export function setTransport(t: HttpTransport | null): void {
  instance = t
}

export type { HttpTransport } from './types'
export { FetchTransport } from './fetch.transport'
export { IpcTransport } from './ipc.transport'
