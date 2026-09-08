// D9：token 只存内存 + sessionStorage（重载不丢、关页即失），绝不落 localStorage。
// ApiClient 只向本模块取 token，不感知存储位置（Electron 形态由 IPC 桥接管）。
const KEY = 'sunzi.token'

let memoryToken: string | null = null

export function getToken(): string | null {
  if (memoryToken !== null) return memoryToken
  try {
    memoryToken = sessionStorage.getItem(KEY)
  } catch {
    // 无 storage 环境退化为纯内存
  }
  return memoryToken
}

export function setToken(token: string): void {
  memoryToken = token
  try {
    sessionStorage.setItem(KEY, token)
  } catch {
    // 忽略
  }
}

export function clearToken(): void {
  memoryToken = null
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    // 忽略
  }
}
