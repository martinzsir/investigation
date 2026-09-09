import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { setTransport } from '../src/api/transport'
import { errEnvelope, FakeTransport, okEnvelope, type FakeRoute } from './helpers'

import { useAuthStore } from '../src/stores/auth'
import { useCaseStore } from '../src/stores/case'
import { casesApi } from '../src/api/endpoints/cases'
import { researchApi } from '../src/api/endpoints/research'
import { graphApi } from '../src/api/endpoints/graph'
import { crossCaseApi } from '../src/api/endpoints/crossCase'
import { packageApi } from '../src/api/endpoints/packageCase'
import { escapeHatchApi } from '../src/api/endpoints/escapeHatch'
import { settingsApi } from '../src/api/endpoints/settings'
import { auditApi } from '../src/api/endpoints/audit'

import {
  canArchive, caseIdError, filterCases, healthTone, portalViewState, sumTodos,
} from '../src/domain/portal'
import { hasGraph, nodesByDegree, truncatedBanner } from '../src/domain/graphModel'
import { applyCandidates, candidateHasNoLevelMutation } from '../src/domain/miaoSuan'
import {
  authorizeSelection, buildUnionTemplate, canExecute, executeBlockReason, hasSourceCaseColumn,
} from '../src/domain/crossCase'
import {
  canImport, chainWarning, downloadZipName, orderedSteps, sensitiveRedList, stepTone,
} from '../src/domain/packageFlow'
import { EXT_ORDER, normalizeStats, stubNameError } from '../src/domain/escapeHatch'
import {
  LOCKED_REDLINES, canSubmit, canViewAdminSettings, settingsReasonError,
} from '../src/domain/settingsModel'

// FE-T-008：登录 → 门户 → 建案 → 案件内（仪表盘/庙算/图谱）→ 跨案 → 案件包 →
// 逃生舱 → 系统设置 → 审计链/归档 的访问层全链路集成（FakeTransport 模拟后端信封，
// 红线纪律由后端路由与 domain 闸门双重保证；UI 编排走查见浏览器 E2E）。

interface Journey {
  transport: FakeTransport
  adminFlag: { value: boolean }
}

function cidOf(path: string, seg: number): string {
  return path.split('/')[seg]
}

