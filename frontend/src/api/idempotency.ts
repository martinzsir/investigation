// FE-I-007：写操作幂等键——逻辑动作 → UUID，ConfirmDialog 生命周期内复用。
// 红线：绝不落 localStorage（敏感面），仅内存/sessionStorage。
const NS = 'sunzi.idem.'

function uuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `idem-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`
}

export function getIdempotencyKey(action: string): string {
  const key = NS + action
  let v: string | null = null
  try {
    v = sessionStorage.getItem(key)
  } catch {
    // 无 storage 环境退化为内存语义
  }
  if (!v) {
    v = uuid()
    try {
      sessionStorage.setItem(key, v)
    } catch {
      // 忽略
    }
  }
  return v
}

/** ConfirmDialog 关闭/提交成功后调用，下次动作生成新键 */
export function releaseIdempotencyKey(action: string): void {
  try {
    sessionStorage.removeItem(NS + action)
  } catch {
    // 忽略
  }
}
