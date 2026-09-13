import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createAutosaver } from '../src/domain/canvas-autosave'

// RC-205 M3 复用：防抖自动保存器
//  - 窗口内多次改动合并为一次保存（最新值）；
//  - flush 立即落盘；
//  - 在途保存期间的改动会补存；
//  - 失败计数 + 自动重试，成功后清零；cancel 丢弃待保存内容。

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('createAutosaver 防抖合并', () => {
  it('coalesces 10 rapid changes into one save with the latest value', async () => {
    const save = vi.fn(async (_v: number) => {})
    const a = createAutosaver<number>({ save, delay: 500 })
    for (let i = 1; i <= 10; i += 1) a.schedule(i)
    expect(save).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(500)
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenLastCalledWith(10)
    await a.flush()
    expect(save).toHaveBeenCalledTimes(1)
    expect(a.pending()).toBe(false)
  })

  it('flush saves immediately without waiting for the debounce window', async () => {
    const save = vi.fn(async (_v: string) => {})
    const a = createAutosaver<string>({ save, delay: 500 })
    a.schedule('x')
    await a.flush()
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenLastCalledWith('x')
  })

  it('retries after a failure and resets failure count on success', async () => {
    let failOnce = true
    const save = vi.fn(async (_v: number) => {
      if (failOnce) {
        failOnce = false
        throw new Error('network')
      }
    })
    const onError = vi.fn()
    const a = createAutosaver<number>({ save, delay: 500, onError })
    a.schedule(7)
    await vi.advanceTimersByTimeAsync(500)
    expect(save).toHaveBeenCalledTimes(1)
    expect(a.failureCount()).toBe(1)
    expect(onError).toHaveBeenCalledTimes(1)
    // 失败后按窗口自动重试
    await vi.advanceTimersByTimeAsync(500)
    expect(save).toHaveBeenCalledTimes(2)
    expect(save).toHaveBeenLastCalledWith(7)
    expect(a.failureCount()).toBe(0)
  })

  it('cancel drops pending changes', async () => {
    const save = vi.fn(async () => {})
    const a = createAutosaver<void>({ save, delay: 500 })
    a.schedule(undefined)
    a.cancel()
    await vi.advanceTimersByTimeAsync(1000)
    expect(save).not.toHaveBeenCalled()
    expect(a.pending()).toBe(false)
  })

  it('saves follow-up change that arrived while a save was inflight', async () => {
    let release: (() => void) | null = null
    const save = vi.fn(
      (v: number) =>
        new Promise<void>((resolve) => {
          if (v === 1) {
            release = resolve
          } else {
            resolve()
          }
        }),
    )
    const a = createAutosaver<number>({ save, delay: 500 })
    a.schedule(1)
    await vi.advanceTimersByTimeAsync(500)
    expect(save).toHaveBeenCalledTimes(1)
    expect(a.inflight()).toBe(true)

    // 在途期间排入新改动（重新计时 500ms）
    a.schedule(2)
    // 闭包内赋值，TS 控制流在调用点将 release 窄化为 null，需显式断言
    ;(release as (() => void) | null)?.()
    await vi.advanceTimersByTimeAsync(500)
    expect(save).toHaveBeenCalledTimes(2)
    expect(save).toHaveBeenLastCalledWith(2)
    await a.flush()
  })
})