function setupJourney(): Journey {
  const adminFlag = { value: false }
  const cases = [
    { id: 'c1', tenant_id: 't1', name: '演示案件·蓝海贸易', status: '侦查中', pack_id: 'default', pack_snapshot_at: '2026-09-08T10:00:00', created_at: '2026-09-01T09:00:00', created_by: '王检察官' },
    { id: 'c2', tenant_id: 't1', name: '演示案件·临港仓储（待建案）', status: '待建案', pack_id: 'default', pack_snapshot_at: '', created_at: '2026-09-07T09:00:00', created_by: '王检察官' },
  ]
  const crossHist: Array<{ id: number; ts: string; case_ids: string[]; sql: string; reason: string; result_rows: number }> = [
    { id: 1, ts: '2026-09-08T15:02:00', case_ids: ['c1', 'c2'], sql: 'SELECT ... UNION ALL ...', reason: '并案排查：两案人员是否重合', result_rows: 4 },
  ]
  let crossSeq = 1
  const settings = {
    queue: { max_workers: 2, poll_interval_ms: 2000 },
    resources: { max_rows_default: 10000, query_timeout_ms: 30000 },
    thresholds: { cross_level_min_sources: 3, cross_level_min_clues: 2, stale_days: 30 },
    features: { ui_density: 'comfortable' as 'comfortable' | 'compact' },
  }

  const auditEvents = [
    { seq: 1, event_id: 'evt-1', case_id: 'c1', occurred_at: '2026-09-08T10:00:00', operator: '王检察官', action: 'disposal', ontology_version: 'default@2.1.0', clue_id: 'CLUE-007', note: '建议并案核查' },
    { seq: 2, event_id: 'evt-2', case_id: 'c1', occurred_at: '2026-09-08T11:00:00', operator: '张助理', action: 'parameter_set', ontology_version: 'default@2.1.0', clue_id: '', note: '窗口参数调整' },
  ]

  const routes: FakeRoute[] = [
    {
      match: (r) => r.method === 'POST' && r.path === '/auth/login',
      respond: (r) => {
        const b = r.body as { password?: string }
        if (b.password !== 'pw' && b.password !== 'adminpw') {
          return errEnvelope(401, 'UNAUTHORIZED', '账号或密码错误')
        }
        return okEnvelope({
          token: 'tok-1', expires_at: '2099-01-01T00:00:00', operator: '王检察官',
          role: 'human', clearance: 4, tenant_id: 't1', is_admin: b.password === 'adminpw',
        })
      },
    },
    {
      match: (r) => r.method === 'GET' && r.path === '/auth/me',
      respond: () => okEnvelope({ operator: '王检察官', role: 'human', clearance: 4, tenant_id: 't1', is_admin: adminFlag.value }),
    },
    { match: (r) => r.method === 'POST' && r.path === '/auth/logout', respond: () => okEnvelope({ revoked: true }) },

    { match: (r) => r.method === 'GET' && r.path === '/cases', respond: () => okEnvelope(cases.map((c) => ({ ...c }))) },
    {
      match: (r) => r.method === 'POST' && r.path === '/cases',
      respond: (r) => {
        const b = r.body as { case_id: string; name: string; pack_id?: string }
        if (cases.some((c) => c.id === b.case_id)) return errEnvelope(409, 'CONFLICT', `案件编号已存在：${b.case_id}`)
        const c = { id: b.case_id, tenant_id: 't1', name: b.name, status: '待建案', pack_id: b.pack_id ?? 'default', pack_snapshot_at: '', created_at: '2026-09-09T12:00:00', created_by: '王检察官' }
        cases.push(c)
        return okEnvelope(c)
      },
    },
    {
      match: (r) => r.method === 'GET' && /^\/cases\/[^/]+\/summary$/.test(r.path),
      respond: (r) => {
        const cid = cidOf(r.path, 2)
        const c = cases.find((x) => x.id === cid)
        if (!c) return errEnvelope(404, 'NOT_FOUND', '案件不存在或无权访问')
        return okEnvelope({
          case: { ...c },
          data_version: 1,
          todos: cid === 'c1' ? { clues_pending: 2, review_pending: 1, anomalies_pending: 1 } : { clues_pending: 0, review_pending: 0, anomalies_pending: 0 },
          health: { chain_ok: cid !== 'c2', degraded: false, diagnostics_warn: cid === 'c2' ? 1 : 0 },
          recent_tasks: [],
        })
      },
    },
    {
      match: (r) => r.method === 'GET' && /^\/cases\/[^/]+\/dashboard$/.test(r.path),
      respond: (r) => {
        const cid = cidOf(r.path, 2)
        return okEnvelope(cid === 'c1' ? { by_severity: { info: 2, warning: 1, critical: 0 } } : { by_severity: {} })
      },
    },
    {
      match: (r) => r.method === 'GET' && /^\/cases\/[^/]+\/hypotheses$/.test(r.path),
      respond: (r) => {
        const cid = cidOf(r.path, 2)
        if (cid !== 'c1') return okEnvelope({ available: false })
        return okEnvelope({
          available: true,
          derived: true,
          coverage: {
            declared: [{ dimension: null, covered: 4, total: 5, missing: ['死间'], reason: '死间尚无分析师声明覆盖', severity: 'warning', created_at: '2026-09-08T10:00:00' }],
            empirical: [{ dimension: null, covered: 3, total: 5, missing: ['死间', '生间'], reason: '数据推导未见死间/生间模式', severity: 'warning', created_at: '2026-09-08T10:00:00' }],
          },
          heatmap: {
            jians: ['因间', '内间', '反间', '死间', '生间'],
            levels: ['观察', '线索', '确认'],
            counts: [[4, 2, 3, 0, 1], [2, 1, 1, 0, 0], [1, 0, 1, 0, 0]],
          },
          candidates: [
            { clue_id: 'CLUE-007', title: '蓝海贸易与宁波关联公司资金同日对冲', jian_types: ['因间', '反间'], level: '观察', priority_score: 81, reason: '3 笔同日反向转账，金额近似' },
            { clue_id: 'CLUE-008', title: '张某通话对象与仓储股东重合', jian_types: ['内间'], level: null, priority_score: 66, reason: '高频通话号码与股东预留号一致' },
          ],
          restricted: [{ clue_id: 'CLUE-009', reason: '内间线索：需更高秩级（主办及以上）' }],
        })
      },
    },
    {
      match: (r) => r.method === 'GET' && /^\/cases\/[^/]+\/graph/.test(r.path),
      respond: (r) => {
        const cid = cidOf(r.path, 2)
        if (cid !== 'c1') return okEnvelope({ available: false, truncated: { nodes: false, edges: false, dropped_edges: 0 }, nodes: [], edges: [] })
        return okEnvelope({
          available: true,
          truncated: { nodes: false, edges: true, dropped_edges: 7 },
          nodes: [
            { id: 'person:1', label: '张某', type: 'person', type_title: '人员', jian: ['因间'] },
            { id: 'org:1', label: '蓝海贸易有限公司', type: 'organization', type_title: '单位', jian: ['反间'] },
            { id: 'person:2', label: '李某', type: 'person', type_title: '人员', jian: ['内间'] },
            { id: 'org:2', label: '临港仓储服务有限公司', type: 'organization', type_title: '单位', jian: [] },
            { id: 'person:3', label: '王某', type: 'person', type_title: '人员', jian: ['生间'] },
            { id: 'person:4', label: '陈某', type: 'person', type_title: '人员', jian: [] },
          ],
          edges: [
            { source: 'person:1', target: 'org:1', label: '法定代表人', type: 'legal_rep' },
            { source: 'person:1', target: 'person:3', label: '转账', type: 'transfer' },
            { source: 'person:1', target: 'person:2', label: '通话', type: 'call' },
            { source: 'person:2', target: 'org:2', label: '股东', type: 'shareholder' },
            { source: 'org:1', target: 'org:2', label: '资金往来', type: 'transfer' },
          ],
        })
      },
    },
    {
      match: (r) => r.method === 'POST' && r.path === '/cross-case/query',
      respond: (r) => {
        const b = r.body as { case_ids: string[]; sql: string; reason?: string }
        const owned = new Set(cases.map((c) => c.id))
        const denied = b.case_ids.filter((id) => !owned.has(id))
        if (denied.length) return errEnvelope(403, 'FORBIDDEN', `全有或全无鉴权失败：案件 ${denied.join('、')} 无权访问，本次查询整体拒绝`)
        const head = (b.sql ?? '').trim().split(/\s+/)[0]?.toUpperCase() ?? ''
        if (!['SELECT', 'WITH', 'PRAGMA'].includes(head)) return errEnvelope(400, 'VALIDATION', '跨案 SQL 仅允许 SELECT/WITH/PRAGMA 开头（只读）')
        if (!b.reason?.trim()) return errEnvelope(400, 'VALIDATION', '查询事由必填（审计留痕）')
        const hasSourceCol = /source_case/i.test(b.sql ?? '')
        const rows = b.case_ids.flatMap((cid) =>
          [1, 2].map((n) => ({
            ...(hasSourceCol ? { source_case: cid } : {}),
            id: `${cid}-p${n}`,
            name: cid === 'c1' ? ['张某', '蓝海贸易有限公司'][n - 1] : ['李某', '临港仓储服务有限公司'][n - 1],
            type: n === 1 ? 'person' : 'organization',
          })),
        )
        crossHist.unshift({ id: ++crossSeq, ts: '2026-09-09T13:20:00', case_ids: b.case_ids, sql: b.sql, reason: b.reason, result_rows: rows.length })
        return okEnvelope({ rows, total: rows.length, case_ids: b.case_ids })
      },
    },
    {
      match: (r) => r.method === 'GET' && r.path.startsWith('/cross-case/history'),
      respond: (r) => {
        const url = new URL(r.path, 'http://x')
        const page = Number(url.searchParams.get('page') ?? '1')
        const pageSize = Number(url.searchParams.get('page_size') ?? '50')
        const start = (page - 1) * pageSize
        return okEnvelope({ items: crossHist.slice(start, start + pageSize), total: crossHist.length, page, page_size: pageSize })
      },
    },
    {
      match: (r) => r.method === 'POST' && /^\/cases\/[^/]+\/package\/export$/.test(r.path),
      respond: () => okEnvelope({ task_id: 'task-exp-1' }),
    },
    {
      match: (r) => r.method === 'GET' && /^\/packages\/[^/]+\/download$/.test(r.path),
      respond: () => ({ status: 200, data: new Blob(['PK mock package bytes'], { type: 'application/zip' }) }),
    },
    {
      match: (r) => r.method === 'POST' && r.path === '/packages/verify',
      respond: () =>
        okEnvelope({
          ok: true,
          errors: [],
          chain_ok: false, // 演示态：审计链不完整 → 橙色告警但不阻断
          file_count: 18,
          steps: [
            { key: 'format', label: '压缩包格式', status: 'pass', detail: 'zip 结构完整' },
            { key: 'manifest', label: 'manifest 清单', status: 'pass', detail: '齐备' },
            { key: 'hash', label: '哈希校验', status: 'pass', detail: 'root_hash 一致' },
            { key: 'declarations', label: '声明文件', status: 'pass', detail: '齐备' },
            { key: 'schema', label: 'schema 版本', status: 'pass', detail: 'schema_version=2' },
            { key: 'chain', label: '审计链', status: 'warn', detail: 'state.sqlite 缺失：橙色告警，不阻断导入' },
            { key: 'duckdb', label: 'DuckDB 库', status: 'pass', detail: '可读' },
          ],
          sensitive_files: ['ontology/default/case_knowledge.json'],
        }),
    },
    {
      match: (r) => r.method === 'POST' && r.path === '/packages/import',
      respond: (r) => {
        const fd = r.body as FormData
        const caseId = String(fd.get('case_id') ?? '')
        if (cases.some((c) => c.id === caseId)) {
          return errEnvelope(409, 'CONFLICT', `案件编号已存在：${caseId}（导入将创建新案件，请换编号）`)
        }
        return okEnvelope({ task_id: 'task-imp-1' })
      },
    },
    {
      match: (r) => r.method === 'POST' && r.path === '/escape-hatch/generate',
      respond: (r) => {
        const b = r.body as { ext_type: string; name: string; description?: string }
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(b.name)) return errEnvelope(400, 'VALIDATION', '名称须为标识符（字母/下划线开头）')
        return okEnvelope({
          files: [{ path: `ontology/default/ext_${b.name}.txt`, content: `// 代码桩：${b.name}\n// ${b.description ?? ''}\n// 文本产物：不写盘、不注册、不执行` }],
          registration_points: ['ontology 声明文件登记', 'core/ 注册实现', 'policies.json 同步策略'],
        })
      },
    },
    {
      match: (r) => r.method === 'GET' && r.path === '/escape-hatch/stats',
      respond: () => okEnvelope({
        items: [
          { ext_type: 'function', count: 12 },
          { ext_type: 'value_type', count: 3 },
          { ext_type: 'clean_rule', count: 5 },
          { ext_type: 'side_effect', count: 2 },
        ],
        total: 22,
      }),
    },
    {
      match: (r) => r.method === 'GET' && r.path === '/settings/health',
      respond: () => okEnvelope({
        meta_ok: true,
        queue: { pending: 1, running: 0 },
        worker: { pool_alive: false, max_workers: settings.queue.max_workers, poll_interval_ms: settings.queue.poll_interval_ms, note: 'API 进程不内嵌 Worker' },
        versions: { backend: 'M6', ontology_default: 'default' },
      }),
    },
    ...(['queue', 'resources', 'policies-thresholds', 'features'] as const).flatMap<FakeRoute>((scope) => {
      const path = `/settings/${scope}`
      const bucket = (scope === 'policies-thresholds' ? 'thresholds' : scope) as 'queue' | 'resources' | 'thresholds' | 'features'
      const key = scope === 'policies-thresholds' ? 'policies_thresholds' : scope
      return [
        {
          match: (r) => r.method === 'GET' && r.path === path,
          respond: () =>
            adminFlag.value
              ? okEnvelope(scope === 'policies-thresholds'
                ? { thresholds: { ...settings[bucket] }, note: '平台值仅用于新建案件默认' }
                : { ...settings[bucket] })
              : errEnvelope(403, 'FORBIDDEN', '平台设置仅管理员可操作'),
        },
        {
          match: (r) => r.method === 'PUT' && r.path === path,
          respond: (r) => {
            if (!adminFlag.value) return errEnvelope(403, 'FORBIDDEN', '平台设置仅管理员可操作')
            const b = r.body as { reason?: string; values?: Record<string, unknown> }
            if (!b.reason?.trim()) return errEnvelope(400, 'VALIDATION', '修改平台设置必须填写原因（审计留痕）')
            Object.entries(b.values ?? {}).forEach(([k, v]) => {
              if (v !== null && v !== undefined) (settings[bucket] as Record<string, unknown>)[k] = v
            })
            return okEnvelope({ [key]: { ...settings[bucket] } })
          },
        },
      ]
    }),
    {
      match: (r) => r.method === 'GET' && r.path === '/settings/snapshots',
      respond: () =>
        adminFlag.value
          ? okEnvelope({ items: [{ case_id: 'c1', pack_id: 'default', version: '2.1.0', locked_at: '2026-09-08T10:00:00' }], total: 1 })
          : errEnvelope(403, 'FORBIDDEN', '平台设置仅管理员可操作'),
    },
    {
      match: (r) => r.method === 'GET' && /^\/cases\/[^/]+\/audit/.test(r.path),
      respond: (r) => {
        const cid = cidOf(r.path, 2)
        const url = new URL(r.path, 'http://x')
        const clueId = url.searchParams.get('clue_id')
        let items = auditEvents.filter((e) => e.case_id === cid)
        if (clueId) items = items.filter((e) => e.clue_id === clueId)
        return okEnvelope({ items: items.sort((a, b) => b.seq - a.seq), total: items.length, page: 1, page_size: 100 })
      },
    },
    {
      match: (r) => r.method === 'POST' && /^\/cases\/[^/]+\/audit\/verify$/.test(r.path),
      respond: (r) => {
        const cid = cidOf(r.path, 2)
        const count = auditEvents.filter((e) => e.case_id === cid).length
        if (count === 0) {
          return okEnvelope({ chain_ok: false, expected_count: 0, actual_count: 0, broken_links: [], empty_chain: true, disposal_events: 0 })
        }
        return okEnvelope({
          chain_ok: true, expected_count: count, actual_count: count, broken_links: [],
          empty_chain: false, disposal_events: 1, chain_source: 'state',
          cross_check: { disposal_events: 1, persisted_non_pending: 1, actions_applied: 1, consistent: true },
        })
      },
    },
    {
      match: (r) => r.method === 'POST' && /^\/cases\/[^/]+\/archive$/.test(r.path),
      respond: (r) => {
        const b = r.body as { reason?: string }
        if (!b.reason?.trim()) return errEnvelope(400, 'VALIDATION', '归档必须填写原因（审计留痕）')
        return okEnvelope({
          id: 'task-arch-1', case_id: cidOf(r.path, 2), task_type: 'ARCHIVE', params: { reason: b.reason },
          status: 'PENDING', progress_pct: 0, progress_stage: 'queued', progress_label: '排队中', progress_detail: '',
          retry_count: 0, max_retries: 3, idem_key: 'idem-arch', created_at: '2026-09-09T14:00:00',
          updated_at: '2026-09-09T14:00:00', started_at: '', finished_at: '', error_code: '', error_message: '', created_by: '王检察官',
        })
      },
    },
  ]

  const transport = new FakeTransport(routes)
  setTransport(transport)
  return { transport, adminFlag }
}

