import { http, HttpResponse } from 'msw'

// MSW 兜底（契约先行，决策 14）：VITE_USE_MSW=1 时启用，供无后端演示与访问层联调前开发。
// 禁止 Mock 红线：权限判定/溯源 URI/状态机门禁/审计链——本文件不含任何此类逻辑。

const ok = <T,>(data: T, status = 200) =>
  HttpResponse.json({ ok: true, data }, { status })
const fail = (code: string, message: string, status: number) =>
  HttpResponse.json({ ok: false, error: { code, message } }, { status })

export const handlers = [
  http.post('*/api/v1/auth/login', async ({ request }) => {
    const body = (await request.json()) as { operator: string; password: string }
    if (!body.operator || !body.password || body.password === 'wrong') {
      // 红线八：统一文案，不泄露账号存在性
      return fail('UNAUTHORIZED', '账号或密码错误', 401)
    }
    return ok({
      token: `mock-token-${Date.now()}`,
      expires_at: '2099-01-01T00:00:00',
      operator: body.operator,
      role: 'human',
      clearance: 4,
      tenant_id: 't1',
    })
  }),

  http.post('*/api/v1/auth/logout', () => ok({ revoked: true })),

  http.get('*/api/v1/auth/me', () =>
    ok({ operator: '王检察官', role: 'human', clearance: 4, tenant_id: 't1' }),
  ),

  http.get('*/api/v1/health', () =>
    ok({ status: 'ok', service: 'sunzi-web', version: 'msw-mock', meta: 'ok' }),
  ),

  http.get('*/api/v1/cases', () =>
    ok([
      {
        id: 'c1',
        tenant_id: 't1',
        name: '演示案件·蓝海贸易',
        status: '侦查中',
        pack_id: 'default',
        pack_snapshot_at: '2026-09-08T10:00:00',
        created_at: '2026-09-01T09:00:00',
        created_by: '王检察官',
      },
      {
        id: 'c2',
        tenant_id: 't1',
        name: '演示案件·临港仓储',
        status: '待建案',
        pack_id: 'default',
        pack_snapshot_at: '',
        created_at: '2026-09-07T09:00:00',
        created_by: '王检察官',
      },
    ]),
  ),

  http.get('*/api/v1/cases/:cid/dashboard', () =>
    ok({ by_severity: { info: 2, warning: 1, critical: 0 }, 诊断总数: 3, 计数: {} }),
  ),
]
