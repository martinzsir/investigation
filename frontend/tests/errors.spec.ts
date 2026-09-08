import { describe, expect, it } from 'vitest'
import { ApiError, LOGIN_FAIL_TEXT, NETWORK_DOWN_TEXT, NOT_FOUND_TEXT, presentError } from '../src/api/errors'

describe('FE-I-005 错误码映射（唯一事实源）', () => {
  it('七码全覆盖映射正确', () => {
    const cases: Array<[ApiError['code'], string]> = [
      ['UNAUTHORIZED', 'auth'],
      ['FORBIDDEN', 'permission'],
      ['NOT_FOUND', 'notfound'],
      ['VALIDATION', 'validation'],
      ['CONFLICT', 'conflict'],
      ['DEGRADED_WRITE_REJECTED', 'degraded'],
      ['INTERNAL', 'internal'],
    ]
    for (const [code, kind] of cases) {
      const p = presentError(new ApiError(code, 'x', 500))
      expect(p.kind).toBe(kind)
    }
  })

  it('UNAUTHORIZED → 跳登录；DEGRADED_WRITE_REJECTED/NETWORK → 禁写；CONFLICT → 跳既有任务', () => {
    expect(presentError(new ApiError('UNAUTHORIZED', '', 401)).redirectToLogin).toBe(true)
    expect(presentError(new ApiError('DEGRADED_WRITE_REJECTED', '', 409)).degradeWrites).toBe(true)
    expect(presentError(new ApiError('NETWORK', '', 0)).degradeWrites).toBe(true)
    expect(presentError(new ApiError('CONFLICT', '', 409)).reuseTask).toBe(true)
  })

  it('非 ApiError（网络层抛出）→ network 态，不静默', () => {
    const p = presentError(new TypeError('fetch failed'))
    expect(p.kind).toBe('network')
    expect(p.title).toBe(NETWORK_DOWN_TEXT)
  })

  it('红线八统一文案常量与 404 统一文案存在', () => {
    expect(LOGIN_FAIL_TEXT).toBe('账号或密码错误')
    expect(NOT_FOUND_TEXT).toBe('不存在或无权访问')
  })
})