beforeEach(() => {
  setActivePinia(createPinia())
  sessionStorage.clear()
  setupJourney()
})

async function login(admin = false): Promise<{
  auth: ReturnType<typeof useAuthStore>
  cs: ReturnType<typeof useCaseStore>
  j: Journey
  transport: FakeTransport
}> {
  const j = setupJourney()
  const auth = useAuthStore()
  const cs = useCaseStore()
  expect(await auth.login('王检察官', admin ? 'adminpw' : 'pw')).toBe(true)
  return { auth, cs, j, transport: j.transport }
}

describe('FE-T-008 全链路：登录 → 门户', () => {
  it('会话就位（is_admin 透出），门户列表 + 并发汇总 + 三态', async () => {
    const { auth, cs } = await login()
    expect(auth.isAuthenticated).toBe(true)
    expect(auth.isAdmin).toBe(false)

    await cs.loadCases()
    expect(cs.cases).toHaveLength(2)

    // A3：并发 summary（单卡失败不拖垮整页用 allSettled，此处全成功）
    const summaries = await Promise.all(cs.cases.map((c) => casesApi.summary(c.id)))
    const [s1, s2] = summaries
    expect(s1.health.chain_ok).toBe(true)
    expect(s2.health.chain_ok).toBe(false)
    expect(healthTone(s1)).toBe('ok')
    expect(healthTone(s2)).toBe('warn')
    expect(sumTodos(summaries)).toEqual({ clues: 2, review: 1, anomalies: 1 })

    // 三态：有数据；筛选无结果 → no-result；空库 → empty
    const filtered = filterCases(cs.cases, { status: 'all', q: '蓝海' })
    expect(portalViewState(cs.cases, filtered, false)).toBe('data')
    const none = filterCases(cs.cases, { status: 'all', q: '不存在的关键词' })
    expect(portalViewState(cs.cases, none, false)).toBe('no-result')
    expect(portalViewState([], [], false)).toBe('empty')

    // 归档门槛：human clearance=4 ≥ 2
    expect(canArchive(auth.clearance)).toBe(true)
  })
})

