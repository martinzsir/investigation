import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { setTransport } from '../src/api/transport'
import { errEnvelope, FakeTransport, okEnvelope } from './helpers'
import { useAuthStore } from '../src/stores/auth'

function routes() {
  return new FakeTransport([
    {
      match: (r) => r.method === 'POST' && r.path === '/auth/login',
      respond: (r) =>
        (r.body as { operator: string; password: string }).password === 'pw'
          ? okEnvelope({
              token: 'tok-1',
              expires_at: '2099-01-01T00:00:00',
              operator: '王检察官',
              role: 'human',
              clearance: 4,
              tenant_id: 't1',
            })
          : errEnvelope(401, 'UNAUTHORIZED', '账号或密码错误'),
    },
    {
      match: (r) => r.method === 'GET' && r.path === '/auth/me',
      respond: () => okEnvelope({ operator: '王检察官', role: 'human', clearance: 4, tenant_id: 't1' }),
    },
    {
      match: (r) => r.method === 'POST' && r.path === '/auth/logout',
      respond: () => okEnvelope({ revoked: true }),
    },
  ])
}

beforeEach(() => {
  setActivePinia(createPinia())
  setTransport(routes())
  sessionStorage.clear()
})

describe('FE-I-008 认证模块 + D9 token 存储', () => {
  it('登录成功：token 入内存 + sessionStorage，me 就位', async () => {
    const auth = useAuthStore()
    expect(await auth.login('王检察官', 'pw')).toBe(true)
    expect(auth.token).toBe('tok-1')
    expect(sessionStorage.getItem('sunzi.token')).toBe('tok-1')
    expect(auth.me?.operator).toBe('王检察官')
    expect(auth.isAuthenticated).toBe(true)
  })

  it('登录失败：统一返回 false、会话清空（文案由 LoginView 用 LOGIN_FAIL_TEXT）', async () => {
    const auth = useAuthStore()
    expect(await auth.login('幽灵账号', 'bad')).toBe(false)
    expect(auth.token).toBe('')
    expect(sessionStorage.getItem('sunzi.token')).toBeNull()
    expect(auth.me).toBeNull()
  })

  it('登出：调用端点并清 token（FE-I-008）', async () => {
    const auth = useAuthStore()
    await auth.login('王检察官', 'pw')
    await auth.logout()
    expect(auth.token).toBe('')
    expect(sessionStorage.getItem('sunzi.token')).toBeNull()
  })

  it('红线：token 与会话痕迹不落 localStorage', async () => {
    const auth = useAuthStore()
    await auth.login('王检察官', 'pw')
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k) keys.push(k)
    }
    expect(keys.filter((k) => k.startsWith('sunzi.'))).toEqual([])
  })
})
