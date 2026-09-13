// RC-205 M3 复用：防抖自动保存器（纯 TS、无 Vue 依赖，可注入时钟做单测）。
//
// 语义：
//  - schedule(v)：合并窗口内的多次改动，窗口静止 delay 毫秒后只保存最新值；
//  - 保存进行中又有改动：改动先记账（并重新计时），本次返回后补存最新值；
//  - 保存失败：计数 +1，按 delay 自动重试最新值，直到成功或 cancel()；
//  - flush()：立即落盘当前最新值并等待在途保存完成（卸载/切页前调用）。

export interface Autosaver<T> {
  schedule: (value: T) => void
  flush: () => Promise<void>
  cancel: () => void
  readonly pending: () => boolean
  readonly inflight: () => boolean
  readonly failureCount: () => number
}

export interface AutosaverOptions<T> {
  save: (value: T) => Promise<void>
  /** 防抖窗口（含失败重试间隔），默认 500ms */
  delay?: number
  onError?: (err: unknown) => void
  setTimeout?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>
  clearTimeout?: (handle: ReturnType<typeof setTimeout>) => void
}

export function createAutosaver<T>(opts: AutosaverOptions<T>): Autosaver<T> {
  const delay = opts.delay ?? 500
  const setTimer = opts.setTimeout ?? ((fn, ms) => setTimeout(fn, ms))
  const clearTimer = opts.clearTimeout ?? ((h) => clearTimeout(h))

  let latest: T | undefined
  /** latest 是否尚未开始保存（保存启动瞬间置 false，期间再有改动置 true） */
  let dirty = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let saving = false
  let failures = 0
  let chain: Promise<void> = Promise.resolve()

  const clearTimerIfAny = () => {
    if (timer !== undefined) {
      clearTimer(timer)
      timer = undefined
    }
  }

  const arm = () => {
    clearTimerIfAny()
    timer = setTimer(fire, delay)
  }

  const startSave = () => {
    if (saving || !dirty) return
    saving = true
    dirty = false
    timer = undefined
    const value = latest as T
    chain = Promise.resolve()
      .then(() => opts.save(value))
      .then(
        () => {
          failures = 0
        },
        (err: unknown) => {
          failures += 1
          opts.onError?.(err)
          // 没落盘成功：内容仍待保存，按防抖间隔重试
          dirty = true
          arm()
        },
      )
      .then(() => {
        saving = false
      })
  }

  function fire() {
    timer = undefined
    startSave()
  }

  return {
    schedule(value: T) {
      latest = value
      dirty = true
      arm()
    },
    async flush() {
      // 至多补两轮：在途保存期间又排入的改动也立即落盘
      for (let pass = 0; pass < 2; pass += 1) {
        clearTimerIfAny()
        if (dirty && !saving) startSave()
        await chain
        if (!dirty) break
      }
    },
    cancel() {
      dirty = false
      clearTimerIfAny()
    },
    pending: () => dirty || timer !== undefined,
    inflight: () => saving,
    failureCount: () => failures,
  }
}