describe('FE-T-008 全链路：建案 → 案件内工作台', () => {
  it('建案成功入列、重复 409；新案空态、c1 庙算/图谱有数据', async () => {
    const { cs } = await login()

    // 前端校验先拦
    expect(caseIdError('坏 编号')).toContain('仅支持字母')

    const created = await casesApi.create({ case_id: 'c3', name: '新立案件' })
    expect(created.id).toBe('c3')
    await cs.loadCases()
    expect(cs.cases).toHaveLength(3)

    await expect(casesApi.create({ case_id: 'c3', name: '重复' })).rejects.toMatchObject({ code: 'CONFLICT', httpStatus: 409 })

    cs.selectCase('c3')
    expect(sessionStorage.getItem('sunzi.case')).toBe('c3')
    const h3 = await researchApi.hypotheses('c3')
    expect(h3.available).toBe(false)

    // c1 庙算：候补不升格（FE-T-014）
    cs.selectCase('c1')
    const h = await researchApi.hypotheses('c1')
    expect(h.available).toBe(true)
    expect(h.candidates.every((c) => candidateHasNoLevelMutation(c))).toBe(true)
    const levelsBefore: Record<string, string | null> = { 'CLUE-001': '线索' }
    const out = applyCandidates(levelsBefore, h.candidates)
    out.levels['CLUE-007'] = '确认' // 改返回的副本
    expect(levelsBefore['CLUE-007']).toBeUndefined() // 原快照不回写

    // 图谱：截断横幅 + 度数排序（张某度数 3 居首）
    const g = await graphApi.get('c1', {})
    expect(hasGraph(g)).toBe(true)
    expect(truncatedBanner(g)).toContain('丢弃 7 条边')
    const top = nodesByDegree(g)[0]
    expect(top.node.label).toBe('张某')
    expect(top.degree).toBe(3)
  })
})

