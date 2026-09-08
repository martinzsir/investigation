import { describe, expect, it } from 'vitest'
import { getIdempotencyKey, releaseIdempotencyKey } from '../src/api/idempotency'

describe('FE-I-007 幂等键管理', () => {
  it('同一逻辑动作复用同一键', () => {
    const a1 = getIdempotencyKey('dispose:c-0231')
    const a2 = getIdempotencyKey('dispose:c-0231')
    expect(a1).toBe(a2)
  })

  it('不同动作不同键', () => {
    expect(getIdempotencyKey('action-a')).not.toBe(getIdempotencyKey('action-b'))
  })

  it('release 后生成新键', () => {
    const before = getIdempotencyKey('action-r')
    releaseIdempotencyKey('action-r')
    const after = getIdempotencyKey('action-r')
    expect(after).not.toBe(before)
  })

  it('红线：不落 localStorage，只进 sessionStorage', () => {
    getIdempotencyKey('no-localstorage')
    const localKeys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k) localKeys.push(k)
    }
    expect(localKeys.filter((k) => k.startsWith('sunzi.idem.'))).toEqual([])
    expect(sessionStorage.getItem('sunzi.idem.no-localstorage')).toBeTruthy()
  })
})
