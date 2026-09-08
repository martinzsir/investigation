import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiClient, setUnauthorizedHandler } from '../src/api/client'
import { ApiError } from '../src/api/errors'
import { setTransport } from '../src/api/transport'
import { setToken, clearToken } from '../src/api/token'
import { errEnvelope, FakeTransport, okEnvelope } from './helpers'

beforeEach(() => {
  setTransport(null)
  setUnauthorizedHandler(null)
  clearToken()
  sessionStorage.clear()
})

describe('ApiClient（FE-I-004）', () => {
  it('信封解包：ok:true → {data, dataVersion}', async () => {
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === '/demo',
        respond: () => ({ status: 200, data: { ok: true, data: { a: 1 }, data_version: 7 } }),
      },
    ])
    setTransport(t)
    const res = await new ApiClient().get<{ a: number }>('/demo')
    expect(res.data).toEqual({ a: 1 })
    expect(res.dataVersion).toBe(7)
  })

  it('信封失败：ok:false → 抛 ApiError(code, message, httpStatus)', async () => {
    setTransport(
      new FakeTransport([
        { match: () => true, respond: () => errEnvelope(409, 'CONFLICT', '状态机拒绝') },
      ]),
    )
    await expect(new ApiClient().post('/demo')).rejects.toMatchObject({
      code: 'CONFLICT',
      message: '状态机拒绝',
      httpStatus: 409,
    })
  })

  it('传输层异常 → NETWORK 态（组件不解读 httpStatus）', async () => {
    setTransport(
      new FakeTransport([
        {
          match: () => true,
          respond: () => {
            throw new TypeError('fetch failed')
          },
        },
      ]),
    )
    await expect(new ApiClient().get('/demo')).rejects.toMatchObject({ code: 'NETWORK' })
  })

  it('写操作自动带 Idempotency-Key 且同动作复用；GET 不带', async () => {
    const t = new FakeTransport([
      { match: () => true, respond: () => okEnvelope({}) },
    ])
    setTransport(t)
    const client = new ApiClient()
    await client.post('/demo', { x: 1 }, { idempotencyAction: 'act-1' })
    await client.post('/demo', { x: 2 }, { idempotencyAction: 'act-1' })
    await client.get('/demo')
    const posts = t.calls.filter((c) => c.method === 'POST')
    const gets = t.calls.filter((c) => c.method === 'GET')
    expect(posts[0].headers?.['Idempotency-Key']).toBeTruthy()
    expect(posts[0].headers?.['Idempotency-Key']).toBe(posts[1].headers?.['Idempotency-Key'])
    expect(gets[0].headers?.['Idempotency-Key']).toBeUndefined()
  })

  it('Bearer token 自动注入（FE-I-004）', async () => {
    setToken('tok-123')
    const t = new FakeTransport([{ match: () => true, respond: () => okEnvelope({}) }])
    setTransport(t)
    await new ApiClient().get('/demo')
    expect(t.calls[0].headers?.Authorization).toBe('Bearer tok-123')
  })

  it('401 触发全局钩子一次；skipAuthHook 不触发', async () => {
    const hook = vi.fn()
    setUnauthorizedHandler(hook)
    setTransport(
      new FakeTransport([
        { match: () => true, respond: () => errEnvelope(401, 'UNAUTHORIZED', '未认证') },
      ]),
    )
    const client = new ApiClient()
    await expect(client.get('/a')).rejects.toBeInstanceOf(ApiError)
    expect(hook).toHaveBeenCalledTimes(1)
    await expect(client.post('/logout', undefined, { skipAuthHook: true })).rejects.toBeInstanceOf(
      ApiError,
    )
    expect(hook).toHaveBeenCalledTimes(1)
  })
})