describe('FE-T-008 全链路：跨案件查询（全有或全无）', () => {
  it('授权查询成功且带 source_case；含无权案件整体 403；历史分页', async () => {
    await login()

    const authz = authorizeSelection(['c1', 'c2'], ['c1', 'c2'])
    expect(canExecute(authz)).toBe(true)
    const sql = buildUnionTemplate(authz.authorized, 'obj_person')
    expect(sql).toContain('case_c1.obj_person')
    expect(sql).toContain('UNION ALL')

    const res = await crossCaseApi.query({ case_ids: ['c1', 'c2'], sql, reason: '并案排查重合主体' })
    expect(res.rows).toHaveLength(4)
    expect(hasSourceCaseColumn(res.rows)).toBe(true)

    // 无权案件：domain 先拦 + 后端 403 双保险
    const bad = authorizeSelection(['c1', 'cX'], ['c1', 'c2'])
    expect(bad.denied).toEqual(['cX'])
    expect(executeBlockReason(bad)).toContain('无权')
    await expect(
      crossCaseApi.query({ case_ids: ['c1', 'cX'], sql, reason: '探测' }),
    ).rejects.toMatchObject({ code: 'FORBIDDEN', httpStatus: 403 })

    const hist = await crossCaseApi.history(1, 20)
    expect(hist.total).toBeGreaterThanOrEqual(2) // 预置 1 条 + 本次成功查询 1 条
    expect(hist.page).toBe(1)

    // 模板表名白名单
    expect(() => buildUnionTemplate(['c1'], 'bad table; DROP')).toThrow('非法表名')
  })
})

describe('FE-T-008 全链路：案件包导出/校验/下载/导入', () => {
  it('导出任务 → 七步校验（chain 橙警不阻断）→ 下载 zip → 导入重号 409', async () => {
    await login()
    const file = new File(['PK'], 'nochain-package.zip', { type: 'application/zip' })

    const taskId = await packageApi.exportCase('c1')
    expect(taskId).toBe('task-exp-1')

    const v = await packageApi.verify(file)
    const steps = orderedSteps(v.steps)
    expect(steps).toHaveLength(7)
    expect(stepTone(steps.find((s) => s.key === 'chain')!.status)).toBe('warning')
    expect(chainWarning(v.chain_ok)).toContain('橙色告警')
    expect(sensitiveRedList(v.sensitive_files)).toContain('ontology/default/case_knowledge.json')
    expect(canImport(v)).toBe(true) // chain warn 不阻断

    const blob = await packageApi.download(taskId)
    expect(blob.type).toContain('zip')
    expect(downloadZipName('c1')).toBe('c1_package.zip')

    // 导入重号 → 409（导入只创建新案件）
    await expect(packageApi.importPackage(file, 'c1', '重复编号')).rejects.toMatchObject({
      code: 'CONFLICT', httpStatus: 409,
    })
  })
})

describe('FE-T-008 全链路：代码逃生舱（只生成文本）', () => {
  it('标识符校验 + 生成桩 + 统计四类保序', async () => {
    await login()
    expect(stubNameError('1bad')).not.toBe('')
    expect(stubNameError('my_fn')).toBe('')

    const r = await escapeHatchApi.generate({ ext_type: 'function', name: 'my_fn', description: '自定义只读函数' })
    expect(r.files[0].path).toContain('my_fn')
    expect(r.registration_points.length).toBeGreaterThan(0)

    const stats = await escapeHatchApi.stats()
    const bars = normalizeStats(stats.items)
    expect(bars.map((b) => b.ext_type)).toEqual(EXT_ORDER)
  })
})

describe('FE-T-008 全链路：系统设置（fail-closed + 留痕）', () => {
  it('非 admin：health 可读、管理面 403；admin：reason 必填、红线键不开放', async () => {
    const { auth, j } = await login()
    expect(canViewAdminSettings(auth.isAdmin)).toBe(false)

    const health = await settingsApi.health()
    expect(health.meta_ok).toBe(true)

    await expect(settingsApi.getQueue()).rejects.toMatchObject({ code: 'FORBIDDEN', httpStatus: 403 })

    // 切换管理员身份
    j.adminFlag.value = true
    const auth2 = useAuthStore()
    expect(await auth2.login('王检察官', 'adminpw')).toBe(true)
    expect(auth2.isAdmin).toBe(true)
    expect(canViewAdminSettings(true)).toBe(true)

    const q = await settingsApi.getQueue()
    expect(q.max_workers).toBe(2)

    // reason 空 → 400（留痕）；domain 闸门一致
    expect(settingsReasonError('')).toContain('审计留痕')
    expect(canSubmit(['max_workers'], '')).toBe(false)
    await expect(
      settingsApi.putQueue({ reason: '', values: { max_workers: 4 } }),
    ).rejects.toMatchObject({ code: 'VALIDATION', httpStatus: 400 })

    const saved = await settingsApi.putQueue({ reason: '季度任务量上调，扩容 Worker', values: { max_workers: 4 } })
    expect(saved.queue.max_workers).toBe(4)

    // 红线键静态锁定
    expect(LOCKED_REDLINES.map((r) => r.key)).toEqual(expect.arrayContaining(['llm_enabled', 'audit_immutable']))
  })
})

describe('FE-T-008 全链路：审计链（双维度/空链 warn）+ 归档留痕', () => {
  it('案件级时间线 → 线索级 ?clue_id= 过滤 → 空链自检 warn → 归档无 reason 400', async () => {
    const { transport } = await login()

    const page = await auditApi.timeline('c1', { page_size: 100 })
    expect(page.items.length).toBeGreaterThanOrEqual(2)

    const cluePage = await auditApi.timeline('c1', { clue_id: 'CLUE-007' })
    expect(cluePage.items).toHaveLength(1)
    expect(cluePage.items[0].event_id).toBe('evt-1')
    const auditCalls = transport.calls.filter((c) => c.method === 'GET' && c.path.includes('/audit'))
    expect(auditCalls[auditCalls.length - 1]?.path).toContain('clue_id=CLUE-007')

    // 空链（c2）自检：chain_ok=false + empty_chain（FE-T-015 warn 语义）
    const v2 = await auditApi.verify('c2')
    expect(v2.chain_ok).toBe(false)
    expect(v2.empty_chain).toBe(true)
    const v1 = await auditApi.verify('c1')
    expect(v1.chain_ok).toBe(true)

    // 归档：reason 必填留痕
    await expect(casesApi.archive('c2', '')).rejects.toMatchObject({ code: 'VALIDATION', httpStatus: 400 })
    const task = await casesApi.archive('c2', '案件已结案，封存归档')
    expect(task.task_type).toBe('ARCHIVE')
    expect(task.status).toBe('PENDING')
  })
})
