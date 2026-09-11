import { http, HttpResponse } from 'msw'

// MSW 兜底（契约先行，决策 14）：VITE_USE_MSW=1 时启用，供无后端演示与访问层联调前开发。
// 禁止 Mock 红线：权限判定/溯源 URI 渲染/状态机门禁/审计链空链语义/交叉升格——本文件
// 只产出数据（与 server/app/routers 信封同构），红线判定全部在 src/domain 纯函数。

const ok = <T,>(data: T, status = 200) =>
  HttpResponse.json({ ok: true, data, data_version: 1 }, { status })
const fail = (code: string, message: string, status: number) =>
  HttpResponse.json({ ok: false, error: { code, message } }, { status })

// ---------- 演示数据（内存态；处置动作会原地改状态并追加审计事件） ----------

interface MockClue {
  clue_id: string
  title: string
  skill_id: string
  jian_types: string[]
  level: string
  dimension: string[]
  priority_rank: number
  priority_score: number
  source_row_count: number
  merged_from: string[]
  status: string
  note: string
  operator: string
  updated_at: string
  status_source: 'state' | 'artifact'
  source_rows: Array<{
    row_uri: string
    source: string
    occurred_at: string
    fields: Array<{ name: string; value: string; hit?: boolean; mask?: 'phone' | 'idcard' | 'text'; policy?: 'visible' | 'masked' | 'denied'; denied_hint?: string }>
  }>
  evidence: Array<{ id: string; kind: 'fact' | 'inference' | 'pending'; text: string; room?: string; source_rows?: Array<{ row_uri: string; source?: string }> }>
  suppressed_log: unknown[]
}

const clues: Record<string, MockClue[]> = {
  c1: [
    {
      clue_id: 'CLUE-001',
      title: '蓝海贸易与张某之间资金回流闭环',
      skill_id: 'R6_time_window_collision',
      jian_types: ['资金', '通讯', '时间'],
      level: '可立案依据候选',
      dimension: ['资金', '通讯', '时间'],
      priority_rank: 1,
      priority_score: 92,
      source_row_count: 4,
      merged_from: ['CLUE-001A', 'CLUE-001B'],
      status: '已固证',
      note: '三通道独立命中，时间窗资金回流与通讯记录互证',
      operator: '王检察官',
      updated_at: '2026-09-08 14:22:10',
      status_source: 'state',
      suppressed_log: [{ seq: 90, reason: '内间决策记录，正兵及以下不可见' }],
      source_rows: [
        {
          row_uri: 'obj_transaction:bank_flow:default:row:8f3a21',
          source: '银行流水',
          occurred_at: '2026-08-21 10:14:00',
          fields: [
            { name: '交易时间', value: '2026-08-21 10:14:22', hit: true },
            { name: '付款方', value: '蓝海贸易有限公司', hit: true },
            { name: '收款方', value: '张某（6222****8843）' },
            { name: '金额', value: '¥ 480,000.00', hit: true },
            { name: '对方手机号', value: '13812345678', mask: 'phone', policy: 'masked' },
            { name: '涉密备注', value: '', policy: 'denied', denied_hint: '需 system 级授权' },
          ],
        },
        {
          row_uri: 'obj_call:call_record:default:row:c77d10',
          source: '通话记录',
          occurred_at: '2026-08-21 10:16:40',
          fields: [
            { name: '通话时间', value: '2026-08-21 10:16:40', hit: true },
            { name: '主叫', value: '张某 138****5678' },
            { name: '被叫', value: '李某 139****1122' },
            { name: '时长', value: '00:04:31' },
          ],
        },
        {
          row_uri: 'obj_transaction:bank_flow:default:row:8f4b07',
          source: '银行流水',
          occurred_at: '2026-08-21 15:02:09',
          fields: [
            { name: '交易时间', value: '2026-08-21 15:02:09', hit: true },
            { name: '付款方', value: '张某关联账户' },
            { name: '收款方', value: '蓝海贸易有限公司', hit: true },
            { name: '金额', value: '¥ 478,500.00', hit: true },
          ],
        },
      ],
      evidence: [
        {
          id: 'f1',
          kind: 'fact',
          text: '2026-08-21 10:14 蓝海贸易向张某账户转出 48 万元，同日 15:02 张某关联账户回转 47.85 万元，差额 1500 元。',
          source_rows: [
            { row_uri: 'obj_transaction:bank_flow:default:row:8f3a21', source: '银行流水' },
            { row_uri: 'obj_transaction:bank_flow:default:row:8f4b07', source: '银行流水' },
          ],
        },
        {
          id: 'f2',
          kind: 'fact',
          text: '转账后 2 分钟内张某与李某通话 4 分 31 秒，基站位于同一商圈。',
          source_rows: [{ row_uri: 'obj_call:call_record:default:row:c77d10', source: '通话记录' }],
        },
        {
          id: 'i1',
          kind: 'inference',
          room: '资金',
          text: '同日双向转账且金额近似，推断为资金回流（过桥费 1500 元）而非真实贸易结算。',
          source_rows: [
            { row_uri: 'obj_transaction:bank_flow:default:row:8f3a21' },
            { row_uri: 'obj_transaction:bank_flow:default:row:8f4b07' },
          ],
        },
        {
          id: 'i2',
          kind: 'inference',
          room: '通讯',
          text: '转账后短时通话推断为资金操作确认联络。',
          source_rows: [{ row_uri: 'obj_call:call_record:default:row:c77d10' }],
        },
        {
          // 红线 FE-T-004 演示数据：推断缺 source_rows → 前端拒绝渲染并计数
          id: 'i3-bad',
          kind: 'inference',
          room: '关系',
          text: '（此条推断在产物中缺少溯源行，前端必须拒绝渲染）',
        },
        {
          id: 'p1',
          kind: 'pending',
          text: '张某关联账户实际控制人待核实（账户开户资料与流水对手方交叉）。',
        },
      ],
    },
    {
      clue_id: 'CLUE-002',
      title: '李某深夜通话集中于案前 3 日',
      skill_id: 'R3_night_call_cluster',
      jian_types: ['通讯', '行为'],
      level: '线索',
      dimension: ['通讯', '行为'],
      priority_rank: 2,
      priority_score: 74,
      source_row_count: 2,
      merged_from: [],
      status: '查证中',
      note: '双通道命中：通讯记录 + 行为轨迹',
      operator: '王检察官',
      updated_at: '2026-08-28 11:05:33',
      status_source: 'state',
      suppressed_log: [],
      source_rows: [
        {
          row_uri: 'obj_call:call_record:default:row:91aa02',
          source: '通话记录',
          occurred_at: '2026-08-24 23:47:00',
          fields: [
            { name: '通话时间', value: '2026-08-24 23:47:12', hit: true },
            { name: '主叫', value: '李某 139****1122' },
            { name: '时长', value: '00:12:08' },
          ],
        },
      ],
      evidence: [
        {
          id: 'f1',
          kind: 'fact',
          text: '8 月 22-24 日（涉案转账前 3 日）李某 23 点后通话共 17 次，日均 5.7 次，显著高于基线 0.8 次。',
          source_rows: [{ row_uri: 'obj_call:call_record:default:row:91aa02', source: '通话记录' }],
        },
        {
          id: 'i1',
          kind: 'inference',
          room: '行为',
          text: '深夜通话密度突增推断为作案前联络协调，需结合通话对象身份进一步固证。',
          source_rows: [{ row_uri: 'obj_call:call_record:default:row:91aa02' }],
        },
        { id: 'p1', kind: 'pending', text: '深夜通话对端号码实名信息待调取。' },
      ],
    },
    {
      clue_id: 'CLUE-003',
      title: '王某名下账户单笔异常入账',
      skill_id: 'R1_large_amount',
      jian_types: ['资金'],
      level: '观察', // 红线 FE-T-006：单源候选恒「观察」
      dimension: ['资金'],
      priority_rank: 3,
      priority_score: 58,
      source_row_count: 1,
      merged_from: [],
      status: '待查',
      note: '仅资金单通道命中，交叉等级=观察，不进入立案候选',
      operator: '',
      updated_at: '2026-09-08 09:40:00',
      status_source: 'artifact',
      suppressed_log: [],
      source_rows: [
        {
          row_uri: 'obj_transaction:bank_flow:default:row:7c09e5',
          source: '银行流水',
          occurred_at: '2026-08-19 09:12:00',
          fields: [
            { name: '交易时间', value: '2026-08-19 09:12:00', hit: true },
            { name: '收款方', value: '王某', hit: true },
            { name: '金额', value: '¥ 260,000.00' },
          ],
        },
      ],
      evidence: [
        {
          id: 'f1',
          kind: 'fact',
          text: '2026-08-19 王某账户收到异地个人账户转入 26 万元，付款方与本案已知主体无关联记录。',
          source_rows: [{ row_uri: 'obj_transaction:bank_flow:default:row:7c09e5', source: '银行流水' }],
        },
        { id: 'p1', kind: 'pending', text: '付款方身份与资金性质待核实；单通道观察，需第二独立通道交叉。' },
      ],
    },
    {
      clue_id: 'CLUE-004',
      title: '临港仓储注册地址与实际经营地不符',
      skill_id: 'R5_address_mismatch',
      jian_types: ['关系', '行为'],
      level: '线索',
      dimension: ['关系', '行为'],
      priority_rank: 4,
      priority_score: 41,
      source_row_count: 2,
      merged_from: [],
      status: '已排除',
      note: '经查证：注册地为集中登记地址，实际经营地有租赁合同与社保缴纳记录',
      operator: '王检察官',
      updated_at: '2026-09-06 16:30:00',
      status_source: 'state',
      suppressed_log: [],
      source_rows: [],
      evidence: [
        { id: 'f1', kind: 'fact', text: '市场监管登记地址为临港某集中登记点，现场核查实际经营于自贸区内写字楼。' },
        { id: 'p1', kind: 'pending', text: '（已排除）登记地差异有合理解释，不指向虚假注册。' },
      ],
    },
    {
      clue_id: 'CLUE-005',
      title: '核心关系人周末轨迹重合于涉案仓储点',
      skill_id: 'R4_track_overlap',
      jian_types: ['行为', '关系', '时间'],
      level: '可立案依据候选',
      dimension: ['行为', '关系', '时间'],
      priority_rank: 5,
      priority_score: 63,
      source_row_count: 3,
      merged_from: [],
      status: '待查',
      note: '三通道命中但尚未人工查证',
      operator: '',
      updated_at: '2026-08-25 08:15:00',
      status_source: 'artifact',
      suppressed_log: [],
      source_rows: [],
      evidence: [
        { id: 'p1', kind: 'pending', text: '张/李/王三人 8 月内 4 个周末轨迹在临港仓储点 500 米范围内重合，待实地核验。' },
      ],
    },
    {
      clue_id: 'CLUE-006',
      title: '蓝海贸易虚开增值税发票闭环（已立案）',
      skill_id: 'R7_invoice_loop',
      jian_types: ['资金', '关系', '时间'],
      level: '可立案依据候选',
      dimension: ['资金', '关系', '时间'],
      priority_rank: 6,
      priority_score: 95,
      source_row_count: 6,
      merged_from: ['CLUE-006A'],
      status: '已立案',
      note: '资金回流 + 发票闭环 + 人员关联三通道互证，已立案侦办',
      operator: '王检察官',
      updated_at: '2026-09-07 17:20:00',
      status_source: 'state',
      suppressed_log: [],
      source_rows: [],
      evidence: [
        { id: 'f1', kind: 'fact', text: '立案决定书已于 2026-09-07 出具，案号 沪浦检刑立〔2026〕118 号。' },
      ],
    },
  ],
  c2: [],
}

// 审计事件（内存态；初始为历史链，处置动作追加）
interface MockAuditEvent {
  seq: number
  event_id: string
  case_id: string
  occurred_at: string
  operator: string
  action: 'disposal' | 'proposal' | 'parameter_set' | 'generic' | 'config'
  status_from: string | null
  status_to: string | null
  legal_basis?: string | null
  note?: string | null
  ontology_version: string
  rule_version: string
  function_version: string
  source_row_ids: string[]
  chain_source: 'state' | 'version'
  clue_id?: string
}

let auditSeq = 100
const auditEvents: MockAuditEvent[] = [
  {
    seq: 4, event_id: 'evt-0004', case_id: 'c1', occurred_at: '2026-09-06T16:30:00',
    operator: '王检察官', action: 'disposal', status_from: '待查', status_to: '已排除',
    note: '集中登记地址有合理解释，排除', ontology_version: '2.1.0', rule_version: '1.4.2',
    function_version: '1.8.0', source_row_ids: [], chain_source: 'state', clue_id: 'CLUE-004',
  },
  {
    seq: 3, event_id: 'evt-0003', case_id: 'c1', occurred_at: '2026-09-07T10:05:00',
    operator: '张助理', action: 'proposal', status_from: '待查', status_to: '查证中',
    note: '建议对李某深夜通话开展查证', ontology_version: '2.1.0', rule_version: '1.4.2',
    function_version: '1.8.0', source_row_ids: ['obj_call:call_record:default:row:91aa02'],
    chain_source: 'version', clue_id: 'CLUE-002',
  },
  {
    seq: 2, event_id: 'evt-0002', case_id: 'c1', occurred_at: '2026-09-08T09:12:00',
    operator: '王检察官', action: 'disposal', status_from: '查证中', status_to: '已固证',
    note: '资金回流闭环三通道互证，固证', legal_basis: null,
    ontology_version: '2.1.0', rule_version: '1.4.2', function_version: '1.8.0',
    source_row_ids: ['obj_transaction:bank_flow:default:row:8f3a21', 'obj_transaction:bank_flow:default:row:8f4b07'],
    chain_source: 'state', clue_id: 'CLUE-001',
  },
  {
    seq: 1, event_id: 'evt-0001', case_id: 'c1', occurred_at: '2026-09-05T14:00:00',
    operator: 'system', action: 'parameter_set', status_from: null, status_to: null,
    note: 'R6 时间窗阈值 60min→30min（参数调整留痕）',
    ontology_version: '2.1.0', rule_version: '1.4.1', function_version: '1.7.3',
    source_row_ids: [], chain_source: 'version',
  },
]

const ACTION_TARGET: Record<string, string> = {
  verify: '查证中',
  reset: '待查',
  exclude: '已排除',
  confirm: '已固证',
  file: '已立案',
}

function nowIso(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// ---------- handlers ----------

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
      is_admin: isMockAdmin(),
    })
  }),

  http.post('*/api/v1/auth/logout', () => ok({ revoked: true })),

  http.get('*/api/v1/auth/me', () =>
    ok({ operator: '王检察官', role: 'human', clearance: 4, tenant_id: 't1', is_admin: isMockAdmin() }),
  ),

  // 降级演示开关（仅 mock 管道）：sessionStorage 'sunzi.mock.degraded'='1' 时返回 degraded。
  // 红线判定（写禁用/原因文案）在 domain/clue.ts fileGate/degradeReason，不在此实现。
  http.get('*/api/v1/health', () => {
    const degraded =
      typeof sessionStorage !== 'undefined' && sessionStorage.getItem('sunzi.mock.degraded') === '1'
    return ok({
      status: degraded ? 'degraded' : 'ok',
      service: 'sunzi-web',
      version: 'msw-mock',
      meta: degraded ? 'metadata layer degraded (mock)' : 'ok',
    })
  }),

  http.get('*/api/v1/cases', () => ok([...mvp5Cases])),

  // MVP-5 建案：重复 409（幂等键由前端访问层携带）
  http.post('*/api/v1/cases', async ({ request }) => {
    const body = (await request.json()) as { case_id?: string; name?: string; pack_id?: string }
    const id = String(body.case_id ?? '').trim()
    if (!id || !/^[A-Za-z0-9_-]+$/.test(id) || id.length > 64) {
      return fail('VALIDATION', '案件编号非法（1–64，字母/数字/下划线/中划线）', 400)
    }
    if (!body.name?.trim()) return fail('VALIDATION', '案件名称不能为空', 400)
    if (mvp5Cases.some((c) => c.id === id)) {
      return fail('CONFLICT', `案件编号已存在：${id}`, 409)
    }
    const c = {
      id,
      tenant_id: 't1',
      name: body.name.trim(),
      status: '待建案',
      pack_id: body.pack_id ?? 'default',
      pack_snapshot_at: '',
      created_at: '2026-09-09T12:00:00',
      created_by: '王检察官',
    }
    mvp5Cases.push(c)
    return ok(c)
  }),

  // 案件门户汇总（A3 并发调用；c2 审计链未建立 → chain_ok=false 演示链告警）
  http.get('*/api/v1/cases/:cid/summary', ({ params }) => {
    const cid = String(params.cid)
    if (!mvp5Cases.some((c) => c.id === cid)) return fail('NOT_FOUND', '案件不存在', 404)
    const chainOk = cid !== 'c2'
    return ok({
      case: mvp5Cases.find((c) => c.id === cid),
      data_version: 1,
      todos:
        cid === 'c1'
          ? { clues_pending: 2, review_pending: 1, anomalies_pending: 1 }
          : { clues_pending: 0, review_pending: 0, anomalies_pending: 0 },
      health: { chain_ok: chainOk, degraded: false, diagnostics_warn: chainOk ? 0 : 1 },
      recent_tasks: [],
    })
  }),

  // 归档：clearance≥2（mock 会话 clearance=4）→ 返 ARCHIVE 任务走 SSE
  http.post('*/api/v1/cases/:cid/archive', async ({ params, request }) => {
    const cid = String(params.cid)
    const c = mvp5Cases.find((x) => x.id === cid)
    if (!c) return fail('NOT_FOUND', '案件不存在', 404)
    const body = (await request.json().catch(() => ({}))) as { reason?: string }
    if (!body.reason?.trim()) return fail('VALIDATION', '归档必须填写原因（审计留痕）', 400)
    if (c.status === '已封存') return fail('CONFLICT', '案件已封存，不可重复归档', 409)
    const t = makeTask(cid, 'ARCHIVE', { reason: body.reason })
    return ok(t)
  }),

  // 仪表盘：c1 有诊断（含 1 warning 降级演示）；c2 零记录（warn 语义，非「一切正常」）
  http.get('*/api/v1/cases/:cid/dashboard', ({ params }) => {
    if (params.cid === 'c2') {
      return ok({
        case_id: 'c2',
        health: { available: false, status: 'cold', 诊断总数: 0, 计数: { critical: 0, warning: 0, info: 0 }, 说明: '案件尚未运行 BUILD/RESCAN：无诊断留痕' },
        diagnostics: { total: 0, by_kind: {}, by_severity: { info: 0, warning: 0, critical: 0 } },
        coverage: { declared: [], empirical: [] },
        todo: { disposal: { available: false, total: 0, by_status: { 待查: 0, 查证中: 0, 已排除: 0, 已固证: 0, 已立案: 0 }, source: 'state' }, review: { available: false, pending: 0, note: '无待审建议' } },
      })
    }
    return ok({
      case_id: 'c1',
      health: {
        available: true,
        status: 'degraded',
        run_id: 'run-20260908-1000',
        诊断总数: 8,
        计数: { critical: 0, warning: 1, info: 7 },
        分类计数: { source_missing: 1, value_cast_failed: 0, rule_zero_hits: 0 },
        说明: '1 条 warning：obj_call 有 2 个声明维度无经验数据覆盖',
      },
      diagnostics: {
        total: 8,
        by_kind: { source_missing: 1, dirty_value: 2, rule_zero_hits: 0, coverage_gap: 2, info: 3 },
        by_severity: { info: 7, warning: 1, critical: 0 },
      },
      coverage: {
        declared: [{ reason: '维度声明覆盖', missing: ['基站编号', '快递地址'], severity: 'warning', created_at: '2026-09-08T10:00:00' }],
        empirical: [{ reason: '经验数据覆盖', missing: ['关系强度分'], severity: 'info', created_at: '2026-09-08T10:00:00' }],
      },
      todo: {
        disposal: {
          available: true,
          total: 6,
          by_status: { 待查: 2, 查证中: 1, 已排除: 1, 已固证: 1, 已立案: 1 },
          source: 'state',
        },
        review: { available: true, pending: 1, note: '张助理 1 条固证建议待审' },
      },
    })
  }),

  // 异常通道：级别恒「待核实」，不参与交叉升格
  http.get('*/api/v1/cases/:cid/anomalies', ({ params }) => {
    if (params.cid !== 'c1') return ok({ items: [], total: 0 })
    return ok({
      items: [
        {
          clue_id: 'ANOM-001',
          title: 'obj_call 通话时长字段空值率突增（2% → 18%）',
          severity: 'warning',
          level: '待核实',
          diagnostic_ids: ['diag-007'],
          dimension: ['通讯'],
          note: '异常通道线索不参与五间交叉升格',
        },
      ],
      total: 1,
    })
  }),

  // 线索列表（支持 status/level 筛选参数）
  http.get('*/api/v1/cases/:cid/clues', ({ params, request }) => {
    const cid = String(params.cid)
    const list = clues[cid] ?? []
    const url = new URL(request.url)
    const status = url.searchParams.get('status')
    const level = url.searchParams.get('level')
    const page = Number(url.searchParams.get('page') ?? '1')
    const pageSize = Number(url.searchParams.get('page_size') ?? '50')
    let items = list.map(({ source_rows: _sr, evidence: _ev, suppressed_log: _sl, ...rest }) => rest)
    if (status) items = items.filter((c) => c.status === status)
    if (level) items = items.filter((c) => c.level === level)
    const total = items.length
    const start = (page - 1) * pageSize
    return ok({
      items: items.slice(start, start + pageSize),
      total,
      page,
      page_size: pageSize,
      artifact_version: 42,
      available: cid === 'c1',
      note: cid === 'c2' ? '案件尚未运行分析' : undefined,
    })
  }),

  // 线索详情；CLUE-404 演示秩级过滤 404（不暴露存在性）
  http.get('*/api/v1/cases/:cid/clues/:clueId', ({ params }) => {
    const cid = String(params.cid)
    const clueId = String(params.clueId)
    if (clueId === 'CLUE-404') {
      return fail('NOT_FOUND', '不存在或无权访问', 404)
    }
    const clue = (clues[cid] ?? []).find((c) => c.clue_id === clueId)
    if (!clue) return fail('NOT_FOUND', '不存在或无权访问', 404)
    return ok({
      ...clue,
      audit_log: [],
      evidence: clue.evidence,
      source_row_details: clue.source_rows,
      decisions: [],
      artifact_version: 42,
    })
  }),

  // 处置动作：202 入队；file 缺 legal_basis → 400；file 机器角色 → 403（前端门禁 1 已拦，后端双保险）
  http.post('*/api/v1/cases/:cid/clues/:clueId/actions', async ({ params, request }) => {
    const body = (await request.json()) as { action: string; note?: string; reason?: string; legal_basis?: string }
    const cid = String(params.cid)
    const clue = (clues[cid] ?? []).find((c) => c.clue_id === String(params.clueId))
    if (!clue) return fail('NOT_FOUND', '不存在或无权访问', 404)

    if (body.action === 'file' && !body.legal_basis?.trim()) {
      return fail('VALIDATION', '立案必须填写法定依据/案号', 400)
    }
    if (body.action === 'exclude' && !body.reason?.trim() && !body.note?.trim()) {
      return fail('VALIDATION', '排除必须填写理由', 400)
    }
    const target = ACTION_TARGET[body.action]
    if (!target) return fail('VALIDATION', `未知动作：${body.action}`, 400)

    // 内存态迁移 + 审计追加（模拟 DISPOSE worker 完成后的效果）
    const from = clue.status
    clue.status = target
    clue.updated_at = nowIso()
    auditSeq += 1
    auditEvents.push({
      seq: auditSeq,
      event_id: `evt-${auditSeq}`,
      case_id: cid,
      occurred_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
      operator: '王检察官',
      action: 'disposal',
      status_from: from,
      status_to: target,
      legal_basis: body.action === 'file' ? body.legal_basis ?? null : null,
      note: body.reason || body.note || null,
      ontology_version: '2.1.0',
      rule_version: '1.4.2',
      function_version: '1.8.0',
      source_row_ids: clue.source_rows.map((r) => r.row_uri),
      chain_source: 'state',
      clue_id: clue.clue_id,
    })

    return ok(
      { task_id: `task-${Date.now()}`, status: 'queued', task_type: 'DISPOSE', clue_id: clue.clue_id },
      202,
    )
  }),

  // 审计链时间线
  http.get('*/api/v1/cases/:cid/audit', ({ params, request }) => {
    const cid = String(params.cid)
    const url = new URL(request.url)
    const action = url.searchParams.get('action')
    const operator = url.searchParams.get('operator')
    const clueId = url.searchParams.get('clue_id')
    const page = Number(url.searchParams.get('page') ?? '1')
    const pageSize = Number(url.searchParams.get('page_size') ?? '100')
    let items = auditEvents
      .filter((e) => e.case_id === cid)
      .map(({ clue_id: _c, ...rest }) => rest)
    if (action) items = items.filter((e) => e.action === action)
    if (operator) items = items.filter((e) => e.operator.includes(operator))
    if (clueId) {
      items = auditEvents
        .filter((e) => e.case_id === cid && e.clue_id === clueId)
        .map(({ clue_id: _c, ...rest }) => rest)
    }
    items = items.sort((a, b) => b.seq - a.seq)
    const total = items.length
    const start = (page - 1) * pageSize
    return ok({ items: items.slice(start, start + pageSize), total, page, page_size: pageSize })
  }),

  // 完整性自检：c2 空链 → chain_ok=false + empty_chain=true（红线 FE-T-015，API 层强制）
  http.post('*/api/v1/cases/:cid/audit/verify', ({ params }) => {
    const cid = String(params.cid)
    const count = auditEvents.filter((e) => e.case_id === cid).length
    if (count === 0) {
      return ok({
        chain_ok: false,
        expected_count: 0,
        actual_count: 0,
        broken_links: [],
        missing_fields: [],
        disposal_events: 0,
        disposal_activity: { persisted_non_pending: 0, actions_applied: 0 },
        cross_check: { disposal_events: 0, persisted_non_pending: 0, actions_applied: 0, consistent: true },
        empty_chain: true,
        chain_source: 'state',
      })
    }
    return ok({
      chain_ok: true,
      expected_count: count,
      actual_count: count,
      broken_links: [],
      missing_fields: [],
      disposal_events: auditEvents.filter((e) => e.case_id === cid && e.action === 'disposal').length,
      disposal_activity: { persisted_non_pending: 4, actions_applied: 4 },
      cross_check: { disposal_events: 4, persisted_non_pending: 4, actions_applied: 4, consistent: true },
      empty_chain: false,
      chain_source: 'state',
    })
  }),

  // 被抑制记录（聚合各线索 detail.suppressed_log）
  http.get('*/api/v1/cases/:cid/clues/suppressed', ({ params }) => {
    const cid = String(params.cid)
    const items = (clues[cid] ?? [])
      .filter((c) => c.suppressed_log.length > 0)
      .flatMap((c) => c.suppressed_log.map((log) => ({ clue_id: c.clue_id, ...(log as object) })))
    return ok({ items, total: items.length })
  }),

  // ---------- 实体裁决（W-021 review 三端点 mock） ----------

  http.get('*/api/v1/cases/:cid/review/queue', ({ params, request }) => {
    const cid = String(params.cid)
    const url = new URL(request.url)
    const page = Number(url.searchParams.get('page') ?? '1')
    const pageSize = Number(url.searchParams.get('page_size') ?? '20')
    const list = reviewCandidates[cid] ?? []
    const start = (page - 1) * pageSize
    const items = list.slice(start, start + pageSize).map((c) => ({
      entity_id: c.entity_id,
      canonical_name: c.canonical_name,
      variants: c.variants,
      evidence: c.evidence,
      confidence: c.confidence,
      needs_review: c.needs_review,
      merge_reason: c.merge_reason,
    }))
    return ok({ items, total: list.length, page, page_size: pageSize })
  }),

  http.get('*/api/v1/cases/:cid/review/history', ({ params }) => {
    const cid = String(params.cid)
    return ok({ items: reviewHistory[cid] ?? [], total: (reviewHistory[cid] ?? []).length })
  }),

  http.get('*/api/v1/cases/:cid/review/:rid/evidence', ({ params }) => {
    const cid = String(params.cid)
    const rid = String(params.rid)
    const c = (reviewCandidates[cid] ?? []).find((x) => x.entity_id === rid)
    if (!c) return fail('NOT_FOUND', '候选不存在或案件尚未 BUILD', 404)
    return ok({
      candidate_id: c.entity_id,
      canonical_name: c.canonical_name,
      variants: c.variants,
      confidence: c.confidence,
      merge_reason: c.merge_reason,
      evidence: c.evidence,
      // mock 用 attributeRows 直传平坦行（adaptEvidence 直传不经适配器）
      attributeRows: c.attributes,
      llm_inferences: c.llm_inferences ?? [],
    })
  }),

  http.post('*/api/v1/cases/:cid/review/:rid/decision', async ({ params, request }) => {
    const cid = String(params.cid)
    const rid = String(params.rid)
    const body = (await request.json()) as { action: string; reason?: string }
    const c = (reviewCandidates[cid] ?? []).find((x) => x.entity_id === rid)
    if (!c) return fail('NOT_FOUND', '候选不存在或案件尚未 BUILD', 404)
    if (body.action !== 'review_merge' && body.action !== 'review_reject') {
      return fail('VALIDATION', `未知裁决动作：${body.action}`, 400)
    }
    // 驳回理由必填（后端 400 双保险；前端 reasonError 先拦）
    if (body.action === 'review_reject' && !body.reason?.trim()) {
      return fail('VALIDATION', '驳回（确认为不同人）必须填写裁决理由', 400)
    }
    const verdict = body.action === 'review_merge' ? 'merge' : 'reject'
    reviewCandidates[cid] = (reviewCandidates[cid] ?? []).filter((x) => x.entity_id !== rid)
    const record = {
      candidate_id: c.entity_id,
      canonical_name: c.canonical_name,
      action: verdict as 'merge' | 'reject',
      confidence: c.confidence,
      operator: '王检察官',
      reason: body.reason?.trim() || undefined,
      occurred_at: nowIso(),
    }
    reviewHistory[cid] = [record, ...(reviewHistory[cid] ?? [])]

    auditSeq += 1
    auditEvents.push({
      seq: auditSeq,
      event_id: `evt-${auditSeq}`,
      case_id: cid,
      occurred_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
      operator: '王检察官',
      action: 'generic',
      status_from: null,
      status_to: null,
      note: `实体裁决：${c.canonical_name} → ${verdict === 'merge' ? '确认为同一人' : '确认为不同人'}${body.reason ? `（${body.reason}）` : ''}`,
      ontology_version: '2.1.0',
      rule_version: '1.4.2',
      function_version: '1.8.0',
      source_row_ids: c.evidence.common_source_rows ?? [],
      chain_source: 'state',
    })

    return ok({ task_id: `task-${Date.now()}`, action: body.action, candidate_id: rid }, 200)
  }),

  // ---------- 数据画像（FE-P-007；后端已实现 profiles_view.assemble_profiles） ----------
  // 后端返回六层报告结构；c2 未 BUILD → available:false（不 404，与后端一致）

  http.get('*/api/v1/cases/:cid/profiles', ({ params }) => {
    if (String(params.cid) !== 'c1') {
      return ok({ available: false, note: '尚未接入数据源（语义层未构建，先导入数据并 BUILD）' })
    }
    return ok(profileMock)
  }),

  // ---------- MVP-3 任务中心（tasks，服务端分页 + 状态/类型过滤）----------
  http.get('*/api/v1/tasks', ({ request }) => {
    const url = new URL(request.url)
    const cid = url.searchParams.get('case_id') ?? ''
    const status = url.searchParams.get('status')
    const taskType = url.searchParams.get('task_type')
    const page = Math.max(1, parseInt(url.searchParams.get('page') ?? '1', 10) || 1)
    const pageSize = Math.min(200, Math.max(1, parseInt(url.searchParams.get('page_size') ?? '50', 10) || 50))

    const all = [...(mockTasks[cid] ?? [])]
    const cnt = (s: string) => all.filter((t) => t.status === s).length
    const stats = {
      total: all.length,
      pending: cnt('PENDING'), running: cnt('RUNNING'),
      active: cnt('PENDING') + cnt('RUNNING'),
      succeeded: cnt('SUCCEEDED'), failed: cnt('FAILED'), cancelled: cnt('CANCELLED'),
    }

    let list = all
    if (status) {
      const statuses = new Set<string>()
      for (const part of status.split(',')) {
        const tok = part.trim()
        if (tok === 'active') { statuses.add('PENDING'); statuses.add('RUNNING') }
        else if (['PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(tok)) statuses.add(tok)
      }
      if (statuses.size) list = list.filter((t) => statuses.has(t.status))
    }
    if (taskType) list = list.filter((t) => t.task_type === taskType)
    list.sort((a, b) => (b.updated_at || b.created_at).localeCompare(a.updated_at || a.created_at))

    const total = list.length
    const start = (page - 1) * pageSize
    const items = list.slice(start, start + pageSize).map((t) => ({ ...t }))
    return ok({ items, total, page, page_size: pageSize, stats })
  }),

  http.post('*/api/v1/cases/:cid/tasks', async ({ params, request }) => {
    const body = (await request.json()) as { task_type?: string; params?: Record<string, unknown> }
    const cid = String(params.cid)
    const t = makeTask(cid, body.task_type ?? 'BUILD', body.params ?? {})
    return ok(t)
  }),

  http.get('*/api/v1/tasks/:tid', ({ params }) => {
    const t = findTask(String(params.tid))
    if (!t) return fail('NOT_FOUND', `任务不存在：${params.tid}`, 404)
    return ok({ ...t })
  }),

  http.post('*/api/v1/tasks/:tid/cancel', async ({ params }) => {
    const t = findTask(String(params.tid))
    if (!t) return fail('NOT_FOUND', `任务不存在：${params.tid}`, 404)
    if (t.status !== 'PENDING') return fail('CONFLICT', `任务状态为 ${t.status}，仅排队中可取消`, 409)
    t.status = 'CANCELLED'
    t.finished_at = '2026-09-09T11:00:00'
    return ok({ task_id: t.id, status: 'CANCELLED' })
  }),

  // 重试终态任务：新任务（新 id / PENDING / 空幂等键），旧任务保留
  http.post('*/api/v1/tasks/:tid/retry', ({ params }) => {
    const t = findTask(String(params.tid))
    if (!t) return fail('NOT_FOUND', `任务不存在：${params.tid}`, 404)
    if (t.status !== 'FAILED' && t.status !== 'CANCELLED') {
      return fail('CONFLICT', `任务状态为 ${t.status}，仅失败或已取消的任务可重试`, 409)
    }
    taskSeq += 1
    const nt: MockTask = {
      ...t,
      id: `task-r-${taskSeq}`,
      status: 'PENDING',
      progress_pct: 0, progress_stage: '', progress_label: '', progress_detail: '',
      retry_count: 0, idem_key: '',
      created_at: '2026-09-10T09:00:00', updated_at: '2026-09-10T09:00:00',
      started_at: '', finished_at: '', error_code: '', error_message: '',
      created_by: '王检察官',
    }
    mockTasks[t.case_id] = mockTasks[t.case_id] ?? []
    mockTasks[t.case_id].push(nt)
    return ok({ ...nt })
  }),

  // SSE：脚本化进度流（progress → terminal），终态帧后关闭
  http.get('*/api/v1/tasks/:tid/events', ({ params }) => {
    const tid = String(params.tid)
    const t = findTask(tid)
    if (!t) return fail('NOT_FOUND', `任务不存在：${tid}`, 404)
    const encoder = new TextEncoder()
    const frame = (seq: number, event: string, data: unknown) =>
      `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`
    const stream = new ReadableStream({
      start(controller) {
        let seq = 0
        const send = (event: string, data: unknown) => {
          seq += 1
          controller.enqueue(encoder.encode(frame(seq, event, data)))
        }
        const close = () => {
          try { controller.close() } catch { /* noop */ }
        }
        if (t.status === 'SUCCEEDED' || t.status === 'FAILED' || t.status === 'CANCELLED') {
          send('terminal', t)
          close()
          return
        }
        // 进行中：脚本推进
        const isImport = t.task_type === 'IMPORT'
        const isDiagnose = t.task_type === 'DIAGNOSE'
        const stages: Array<[number, string, string, string]> = isImport
          ? [
              [20, 'parse', '解析数据', t.params.filename ? String(t.params.filename) : '读取上传件'],
              [60, 'cold', '写冷层 parquet', String(t.params.target_table ?? '银行流水')],
              [100, 'done', '导入完成', `${t.params.target_table ?? '银行流水'} 1203 行 → BUILD 已入队`],
            ]
          : isDiagnose
            ? [
                [20, 'build_stats', '补落构建期留痕', '脏值/缺列/隔离/清洗剔除/去重冲突'],
                [50, 'quality_gate', '质量门扫描', '合规/敏感/新鲜度/单位'],
                [100, 'done', '诊断完成', '诊断 8 条（healthy）'],
              ]
            : [
                [30, 'prepare', '准备构建', '目标 v3（基线 v2）'],
                [70, 'align', '实体对齐', '已对齐 243 / 待确认 6'],
                [100, 'done', '构建完成', '语义层 v3 已生成'],
              ]
        stages.forEach(([pct, stage, label, detail], i) => {
          setTimeout(() => {
            t.status = 'RUNNING'
            t.progress_pct = pct
            t.progress_stage = stage
            t.progress_label = label
            t.progress_detail = detail
            send('progress', { ...t })
            if (i === stages.length - 1) {
              setTimeout(() => {
                t.status = 'SUCCEEDED'
                t.finished_at = '2026-09-09T11:05:00'
                send('terminal', { ...t })
                close()
              }, 700)
            }
          }, 800 * (i + 1))
        })
      },
    })
    return new HttpResponse(stream, {
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
    })
  }),

  // ---------- MVP-3 数据接入（sources）----------
  http.post('*/api/v1/cases/:cid/sources/upload', async ({ params, request }) => {
    const cid = String(params.cid)
    let filename = 'upload.csv'
    try {
      const fd = await request.formData()
      const f = fd.get('file')
      if (f && f instanceof File) filename = f.name
    } catch { /* noop */ }
    const ext = (filename.split('.').pop() ?? '').toLowerCase()
    const supported = ['csv', 'xlsx', 'xls', 'parquet', 'json', 'sqlite', 'db']
    if (!supported.includes(ext)) {
      return fail('VALIDATION', `不支持的文件类型：${filename}（支持 CSV/Excel/Parquet/JSON/SQLite）`, 400)
    }
    const uid = `up_${Math.random().toString(16).slice(2, 12)}`
    const src = {
      upload_id: uid, filename, format: ext === 'xls' ? 'excel' : ext,
      fingerprint: `fp_${filename}_sha256_${Math.random().toString(16).slice(2, 10)}_1203`,
      rows: 1203, status: 'staged',
      columns: bankSourceColumns(),
      declared_tables: bankDeclaredTablesDict(),
    }
    mockSources[cid] = mockSources[cid] ?? []
    mockSources[cid].push({ ...src, created_by: '王检察官', created_at: '2026-09-09 10:58:00' })
    return ok(src)
  }),

  http.get('*/api/v1/cases/:cid/sources', ({ params }) => {
    const items = mockSources[String(params.cid)] ?? []
    return ok({ items, total: items.length })
  }),

  http.post('*/api/v1/cases/:cid/sources/:uid/analyze', ({ params }) => {
    const cid = String(params.cid)
    const src = (mockSources[cid] ?? []).find((s) => s.upload_id === params.uid)
    if (!src) return fail('NOT_FOUND', `上传件不存在：${params.uid}`, 404)
    return ok({
      upload_id: String(params.uid),
      filename: src.filename,
      format: src.format,
      row_count: src.rows,
      sha256: src.fingerprint,
      fingerprint: src.fingerprint,
      columns: bankSourceColumns(),
      declared_tables: bankDeclaredTablesView(),
      suggestion: bankSuggestion(),
      element_hints: [],
    })
  }),

  http.put('*/api/v1/cases/:cid/sources/:uid', async ({ params, request }) => {
    const cid = String(params.cid)
    const src = (mockSources[cid] ?? []).find((s) => s.upload_id === params.uid)
    if (!src) return fail('NOT_FOUND', `上传件不存在：${params.uid}`, 404)
    const body = (await request.json()) as { target_table?: string }
    if (body.target_table && !(body.target_table in bankDeclaredTablesDict())) {
      return fail('VALIDATION', `目标源表未在 bindings 声明`, 400)
    }
    return ok(src)
  }),

  http.post('*/api/v1/cases/:cid/sources/:uid/import', async ({ params, request }) => {
    const cid = String(params.cid)
    const uid = String(params.uid)
    const src = (mockSources[cid] ?? []).find((s) => s.upload_id === uid)
    if (!src) return fail('NOT_FOUND', `上传件不存在：${uid}`, 404)
    // 指纹幂等：同源已 queued/imported → 409
    const dup = (mockTasks[cid] ?? []).find(
      (t) => t.task_type === 'IMPORT' && (t.params.upload_id as string) !== uid && t.status === 'RUNNING',
    )
    if (dup) {
      return fail('CONFLICT', `相同数据源已在导入（upload_id=${dup.params.upload_id}，指纹=${src.fingerprint}，状态=queued）`, 409)
    }
    const body = (await request.json()) as { target_table?: string; column_map?: Record<string, string> }
    const task = makeTask(cid, 'IMPORT', {
      upload_id: uid, target_table: body.target_table ?? '银行流水',
      column_map: body.column_map ?? {}, filename: src.filename,
    })
    task.status = 'RUNNING'
    task.progress_pct = 0
    src.status = 'queued'
    return ok(task)
  }),

  // ---------- MVP-3 接入建议（de-recommendations）----------
  http.get('*/api/v1/cases/:cid/de-recommendations', ({ params }) => {
    if (String(params.cid) !== 'c1') return ok({ items: [], total: 0 })
    return ok({ items: mockDeRecos, total: mockDeRecos.length })
  }),

  http.post('*/api/v1/cases/:cid/de-recommendations', async ({ params }) => {
    const cid = String(params.cid)
    const t = makeTask(cid, 'DE_RECOMMEND', {})
    return HttpResponse.json({ ok: true, data: t, data_version: 1 }, { status: 202 })
  }),

  http.post('*/api/v1/cases/:cid/de-recommendations/:rid/decide', async ({ params, request }) => {
    const rid = String(params.rid)
    const reco = mockDeRecos.find((r) => r.rid === rid)
    if (!reco) return fail('NOT_FOUND', `推荐不存在：${rid}`, 404)
    const body = (await request.json()) as { decision?: string }
    if (body.decision !== 'adopt' && body.decision !== 'reject') {
      return fail('VALIDATION', 'decision 非法：需 adopt|reject', 400)
    }
    reco.status = body.decision === 'adopt' ? '采纳' : '驳回'
    reco.decided_by = '王检察官'
    reco.decided_at = '2026-09-09 11:10:00'
    const t = makeTask(String(params.cid), 'DE_RECO_DECIDE', { rid, decision: body.decision })
    return HttpResponse.json({ ok: true, data: t, data_version: 1 }, { status: 202 })
  }),

  // ---------- MVP-4 配置面（规则工坊/模型/权限/知识/ETL/质量/隔离区）----------
  // 只 mock 数据塑形；权限门禁/红线判定全部在 src/domain 纯函数与后端，不在此实现。

  // 规则工坊：列表 + 函数目录
  http.get('*/api/v1/cases/:cid/rules', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ rules: mockRules, function_catalog: mockFunctionCatalog, pack: 'default' })
  }),

  // 规则编辑：仅 rule_text/params/enabled；结构字段 400；危险项理由缺失 400
  http.put('*/api/v1/cases/:cid/rules/:rid', async ({ params, request }) => {
    const cid = String(params.cid)
    const rid = String(params.rid)
    const rule = mockRules.find((r) => r.id === rid)
    if (!rule) return fail('NOT_FOUND', `规则不存在：${rid}`, 404)
    const body = (await request.json()) as {
      rule_text?: string
      params?: Record<string, unknown>
      enabled?: boolean
      reason?: string
    }
    const structural = Object.keys(body).filter((k) => !['rule_text', 'params', 'enabled', 'reason'].includes(k))
    if (structural.length) return fail('VALIDATION', `结构字段只读不可改：${structural.join(',')}`, 400)
    if (body.rule_text !== undefined && body.rule_text.trim().length < 20) {
      return fail('VALIDATION', '判据须写明模式/反常理由/边界排除（至少 20 字）', 400)
    }
    const changed: string[] = []
    if (body.rule_text !== undefined && body.rule_text.trim() !== rule.rule_text.trim()) changed.push('rule_text')
    if (body.params !== undefined) changed.push('params')
    if (body.enabled !== undefined && Boolean(body.enabled) !== Boolean(rule.enabled)) changed.push('enabled')
    const dangerous = changed.includes('params') || changed.includes('enabled')
    if (dangerous && !body.reason?.trim()) {
      return fail('VALIDATION', '机器行为变更（阈值/启停）必须填写变更理由', 400)
    }
    if (body.rule_text !== undefined) rule.rule_text = body.rule_text
    if (body.params !== undefined) rule.params = { ...rule.params, ...body.params }
    if (body.enabled !== undefined) rule.enabled = body.enabled
    appendConfigAudit(cid, 'rule_edit', 'rules.json', body.reason, { rule_id: rid, changed })
    const rescan = dangerous ? makeTask(cid, 'RESCAN', { reason: `rule_edit:${rid}` }) : null
    return ok({ rule_id: rid, changed, rescan_task: rescan })
  }),

  // LLM 起草：默认通道关闭 → 503（产物永不落盘）
  http.post('*/api/v1/cases/:cid/rules/draft', () =>
    fail('LLM_DISABLED', '当前环境未启用 LLM 通道（离线内核）；判据草稿请人工撰写', 503),
  ),

  // 对象模型
  http.get('*/api/v1/cases/:cid/objects', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ objects: mockObjects, pack: 'default' })
  }),
  http.put('*/api/v1/cases/:cid/objects', async ({ params, request }) => {
    const body = (await request.json()) as { objects?: unknown[]; reason?: string }
    if (!Array.isArray(body.objects)) return fail('VALIDATION', 'objects 必须是数组', 400)
    appendConfigAudit(String(params.cid), 'model_objects_save', 'objects.json', body.reason, { count: body.objects.length })
    return ok({ saved: body.objects.length })
  }),
  http.get('*/api/v1/cases/:cid/links', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ links: mockLinks, pack: 'default' })
  }),
  http.put('*/api/v1/cases/:cid/links', async ({ params, request }) => {
    const body = (await request.json()) as { links?: unknown[]; reason?: string }
    if (!Array.isArray(body.links)) return fail('VALIDATION', 'links 必须是数组', 400)
    appendConfigAudit(String(params.cid), 'model_links_save', 'links.json', body.reason, { count: body.links.length })
    return ok({ saved: body.links.length })
  }),
  http.post('*/api/v1/cases/:cid/validate', () => ok({ valid: true, pack: 'default' })),

  // 权限与遮蔽
  http.get('*/api/v1/cases/:cid/policies', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ ...mockPolicies, pack: 'default' })
  }),
  http.put('*/api/v1/cases/:cid/policies', async ({ params, request }) => {
    const body = (await request.json()) as Record<string, unknown>
    appendConfigAudit(String(params.cid), 'policies_save', 'policies.json', body.reason as string | undefined, {
      objects: (body.object_policies as unknown[])?.length ?? 0,
      links: (body.link_policies as unknown[])?.length ?? 0,
      properties: (body.property_policies as unknown[])?.length ?? 0,
    })
    return ok({ saved: true })
  }),
  http.get('*/api/v1/cases/:cid/views', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ views: mockViews, pack: 'default' })
  }),
  http.put('*/api/v1/cases/:cid/views', async ({ params, request }) => {
    const body = (await request.json()) as { views?: unknown[]; reason?: string }
    appendConfigAudit(String(params.cid), 'views_save', 'views.json', body.reason, { count: body.views?.length ?? 0 })
    return ok({ saved: body.views?.length ?? 0 })
  }),

  // 知识包
  http.get('*/api/v1/cases/:cid/knowledge', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ ...mockKnowledge, pack: 'default' })
  }),
  http.post('*/api/v1/cases/:cid/knowledge', async ({ params, request }) => {
    const body = (await request.json()) as { relation_assertions?: unknown[]; reason?: string }
    const n = body.relation_assertions?.length ?? 0
    mockKnowledge.relation_assertions.push(...(body.relation_assertions as typeof mockKnowledge.relation_assertions))
    appendConfigAudit(String(params.cid), 'knowledge_add', 'case_knowledge.json', body.reason, { added: n })
    return ok({ added: n, total: mockKnowledge.relation_assertions.length })
  }),
  http.put('*/api/v1/cases/:cid/knowledge', async ({ params, request }) => {
    const body = (await request.json()) as { relation_assertions?: unknown[]; subject_aliases?: Record<string, string[]>; reason?: string }
    if (body.relation_assertions) mockKnowledge.relation_assertions = body.relation_assertions as typeof mockKnowledge.relation_assertions
    if (body.subject_aliases) mockKnowledge.subject_aliases = body.subject_aliases
    appendConfigAudit(String(params.cid), 'knowledge_save', 'case_knowledge.json', body.reason, {
      assertions: mockKnowledge.relation_assertions.length,
    })
    return ok({ saved: true, assertions: mockKnowledge.relation_assertions.length })
  }),

  // 数据元
  http.get('*/api/v1/cases/:cid/data-elements', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok(mockDataElements)
  }),
  http.put('*/api/v1/cases/:cid/data-elements', async ({ params, request }) => {
    const body = (await request.json()) as { elements?: Record<string, unknown>; reason?: string }
    appendConfigAudit(String(params.cid), 'data_elements_edit', 'data_elements.json', body.reason, {
      elements: Object.keys(body.elements ?? {}).length,
    })
    return ok({ updated: true })
  }),

  // ETL 管道 + 映射预检 + 缺列降级
  http.get('*/api/v1/cases/:cid/etl-pipeline', ({ params }) => {
    if (String(params.cid) !== 'c1') return fail('NOT_FOUND', '案件快照不存在', 404)
    return ok({ sources: mockEtlSources })
  }),
  http.put('*/api/v1/cases/:cid/etl-pipeline', async ({ params, request }) => {
    const body = (await request.json()) as { sources?: unknown[]; reason?: string }
    appendConfigAudit(String(params.cid), 'etl_pipeline_edit', 'bindings.json', body.reason, {
      sources: body.sources?.length ?? 0,
    })
    return ok({ sources: mockEtlSources })
  }),
  http.post('*/api/v1/cases/:cid/etl-pipeline/validate', async ({ request }) => {
    const body = (await request.json()) as { target_table?: string; mapping?: Record<string, string> }
    const conflicts: Array<Record<string, unknown>> = []
    const knownProps = new Set<string>([
      'raw_name', 'amount', 'date', 'from_raw', 'to_raw', 'caller_raw', 'callee_raw',
      'person_raw', 'location', 'title', 'status', 'txn_id',
    ])
    const byTarget = new Map<string, string>()
    for (const [col, prop] of Object.entries(body.mapping ?? {})) {
      if (!knownProps.has(prop)) {
        conflicts.push({ type: 'unknown_prop', message: `目标属性 ${prop} 未在对象上声明`, source_col: col, target_prop: prop })
      }
      if (byTarget.has(prop)) {
        conflicts.push({ type: 'one_to_one', message: `源列 ${byTarget.get(prop)} 与 ${col} 同时映射到 ${prop}`, source_col: col, target_a: byTarget.get(prop), target_b: col })
      }
      byTarget.set(prop, col)
    }
    return ok({
      valid: conflicts.length === 0,
      conflicts,
      paths: [
        { key: 'A_split_source_sql', label: '在 source_sql 中把复合表达式拆成多个独立源列' },
        { key: 'B_degrade_column', label: '该列整列降级 NULL 并持续诊断' },
      ],
    })
  }),
  http.get('*/api/v1/cases/:cid/governance/missing-columns', ({ params }) => {
    if (String(params.cid) !== 'c1') return ok({ items: [], total_warnings: 0 })
    return ok({
      items: [
        { object: 'call', property: 'base_station', count: 47, kinds: ['source_column_missing'], samples: ['通话记录*批次 2026-08'] },
        { object: 'transaction', property: 'amount', count: 12, kinds: ['source_value_cast_failed'], samples: ['¥ 肆拾捌万元整', '480,000.00 元（手工录入）'] },
      ],
      total_warnings: 2,
    })
  }),

  // P4: ETL 清洗预演（返 affected_rows）
  http.post('*/api/v1/cases/:cid/etl-pipeline/preview', async ({ request }) => {
    const body = (await request.json()) as { source_col?: string; op_token?: string }
    const col = body.source_col ?? '金额'
    const op = body.op_token ?? 'strip_thousands'
    const before = col.includes('金额') ? ['48,000.00', '12,345.67', '9,876.50'] : ['abc123', 'def456']
    const after = op.startsWith('strip_thousands') ? before.map(v => v.replace(/,/g, '')) : before
    const samples = before.map((b, i) => ({ before: b, after: after[i], rejected: false }))
    return ok({
      op, source_col: col, samples,
      total_rows: 100, affected_rows: before.length,
    })
  }),

  // P4: ETL 处置草稿 CRUD（内存数组）
  http.get('*/api/v1/cases/:cid/etl-drafts', ({ request }) => {
    const url = new URL(request.url)
    const uid = url.searchParams.get('upload_id')
    const items = uid ? mockEtlDrafts.filter(d => d.upload_id === uid) : mockEtlDrafts
    return ok({ items, total: items.length })
  }),
  http.post('*/api/v1/cases/:cid/etl-drafts', async ({ request }) => {
    const body = (await request.json()) as { target_prop?: string; op_token?: string; op_class?: string; preview_affected_rows?: number; preview_samples?: unknown[] }
    const d = {
      draft_id: `draft_${Math.random().toString(36).slice(2, 14)}`,
      case_id: 'c1', upload_id: 'u1',
      target_object: 'transaction',
      target_prop: body.target_prop ?? '金额',
      op_token: body.op_token ?? 'strip_thousands',
      op_class: body.op_class ?? 'A',
      source: 'Step2',
      preview_affected_rows: body.preview_affected_rows ?? 0,
      preview_samples: body.preview_samples ?? [],
      status: '待复核', created_at: '2026-09-11 14:00:00',
      created_by: '王检察官', reviewed_by: '', reviewed_at: '', note: '',
    }
    mockEtlDrafts.unshift(d)
    return ok(d)
  }),
  http.post('*/api/v1/cases/:cid/etl-drafts/:did/confirm', ({ params }) => {
    const did = String(params.did)
    const d = mockEtlDrafts.find(x => x.draft_id === did)
    if (!d) return fail('NOT_FOUND', `草稿不存在：${did}`, 404)
    if (d.status !== '待复核') return fail('CONFLICT', '草稿状态非待复核', 409)
    d.status = '已确认'; d.reviewed_by = '王检察官'; d.reviewed_at = '2026-09-11 14:01:00'
    return ok(d)
  }),
  http.post('*/api/v1/cases/:cid/etl-drafts/:did/reject', ({ params }) => {
    const did = String(params.did)
    const d = mockEtlDrafts.find(x => x.draft_id === did)
    if (!d) return fail('NOT_FOUND', `草稿不存在：${did}`, 404)
    if (d.status !== '待复核') return fail('CONFLICT', '草稿状态非待复核', 409)
    d.status = '已驳回'
    return ok(d)
  }),
  http.post('*/api/v1/cases/:cid/etl-drafts/:did/publish', ({ params }) => {
    const did = String(params.did)
    const d = mockEtlDrafts.find(x => x.draft_id === did)
    if (!d) return fail('NOT_FOUND', `草稿不存在：${did}`, 404)
    if (d.status !== '已确认') return fail('CONFLICT', '仅已确认草稿可发布', 409)
    d.status = '已发布'
    return ok(d)
  }),

  // 质量检查：触发 202 + 最近报告（c2 无报告）
  http.post('*/api/v1/cases/:cid/quality-checks', ({ params }) =>
    HttpResponse.json({ ok: true, data: makeTask(String(params.cid), 'QUALITY_CHECK', {}), data_version: 1 }, { status: 202 }),
  ),
  http.get('*/api/v1/cases/:cid/quality-checks/latest', ({ params }) => {
    if (String(params.cid) !== 'c1') return ok({ available: false })
    return ok(mockQualityReport)
  }),

  // 隔离区 + 清洗留痕（c2 零隔离显式文案，红线五）
  http.get('*/api/v1/cases/:cid/quarantine', ({ params, request }) => {
    const cid = String(params.cid)
    if (cid !== 'c1') {
      return ok({ items: [], total: 0, stats: { cast_error: 0, null_value: 0, dedup: 0, other: 0 }, page: 1, page_size: 50, empty_message: '本次装载无数据被丢弃' })
    }
    const url = new URL(request.url)
    const reason = url.searchParams.get('reason')
    const items = reason ? mockQuarantine.filter((q) => q.reason === reason) : mockQuarantine
    return ok({
      items,
      total: items.length,
      stats: { cast_error: 2, null_value: 1, dedup: 0, other: 0 },
      page: Number(url.searchParams.get('page') ?? '1'),
      page_size: Number(url.searchParams.get('page_size') ?? '50'),
    })
  }),
  http.get('*/api/v1/cases/:cid/clean-trace', ({ params, request }) => {
    const cid = String(params.cid)
    const url = new URL(request.url)
    const items = cid === 'c1' ? mockCleanTrace : []
    return ok({ items, total: items.length, page: Number(url.searchParams.get('page') ?? '1'), page_size: Number(url.searchParams.get('page_size') ?? '50') })
  }),

  // ---------- MVP-5 庙算工作台（derived:true 派生口径；候补无升格字段，红线二）----------
  http.get('*/api/v1/cases/:cid/hypotheses', ({ params }) => {
    if (String(params.cid) !== 'c1') return ok({ available: false })
    return ok({
      available: true,
      derived: true,
      coverage: {
        declared: [
          { dimension: null, covered: 4, total: 5, missing: ['死间'], reason: '死间尚无分析师声明覆盖', severity: 'warning', created_at: '2026-09-08T10:00:00' },
        ],
        empirical: [
          { dimension: null, covered: 3, total: 5, missing: ['死间', '生间'], reason: '数据推导未见死间/生间模式', severity: 'warning', created_at: '2026-09-08T10:00:00' },
        ],
      },
      heatmap: {
        jians: ['因间', '内间', '反间', '死间', '生间'],
        levels: ['观察', '线索', '确认'],
        counts: [
          [4, 2, 3, 0, 1],
          [2, 1, 1, 0, 0],
          [1, 0, 1, 0, 0],
        ],
      },
      // 候补池：结构上不含 cross_level/new_level 等任何升格字段（FE-T-014）
      candidates: [
        { clue_id: 'CLUE-007', title: '蓝海贸易与宁波关联公司资金同日对冲', jian_types: ['因间', '反间'], level: '观察', priority_score: 81, reason: '两公司间 3 笔同日反向转账，金额近似，建议核查关联关系' },
        { clue_id: 'CLUE-008', title: '张某通话对象与仓储股东重合', jian_types: ['内间'], level: null, priority_score: 66, reason: '高频通话号码 2 个与临港仓储股东预留号一致，待实名交叉' },
      ],
      restricted: [
        { clue_id: 'CLUE-009', reason: '内间线索：需更高秩级（主办及以上）' },
      ],
    })
  }),

  // ---------- MVP-5 知识图谱（按度数采样；截断态真实给出 dropped_edges）----------
  http.get('*/api/v1/cases/:cid/graph', ({ params }) => {
    if (String(params.cid) !== 'c1') {
      return ok({ available: false, truncated: { nodes: false, edges: false, dropped_edges: 0 }, nodes: [], edges: [] })
    }
    return ok({
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
  }),

  // ---------- MVP-5 跨案件查询（全有或全无：无权案件整体 403；SQL 白名单）----------
  http.post('*/api/v1/cross-case/query', async ({ request }) => {
    const body = (await request.json()) as { case_ids?: string[]; sql?: string; reason?: string }
    const ids = body.case_ids ?? []
    const owned = new Set(mvp5Cases.map((c) => c.id))
    const denied = ids.filter((id) => !owned.has(id))
    if (denied.length) {
      return fail('FORBIDDEN', `全有或全无鉴权失败：案件 ${denied.join('、')} 无权访问，本次查询整体拒绝`, 403)
    }
    const head = (body.sql ?? '').trim().split(/\s+/)[0]?.toUpperCase() ?? ''
    if (!['SELECT', 'WITH', 'PRAGMA'].includes(head)) {
      return fail('VALIDATION', '跨案 SQL 仅允许 SELECT/WITH/PRAGMA 开头（只读）', 400)
    }
    if (!body.reason?.trim()) return fail('VALIDATION', '查询事由必填（审计留痕）', 400)
    const hasSourceCol = /source_case/i.test(body.sql ?? '')
    const rows = ids.flatMap((cid) =>
      [1, 2].map((n) => ({
        ...(hasSourceCol ? { source_case: cid } : {}),
        id: `${cid}-p${n}`,
        name: cid === 'c1' ? ['张某', '蓝海贸易有限公司'][n - 1] : ['李某', '临港仓储服务有限公司'][n - 1],
        type: n === 1 ? 'person' : 'organization',
      })),
    )
    crossHistory.unshift({
      id: ++crossHistorySeq,
      ts: '2026-09-09 13:20:00',
      case_ids: ids,
      sql: body.sql ?? '',
      reason: body.reason,
      result_rows: rows.length,
    })
    return ok({ rows, total: rows.length, case_ids: ids })
  }),

  http.get('*/api/v1/cross-case/history', ({ request }) => {
    const url = new URL(request.url)
    const page = Math.max(1, parseInt(url.searchParams.get('page') ?? '1', 10) || 1)
    const pageSize = Math.min(200, Math.max(1, parseInt(url.searchParams.get('page_size') ?? '50', 10) || 50))
    const start = (page - 1) * pageSize
    return ok({ items: crossHistory.slice(start, start + pageSize), total: crossHistory.length, page, page_size: pageSize })
  }),

  // ---------- MVP-5 案件包（导出任务/SSE/下载 Blob/七步校验/导入）----------
  http.post('*/api/v1/cases/:cid/package/export', ({ params }) => {
    const cid = String(params.cid)
    if (!mvp5Cases.some((c) => c.id === cid)) return fail('NOT_FOUND', '案件不存在', 404)
    const t = makeTask(cid, 'EXPORT_PACKAGE', {})
    return ok({ task_id: t.id })
  }),

  http.get('*/api/v1/packages/:tid/download', ({ params }) => {
    const t = findTask(String(params.tid))
    if (!t || t.task_type !== 'EXPORT_PACKAGE') return fail('NOT_FOUND', '导出包不存在或任务未完成', 404)
    const blob = new Blob([`PK mock package for ${t.case_id}\n(演示环境占位 zip 字节流)`], {
      type: 'application/zip',
    })
    return new HttpResponse(blob, {
      headers: {
        'Content-Type': 'application/zip',
        'Content-Disposition': `attachment; filename="${t.case_id}_package.zip"`,
      },
    })
  }),

  http.post('*/api/v1/packages/verify', async ({ request }) => {
    let filename = 'package.zip'
    try {
      const fd = await request.formData()
      const f = fd.get('file')
      if (f instanceof File) filename = f.name
    } catch { /* noop */ }
    const bad = /bad|损坏|篡改/i.test(filename)
    const noChain = /nochain|无链|broken/i.test(filename)
    const steps = [
      { key: 'format', label: '压缩包格式', status: 'pass' as const, detail: 'zip 结构完整' },
      { key: 'manifest', label: 'manifest 清单', status: 'pass' as const, detail: 'manifest.json 齐备' },
      {
        key: 'hash', label: '哈希校验',
        status: bad ? ('fail' as const) : ('pass' as const),
        detail: bad ? 'root_hash 与内容不符（包可能被篡改）' : 'root_hash 一致',
      },
      { key: 'declarations', label: '声明文件', status: 'pass' as const, detail: 'objects/links/rules/functions 齐备' },
      { key: 'schema', label: 'schema 版本', status: 'pass' as const, detail: 'schema_version=2 兼容' },
      {
        key: 'chain', label: '审计链',
        status: noChain || bad ? ('warn' as const) : ('pass' as const),
        detail: noChain || bad ? 'state.sqlite 缺失/链不完整：橙色告警，不阻断导入' : '审计链完整',
      },
      { key: 'duckdb', label: 'DuckDB 库', status: 'pass' as const, detail: 'investigation.duckdb 可读' },
    ]
    const errors = bad ? ['哈希校验失败：root_hash 不匹配', '声明 objects.json 哈希条目缺失'] : []
    return ok({
      ok: !bad,
      errors,
      chain_ok: !(noChain || bad),
      file_count: 18,
      steps,
      sensitive_files: ['ontology/default/case_knowledge.json'],
    })
  }),

  http.post('*/api/v1/packages/import', async ({ request }) => {
    let caseId = ''
    let name = ''
    try {
      const fd = await request.formData()
      caseId = String(fd.get('case_id') ?? '')
      name = String(fd.get('name') ?? '')
    } catch { /* noop */ }
    if (!caseId.trim() || !name.trim()) return fail('VALIDATION', '新案件编号与名称必填', 400)
    if (mvp5Cases.some((c) => c.id === caseId)) {
      return fail('CONFLICT', `案件编号已存在：${caseId}（导入将创建新案件，请换编号）`, 409)
    }
    const t = makeTask(caseId, 'IMPORT_PACKAGE', { case_id: caseId, name })
    return ok({ task_id: t.id })
  }),

  // ---------- MVP-5 代码逃生舱（只生成文本，不写盘/不注册/不执行）----------
  http.post('*/api/v1/escape-hatch/generate', async ({ request }) => {
    const body = (await request.json()) as { ext_type?: string; name?: string; description?: string }
    const name = String(body.name ?? '').trim()
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
      return fail('VALIDATION', '名称须为标识符（字母/下划线开头）', 400)
    }
    const tpl = HATCH_TEMPLATES[body.ext_type ?? 'function'] ?? HATCH_TEMPLATES.function
    hatchStats[body.ext_type ?? 'function'] = (hatchStats[body.ext_type ?? 'function'] ?? 0) + 1
    return ok({
      files: [
        { path: tpl.path.replace('{name}', name), content: tpl.content.replace(/\{name\}/g, name).replace('{desc}', body.description ?? '') },
      ],
      registration_points: tpl.points,
    })
  }),

  http.get('*/api/v1/escape-hatch/stats', () =>
    ok({
      items: [
        { ext_type: 'function', count: hatchStats.function },
        { ext_type: 'value_type', count: hatchStats.value_type },
        { ext_type: 'clean_rule', count: hatchStats.clean_rule },
        { ext_type: 'side_effect', count: hatchStats.side_effect },
      ],
      total: Object.values(hatchStats).reduce((a, b) => a + b, 0),
    }),
  ),

  // ---------- MVP-5 系统设置（非 admin 全 403；health 登录可读）----------
  http.get('*/api/v1/settings/health', () =>
    ok({
      meta_ok: true,
      queue: { pending: 1, running: 0 },
      worker: {
        pool_alive: false,
        max_workers: mockSettings.queue.max_workers,
        poll_interval_ms: mockSettings.queue.poll_interval_ms,
        note: 'API 进程不内嵌 Worker；池存活由部署侧监控',
      },
      versions: { backend: 'M6', ontology_default: 'default' },
    }),
  ),
  http.get('*/api/v1/settings/queue', () =>
    isMockAdmin() ? ok({ ...mockSettings.queue }) : fail('FORBIDDEN', '平台设置仅管理员可操作', 403),
  ),
  http.put('*/api/v1/settings/queue', async ({ request }) =>
    putSettings(request, 'queue', ['max_workers', 'poll_interval_ms'], mockSettings.queue),
  ),
  http.get('*/api/v1/settings/resources', () =>
    isMockAdmin()
      ? ok({ ...mockSettings.resources, storage_root: '/data/sunzi/cases' })
      : fail('FORBIDDEN', '平台设置仅管理员可操作', 403),
  ),
  http.put('*/api/v1/settings/resources', async ({ request }) =>
    putSettings(request, 'resources', ['max_rows_default', 'query_timeout_ms'], mockSettings.resources),
  ),
  http.get('*/api/v1/settings/policies-thresholds', () =>
    isMockAdmin()
      ? ok({ thresholds: { ...mockSettings.thresholds }, note: '平台值仅用于新建案件默认；案件生效阈值以案件快照 thresholds.json 为准' })
      : fail('FORBIDDEN', '平台设置仅管理员可操作', 403),
  ),
  http.put('*/api/v1/settings/policies-thresholds', async ({ request }) =>
    putSettings(request, 'policies_thresholds', ['cross_level_min_sources', 'cross_level_min_clues', 'stale_days'], mockSettings.thresholds),
  ),
  http.get('*/api/v1/settings/features', () =>
    isMockAdmin() ? ok({ ...mockSettings.features }) : fail('FORBIDDEN', '平台设置仅管理员可操作', 403),
  ),
  http.put('*/api/v1/settings/features', async ({ request }) =>
    putSettings(request, 'features', ['ui_density'], mockSettings.features),
  ),
  http.get('*/api/v1/settings/snapshots', () => {
    if (!isMockAdmin()) return fail('FORBIDDEN', '平台设置仅管理员可操作', 403)
    return ok({
      items: [
        { case_id: 'c1', pack_id: 'default', version: '2.1.0', snapshot_path: 'cases/c1/ontology/default', locked_at: '2026-09-08T10:00:00' },
      ],
      total: 1,
    })
  }),
]

// ---------- 实体裁决 mock 数据 ----------

interface MockReviewAttribute {
  label: string
  left: string
  right: string
  basis?: boolean
  mask?: 'phone' | 'idcard' | 'text'
  policy?: 'visible' | 'masked' | 'denied'
}

interface MockReviewCandidate {
  entity_id: string
  canonical_name: string
  variants: string[]
  confidence: number
  needs_review: boolean
  merge_reason: string
  evidence: {
    common_credit_codes: string[]
    common_legal_reps: string[]
    common_addresses: string[]
    common_accounts: string[]
    common_source_rows: string[]
  }
  attributes: MockReviewAttribute[]
  llm_inferences?: Array<Record<string, unknown>>
}

function evRows(code: string, rep: string, addr: string, acct: string, rows: string[]) {
  return {
    common_credit_codes: [code],
    common_legal_reps: [rep],
    common_addresses: [addr],
    common_accounts: [acct],
    common_source_rows: rows,
  }
}

const handwrittenCandidates: MockReviewCandidate[] = [
  {
    entity_id: 'ENT-1001',
    canonical_name: '蓝海贸易有限公司',
    variants: ['蓝海贸易', '蓝海贸易（上海）', '上海蓝海贸易有限公司'],
    confidence: 0.92,
    needs_review: true,
    merge_reason: '统一社会信用代码一致 + 法定代表人同名 + 银行账号重合 1 个',
    evidence: evRows(
      '91310115MA1K4X7T2B',
      '张某',
      '上海市浦东新区富特北路 211 号',
      '6222-****-8843',
      ['obj_company:reg:default:row:101', 'obj_account:bank:default:row:8843'],
    ),
    attributes: [
      { label: '统一社会信用代码', left: '91310115MA1K4X7T2B', right: '91310115MA1K4X7T2B', basis: true },
      { label: '法定代表人', left: '张某', right: '张某', basis: true },
      { label: '注册地址', left: '中国（上海）自由贸易试验区富特北路 211 号 302 室', right: '上海市浦东新区富特北路 211 号 302 室' },
      { label: '联系电话', left: '13812345678', right: '13812345678', basis: true, mask: 'phone', policy: 'masked' },
      { label: '银行账号', left: '6222-****-8843', right: '6222-****-8843', basis: true },
      { label: '成立日期', left: '2019-03-12', right: '2019-03-12' },
    ],
    llm_inferences: [
      // FE-T-007 演示：同一模型两条判读（资金/通讯）——同源不双计
      {
        text: '转账备注语义（货款/走账）与群聊暗语风格一致，判读为同一资金调度主体。',
        llm_model: 'sunzi-llm-shadow-v1',
        confidence: 0.78,
        room: '资金',
        source_rows: [{ row_uri: 'obj_transaction:bank_flow:default:row:8f3a21', source: '银行流水' }],
      },
      {
        text: '两个实体预留联系号码通话对象重合度 86%，判读为同一经办人。',
        llm_model: 'sunzi-llm-shadow-v1',
        confidence: 0.74,
        room: '通讯',
        source_rows: [{ row_uri: 'obj_call:call_record:default:row:c77d10', source: '通话记录' }],
      },
      // 三件套缺一（无溯源）：前端不得渲染
      {
        text: '（缺溯源行，FE-C-023 应拒绝渲染）',
        llm_model: 'sunzi-llm-shadow-v1',
        confidence: 0.66,
        room: '关系',
      },
    ],
  },
  {
    entity_id: 'ENT-1002',
    canonical_name: '张伟',
    variants: ['张伟', '張偉', '张伟（浦东）'],
    confidence: 0.62,
    needs_review: true,
    merge_reason: '同名 + 手机号前 3 后 4 一致；身份证号与住址不一致，疑似同名不同人',
    evidence: evRows('', '', '上海市浦东新区', '', [
      'obj_person:pop:default:row:2001',
      'obj_person:pop:default:row:2044',
    ]),
    attributes: [
      { label: '姓名', left: '张伟', right: '张伟', basis: true },
      { label: '身份证号', left: '310115199003124512', right: '310115198711053218', mask: 'idcard', policy: 'masked' },
      { label: '手机号码', left: '13912340001', right: '13912340002', mask: 'phone', policy: 'masked' },
      { label: '住址', left: '上海市浦东新区惠南镇文友街 18 号', right: '上海市闵行区莘庄镇都市路 520 号' },
      { label: '出生年月', left: '1990-03', right: '1987-11' },
    ],
  },
  {
    entity_id: 'ENT-1003',
    canonical_name: '临港仓储服务有限公司',
    variants: ['临港仓储', '临港仓储服务社', '临港仓储服务有限公司'],
    confidence: 0.88,
    needs_review: true,
    merge_reason: '统一社会信用代码一致 + 注册地址同址',
    evidence: evRows('91310115MA1H9L2K8M', '李某', '上海市浦东新区临港大道 1800 号', '', [
      'obj_company:reg:default:row:330',
    ]),
    attributes: [
      { label: '统一社会信用代码', left: '91310115MA1H9L2K8M', right: '91310115MA1H9L2K8M', basis: true },
      { label: '企业名称', left: '临港仓储服务社', right: '临港仓储服务有限公司' },
      { label: '法定代表人', left: '李某', right: '李某', basis: true },
      { label: '注册地址', left: '浦东新区临港大道 1800 号', right: '上海市浦东新区临港大道 1800 号 2 幢' },
    ],
  },
  {
    entity_id: 'ENT-1004',
    canonical_name: '李某某',
    variants: ['李某', '李某某'],
    confidence: 0.55,
    needs_review: true,
    merge_reason: '仅姓名近似，无其他共同标识——低置信度，需人工研判是否同名不同人',
    evidence: evRows('', '', '', '', ['obj_person:pop:default:row:410']),
    attributes: [
      { label: '姓名', left: '李某', right: '李某某' },
      { label: '身份证号', left: '310109198507126633', right: '310226199201087745', mask: 'idcard', policy: 'masked' },
      { label: '关联企业', left: '临港仓储服务有限公司', right: '无' },
      { label: '手机号', left: '13900001122', right: '13700008899', mask: 'phone', policy: 'masked' },
    ],
  },
  {
    entity_id: 'ENT-1005',
    canonical_name: '王某',
    variants: ['王某', '王某某', '王某（收款方）'],
    confidence: 0.71,
    needs_review: true,
    merge_reason: '银行预留手机号一致；姓名为简写变体，待人工确认',
    evidence: evRows('', '', '', '6217-****-2210', [
      'obj_account:bank:default:row:2210',
    ]),
    attributes: [
      { label: '姓名', left: '王某', right: '王某某' },
      { label: '银行预留手机', left: '13655552210', right: '13655552210', basis: true, mask: 'phone', policy: 'masked' },
      { label: '开户银行', left: '工商银行浦东支行', right: '工商银行自贸区支行' },
      { label: '账号', left: '6217-****-2210', right: '6217-****-2210', basis: true },
    ],
  },
  {
    entity_id: 'ENT-1006',
    canonical_name: '蓝海贸易（宁波）有限公司',
    variants: ['蓝海贸易', '蓝海贸易（宁波）'],
    confidence: 0.43,
    needs_review: true,
    merge_reason: '企业字号相同但信用代码不同、法定代表人不同——疑似名称撞车，低置信度',
    evidence: evRows('91330201MA2K8T9X3P', '陈某', '宁波市北仑区', '', [
      'obj_company:reg:default:row:602',
    ]),
    attributes: [
      { label: '企业名称', left: '蓝海贸易有限公司', right: '蓝海贸易（宁波）有限公司' },
      { label: '统一社会信用代码', left: '91310115MA1K4X7T2B', right: '91330201MA2K8T9X3P' },
      { label: '法定代表人', left: '张某', right: '陈某' },
      { label: '注册地', left: '上海市浦东新区', right: '宁波市北仑区' },
    ],
  },
]

const personNames: Array<[string, string[]]> = [
  ['刘强', ['刘强', '刘強']],
  ['陈静', ['陈静', '陳靜']],
  ['杨丽', ['杨丽', '楊麗', '杨丽（财务）']],
  ['赵磊', ['赵磊', '趙磊']],
  ['孙敏', ['孙敏', '孫敏']],
  ['周涛', ['周涛', '周濤']],
  ['吴倩', ['吴倩', '吳倩']],
  ['郑浩', ['郑浩', '鄭浩']],
  ['冯雪', ['冯雪', '馮雪']],
  ['何军', ['何军', '何軍']],
  ['马丽', ['马丽', '馬麗']],
  ['黄勇', ['黄勇', '黃勇']],
  ['徐婷', ['徐婷']],
  ['高鹏', ['高鹏', '高鵬']],
  ['林芳', ['林芳']],
  ['罗杰', ['罗杰', '羅傑']],
  ['梁燕', ['梁燕', '樑燕']],
  ['宋凯', ['宋凯', '宋凱']],
  ['谢楠', ['谢楠', '謝楠']],
]

const generatedCandidates: MockReviewCandidate[] = personNames.map(([name, variants], i) => {
  const id = 1007 + i
  const conf = 0.6 + ((i * 7) % 30) / 100
  return {
    entity_id: `ENT-${id}`,
    canonical_name: name,
    variants: [`${name}`, ...variants.filter((v) => v !== name)],
    confidence: Number(conf.toFixed(2)),
    needs_review: true,
    merge_reason: `姓名书写变体 ${variants.length} 个 + 标识项部分重合（相似度 ${Math.round(conf * 100)}%）`,
    evidence: evRows(
      i % 3 === 0 ? `91310115MA1${String(1000 + id)}` : '',
      name,
      i % 2 === 0 ? '上海市浦东新区' : '',
      '',
      [`obj_person:pop:default:row:${2000 + id}`],
    ),
    attributes: [
      { label: '姓名', left: variants[0], right: name, basis: true },
      { label: '身份证号', left: `31011519${80 + (i % 15)}0101${1000 + i}`, right: `31011519${80 + (i % 15)}0101${1000 + i}`, basis: true, mask: 'idcard', policy: 'masked' },
      { label: '手机号', left: `13${6 + (i % 4)}${String(10000000 + i * 137).padStart(8, '0')}`, right: `13${6 + (i % 4)}${String(10000000 + i * 137).padStart(8, '0')}`, basis: true, mask: 'phone', policy: 'masked' },
      { label: '住址', left: `上海市浦东新区${['惠南镇', '周浦镇', '川沙新镇'][i % 3]}`, right: `上海市浦东新区${['惠南镇', '周浦镇', '川沙新镇'][i % 3]}路名记录不一`, basis: false },
    ],
  }
})

const reviewCandidates: Record<string, MockReviewCandidate[]> = {
  c1: [...handwrittenCandidates, ...generatedCandidates],
  c2: [],
}

const reviewHistory: Record<string, Array<{
  candidate_id: string
  canonical_name: string
  action: 'merge' | 'reject'
  confidence: number
  operator: string
  reason?: string
  occurred_at: string
}>> = {
  c1: [
    {
      candidate_id: 'ENT-0998',
      canonical_name: '蓝海贸易商行',
      action: 'merge',
      confidence: 0.91,
      operator: '王检察官',
      reason: '信用代码与账号均一致，确认为同一主体',
      occurred_at: '2026-09-07 15:42',
    },
    {
      candidate_id: 'ENT-0997',
      canonical_name: '张威',
      action: 'reject',
      confidence: 0.58,
      operator: '张助理',
      reason: '仅姓名同音，身份证号/住址/手机均不同，为同名不同人',
      occurred_at: '2026-09-06 10:18',
    },
  ],
}

// ---------- 数据画像 mock 数据（与后端 profiles_view.assemble_profiles 同构） ----------

const profileMock = {
  available: true,
  derived: true,
  focus: ['蓝海贸易有限公司', '张伟'],
  anchor_date: '2026-09-09',
  pack: 'default',
  l0: 'not_applicable（物化后无文件层；L0 拓扑见 core.data_map.DataMap）',
  // 对象/属性名与 ontology/default/objects.json 真实声明一致
  l1_l2: [
    { obj: 'person', prop: 'raw_name', declared_type: 'string', connectable: true, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 1208, non_null: 1205, null_rate: 0.002, distinct: 1180, samples: ['张伟', '李某某', '王某'] }, mixed: false, type_dist: { person: 1205 }, landing_suggestions: ['person'], needs_confirmation: true, variants: { rule: 2, alias: 1 } },
    { obj: 'org', prop: 'raw_name', declared_type: 'string', connectable: true, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 320, non_null: 320, null_rate: 0.0, distinct: 320, samples: ['蓝海贸易有限公司'] }, mixed: false, type_dist: { org: 320 }, landing_suggestions: ['org'], needs_confirmation: false, variants: { rule: 0, alias: 0 } },
    { obj: 'org', prop: 'legal_rep', declared_type: 'string', connectable: false, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 320, non_null: 306, null_rate: 0.044, distinct: 298, samples: ['王建国'] } },
    { obj: 'account', prop: 'raw_name', declared_type: 'string', connectable: true, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 243, non_null: 236, null_rate: 0.029, distinct: 243, samples: ['6222****8888'] }, mixed: false, type_dist: { account: 236 }, landing_suggestions: ['account'], needs_confirmation: false, variants: { rule: 0, alias: 0 } },
    { obj: 'transaction', prop: 'from_raw', declared_type: 'string', connectable: true, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 9214, non_null: 9210, null_rate: 0.0, distinct: 312, samples: ['蓝海贸易有限公司'] }, mixed: false, type_dist: { org: 8800, person: 410 }, landing_suggestions: ['org', 'person'], needs_confirmation: false, variants: { rule: 0, alias: 0 } },
    { obj: 'transaction', prop: 'amount', declared_type: 'decimal', connectable: false, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 9214, non_null: 9205, null_rate: 0.001, distinct: 8600, samples: ['50000.00'] }, mixed: true },
    { obj: 'transaction', prop: 'date', declared_type: 'date', connectable: false, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 9214, non_null: 9214, null_rate: 0.0, distinct: 365, samples: ['2026-08-15'] } },
    { obj: 'call', prop: 'caller_raw', declared_type: 'string', connectable: true, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 2540, non_null: 2540, null_rate: 0.0, distinct: 186, samples: ['张伟'] }, mixed: false, type_dist: { person: 2540 }, landing_suggestions: ['person'], needs_confirmation: false, variants: { rule: 0, alias: 0 } },
    { obj: 'call', prop: 'times', declared_type: 'integer', connectable: false, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 2540, non_null: 1996, null_rate: 0.214, distinct: 540, samples: ['120'] } },
    { obj: 'call', prop: 'date', declared_type: 'date', connectable: false, materialized_object: true, materialized_prop: true, status: 'ok', value_profile: { row_count: 2540, non_null: 2540, null_rate: 0.0, distinct: 120, samples: ['2026-08-20'] } },
  ],
  l3: [
    { obj: 'person', prop: 'raw_name', metric: 'focus_hit_rate', status: 'ok', value: 0.92 },
    { obj: 'org', prop: 'raw_name', metric: 'focus_hit_rate', status: 'ok', value: 0.88 },
    { obj: 'transaction', prop: 'from_raw', metric: 'focus_hit_rate', status: 'ok', value: 0.76 },
  ],
  l4: {
    forward: {
      生间: { objects: ['transaction', 'call'], links: ['lnk_transfers', 'lnk_calls'] },
      死间: { objects: ['org', 'account'], links: [] },
      因间: { objects: [], links: [] },
      内间: { objects: ['person'], links: [] },
      反间: { objects: [], links: [] },
    },
    reverse: [
      { jian: '生间', objects: ['transaction', 'call'], links: ['lnk_transfers', 'lnk_calls'], declared: true, has_materialized: true },
      { jian: '死间', objects: ['org', 'account'], links: [], declared: true, has_materialized: true },
      { jian: '因间', objects: [], links: [], declared: false, has_materialized: false },
      { jian: '内间', objects: ['person'], links: [], declared: true, has_materialized: true },
      { jian: '反间', objects: [], links: [], declared: false, has_materialized: false },
    ],
  },
  l5: {
    score: 65,
    score_range: [65, 70],
    deductions: [
      { scope: 'prop', ref: 'transaction.amount', code: 'mixed', reason: '混装：decimal 列混入字符串（"约5万"/"480000.00元"），TRY_CAST 降级 NULL', severity: 'block', points: -25 },
      { scope: 'prop', ref: 'person.raw_name', code: 'has_variants', reason: '变体候选 3 个（规则 2/别名 1）', severity: 'warn', points: -5 },
      { scope: 'object', ref: 'transaction', code: 'no_wan_integer', reason: 'transaction.amount 无万元整数交易（资金信号弱）', severity: 'warn', points: -5 },
    ],
    reviewable: true,
    weights: {
      block: { mixed: -25, null_rate_high: -20, zero_rows: -30 },
      warn: { unmaterialized: -12, low_cardinality: -8, affirmative_type: -5, no_wan_integer: -5, has_variants: -5 },
      compliance: { compliance_violation_high: -20, compliance_violation: -10 },
    },
    note: '画像分 = 100 + Σ(扣分)；启发式扣分（肯定式识别/变体）可人工推翻（score_range 上沿）',
  },
  compliance: null,
  params: { window_days: 90, anchor_date: '2026-09-09', focus_entities: ['蓝海贸易有限公司', '张伟'] },
  health: { degraded: false, warnings: [] },
  note: '结论均为【待核实】候选；画像只观察不写回，启发式扣分（肯定式识别/变体）可人工推翻',
}

// ---------- MVP-3 任务/接入 mock 数据 ----------

interface MockTask {
  id: string
  case_id: string
  task_type: string
  params: Record<string, unknown>
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  progress_pct: number
  progress_stage: string
  progress_label: string
  progress_detail: string
  retry_count: number
  max_retries: number
  idem_key: string
  created_at: string
  updated_at: string
  started_at: string
  finished_at: string
  error_code: string
  error_message: string
  created_by: string
}

interface MockSource {
  upload_id: string
  filename: string
  format: string
  fingerprint: string
  rows: number
  status: string
  columns: Array<{ name: string; inferred_type?: string; null_rate?: number; distinct?: number; samples?: string[] }>
  declared_tables: Record<string, string[]>
  created_by: string
  created_at: string
}

let taskSeq = 1000
function makeTask(cid: string, taskType: string, params: Record<string, unknown>): MockTask {
  taskSeq += 1
  const id = `task-${taskSeq}`
  const t: MockTask = {
    id, case_id: cid, task_type: taskType, params,
    status: 'PENDING', progress_pct: 0, progress_stage: '', progress_label: '', progress_detail: '',
    retry_count: 0, max_retries: 3, idem_key: `idem-${id}`,
    created_at: '2026-09-09T10:59:00', updated_at: '2026-09-09T10:59:00',
    started_at: '', finished_at: '', error_code: '', error_message: '', created_by: '王检察官',
  }
  mockTasks[cid] = mockTasks[cid] ?? []
  mockTasks[cid].push(t)
  return t
}

function findTask(tid: string): MockTask | undefined {
  for (const list of Object.values(mockTasks)) {
    const found = list.find((t) => t.id === tid)
    if (found) return found
  }
  return undefined
}

const mockTasks: Record<string, MockTask[]> = {
  c1: [
    {
      id: 'task-0901', case_id: 'c1', task_type: 'BUILD', params: { target_version: 'v3', base_version: 'v2' },
      status: 'SUCCEEDED', progress_pct: 100, progress_stage: 'done', progress_label: '构建完成',
      progress_detail: '语义层 v3 已生成（目标 v3，基线 v2）', retry_count: 0, max_retries: 3,
      idem_key: 'idem-0901', created_at: '2026-09-09T09:30:00', updated_at: '2026-09-09T09:32:10',
      started_at: '2026-09-09T09:30:05', finished_at: '2026-09-09T09:32:10', error_code: '', error_message: '',
      created_by: '王检察官',
    },
    {
      id: 'task-0902', case_id: 'c1', task_type: 'IMPORT', params: { target_table: '银行流水', filename: '银行流水_8月.csv' },
      status: 'SUCCEEDED', progress_pct: 100, progress_stage: 'done', progress_label: '导入完成',
      progress_detail: '银行流水 1203 行 → BUILD 已入队', retry_count: 0, max_retries: 3,
      idem_key: 'idem-0902', created_at: '2026-09-09T09:10:00', updated_at: '2026-09-09T09:11:40',
      started_at: '2026-09-09T09:10:03', finished_at: '2026-09-09T09:11:40', error_code: '', error_message: '',
      created_by: '王检察官',
    },
    {
      id: 'task-0903', case_id: 'c1', task_type: 'IMPORT', params: { target_table: '通话记录', filename: '通话记录_破损.xlsx' },
      status: 'FAILED', progress_pct: 40, progress_stage: 'parse', progress_label: '解析数据',
      progress_detail: '通话记录_破损.xlsx 第 7 行列数不一致', retry_count: 2, max_retries: 3,
      idem_key: 'idem-0903', created_at: '2026-09-09T08:50:00', updated_at: '2026-09-09T08:51:20',
      started_at: '2026-09-09T08:50:05', finished_at: '2026-09-09T08:51:20',
      error_code: 'IMPORT_PARSE', error_message: '文件解析失败：第 7 行列数与表头不一致',
      created_by: '张助理',
    },
    {
      id: 'task-0904', case_id: 'c1', task_type: 'QUALITY_CHECK', params: {},
      status: 'CANCELLED', progress_pct: 15, progress_stage: 'prepare', progress_label: '准备扫描',
      progress_detail: '用户取消', retry_count: 0, max_retries: 3,
      idem_key: 'idem-0904', created_at: '2026-09-09T08:20:00', updated_at: '2026-09-09T08:20:30',
      started_at: '', finished_at: '2026-09-09T08:20:30', error_code: '', error_message: '',
      created_by: '王检察官',
    },
    {
      id: 'task-0905', case_id: 'c1', task_type: 'DISPOSE', params: { clue_id: 'CLUE-003' },
      status: 'SUCCEEDED', progress_pct: 100, progress_stage: 'done', progress_label: '处置完成',
      progress_detail: '线索处置已记录', retry_count: 0, max_retries: 3,
      idem_key: 'idem-0905', created_at: '2026-09-09T10:00:00', updated_at: '2026-09-09T10:00:02',
      started_at: '2026-09-09T10:00:01', finished_at: '2026-09-09T10:00:02', error_code: '', error_message: '',
      created_by: '王检察官',
    },
  ],
}

// 分页演示：补足 55 条 8 月历史终态任务（终态不进「进行中」区、不触发 SSE），
// 使 c1 历史表达 60 条 / 2 页（page_size=50），任务中心分页栏可见可翻。
;(() => {
  const histTables = ['银行流水', '通话记录', '招投标', '轨迹']
  for (let i = 0; i < 55; i++) {
    const day = String(1 + Math.floor(i / 3)).padStart(2, '0')
    const hh = String(9 + (i % 9)).padStart(2, '0')
    const mm = String((i * 13) % 60).padStart(2, '0')
    const ts = `2026-08-${day}T${hh}:${mm}:00`
    const type = i % 3 === 0 ? 'IMPORT' : i % 3 === 1 ? 'BUILD' : 'QUALITY_CHECK'
    const failed = i % 17 === 4
    const cancelled = !failed && i % 23 === 7
    const status = failed ? 'FAILED' : cancelled ? 'CANCELLED' : 'SUCCEEDED'
    const table = histTables[i % histTables.length]
    const ver = 2 + (i % 3)
    const params = type === 'IMPORT'
      ? { target_table: table, filename: `${table}_8月.csv` }
      : type === 'BUILD'
        ? { target_version: `v${ver}`, base_version: `v${ver - 1}` }
        : {}
    const doneDetail = type === 'IMPORT'
      ? `${table} ${800 + i * 7} 行 → BUILD 已入队`
      : type === 'BUILD' ? `语义层 v${ver} 已生成` : '质量扫描完成'
    const doneLabel = type === 'IMPORT' ? '导入完成' : type === 'BUILD' ? '构建完成' : '扫描完成'
    mockTasks.c1.push({
      id: `task-h${String(i + 1).padStart(2, '0')}`,
      case_id: 'c1', task_type: type, params,
      status,
      progress_pct: status === 'SUCCEEDED' ? 100 : status === 'FAILED' ? 40 + (i % 30) : 15,
      progress_stage: status === 'SUCCEEDED' ? 'done' : status === 'FAILED' ? 'parse' : 'prepare',
      progress_label: status === 'SUCCEEDED' ? doneLabel : status === 'FAILED' ? '解析数据' : '准备扫描',
      progress_detail: status === 'FAILED'
        ? `${table}_8月.csv 第 ${10 + i} 行列数不一致`
        : status === 'CANCELLED' ? '用户取消' : doneDetail,
      retry_count: failed ? 2 : 0, max_retries: 3,
      idem_key: `idem-h${i + 1}`,
      created_at: ts, updated_at: ts,
      started_at: cancelled ? '' : ts,
      finished_at: ts,
      error_code: failed
        ? (type === 'IMPORT' ? 'IMPORT_PARSE' : type === 'BUILD' ? 'BUILD_ALIGN' : 'QC_FAILED')
        : '',
      error_message: failed ? `文件解析失败：第 ${10 + i} 行列数与表头不一致` : '',
      created_by: i % 4 === 0 ? '张助理' : '王检察官',
    })
  }
})()

const mockSources: Record<string, MockSource[]> = {}

function bankSourceColumns() {
  return [
    { name: '交易时间', inferred_type: 'date', null_rate: 0.0, distinct: 360, samples: ['2026-08-21 10:14:22'] },
    { name: '付款方名称', inferred_type: 'string', null_rate: 0.0, distinct: 88, samples: ['蓝海贸易有限公司'] },
    { name: '收款方', inferred_type: 'string', null_rate: 0.0, distinct: 102, samples: ['张某'] },
    { name: '交易金额', inferred_type: 'decimal', null_rate: 0.01, distinct: 980, samples: ['480000.00'] },
    { name: '对方账号', inferred_type: 'string', null_rate: 0.03, distinct: 130, samples: ['6222****8843'] },
    { name: '备注', inferred_type: 'string', null_rate: 0.42, distinct: 60, samples: ['货款'] },
  ]
}

function bankDeclaredTablesDict(): Record<string, string[]> {
  return { 银行流水: ['交易时间', '付款方', '收款方', '金额', '摘要'], 通话记录: ['通话时间', '主叫', '被叫', '通话时长'] }
}

function bankDeclaredTablesView() {
  return [
    { name: '银行流水', title: '银行流水', required_columns: ['交易时间', '付款方', '收款方', '金额', '摘要'], optional_columns: ['对方账号', '备注'] },
    { name: '通话记录', title: '通话记录', required_columns: ['通话时间', '主叫', '被叫'], optional_columns: ['通话时长'] },
  ]
}

function bankSuggestion() {
  return {
    target_table: '银行流水',
    confidence: 0.77,
    matches: [
      { source_col: '交易时间', target_prop: '交易时间', match_type: 'exact', confidence: 1.0 },
      { source_col: '付款方名称', target_prop: '付款方', match_type: 'normalized', confidence: 0.85 },
      { source_col: '收款方', target_prop: '收款方', match_type: 'normalized', confidence: 0.85 },
      { source_col: '交易金额', target_prop: '金额', match_type: 'fuzzy', confidence: 0.6 },
      { source_col: null, target_prop: '摘要', match_type: 'none', confidence: 0.0 },
      { source_col: '对方账号', target_prop: '对方账号', match_type: 'exact', confidence: 1.0 },
      { source_col: '备注', target_prop: '备注', match_type: 'exact', confidence: 1.0 },
    ],
    missing_required: ['摘要'],
    low_confidence: ['金额'],
  }
}

const mockDeRecos = [
  {
    rid: 'der_aaa111bbb222',
    upload_id: 'up_demo_bank',
    status: '待核实',
    created_at: '2026-09-09T10:30:00',
    created_by: '王检察官',
    decided_by: '',
    decided_at: '',
    note: '',
    filename: '银行流水_8月.csv',
    recommendations: [
      {
        col: '付款方名称',
        element_id: 'DE_ORG_NAME',
        element_name: '机构名称',
        confidence: 0.9,
        evidence: { match_values: ['蓝海贸易有限公司', '临港仓储有限公司', '宁波蓝海商贸'] },
      },
      {
        col: '交易金额',
        element_id: 'DE_AMOUNT_YUAN',
        element_name: '金额（元）',
        confidence: 0.6,
        evidence: { match_values: ['480000.00', '50000.00'] },
      },
    ],
  },
]

// ---------- MVP-4 配置面 mock 数据（结构仿 ontology/default/*.json）----------

/** 配置写追加 config 审计事件（审计页「配置」tab 可见；红线判定不在 mock） */
function appendConfigAudit(cid: string, op: string, filename: string, reason: string | undefined, summary?: Record<string, unknown>): void {
  auditSeq += 1
  auditEvents.push({
    seq: auditSeq,
    event_id: `evt-${auditSeq}`,
    case_id: cid,
    occurred_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
    operator: '王检察官',
    action: 'config',
    status_from: null,
    status_to: null,
    note: reason?.trim() ? `${op}（${filename}）：${reason.trim()}` : `${op}（${filename}）`,
    ontology_version: '2.1.0',
    rule_version: '1.4.2',
    function_version: '1.8.0',
    source_row_ids: [],
    chain_source: 'state',
    ...(summary ? { config_summary: summary } : {}),
  } as MockAuditEvent)
}

const mockFunctionCatalog = [
  'large_amount', 'integer_transfer_chain', 'night_call_cluster', 'track_overlap',
  'address_mismatch', 'time_window_collision', 'invoice_loop', 'relation_asserted',
]

interface MockRule {
  id: string
  stage?: string
  title?: string
  dimension?: string | string[]
  jian_types?: string[]
  enabled: boolean
  function: string
  rule_text: string
  params: Record<string, unknown>
}

const mockRules: MockRule[] = [
  {
    id: 'R1', stage: 'xu_shi', title: '季度末整数现金存入', dimension: '资金',
    jian_types: ['反间'], enabled: true, function: 'large_amount',
    rule_text: '季度末（3/6/9/12 月最后 5 个工作日）个人账户现金存入为 5 万元整数倍且单笔≥20 万元，与账户持有人申报收入水平显著不符；正常经营性现金存款多有零头、连续多笔与营业额匹配，整数大额季末突增符合现金归集、虚增流水特征，列为候选反常。',
    params: { amount_min: 200000, multiple_of: 50000, quarter_end_days: 5 },
  },
  {
    id: 'R2', stage: 'xu_shi', title: '整数转账聚合（第三方过桥结构）', dimension: '资金',
    jian_types: ['反间'], enabled: true, function: 'integer_transfer_chain',
    rule_text: '从一方到另一方的转账金额为 1 万元整数倍且金额显著大于日常收支（如百万元级），按转出方→转入方聚合后呈现单向链条（上游单位→中间方→利益关系人账户）。正常贸易往来多有非整数尾款、双向对冲与发票背景；整数大额单向流转符合第三方过桥、资金洗白的结构特征，列为候选反常。',
    params: { amount_min: 1000000, multiple_of: 10000, chain_min_hops: 2 },
  },
  {
    id: 'R3', stage: 'xu_shi', title: '深夜通话聚集', dimension: '通讯',
    jian_types: ['因间'], enabled: true, function: 'night_call_cluster',
    rule_text: '主体在 23:00-次日 5:00 的通话次数按日聚合，显著高于其近 90 日同时段基线（如日均 3 倍以上）且连续 3 日以上；正常夜间通话稀疏且对象稳定，密度突增符合作案前联络协调、对串供的行为特征，列为候选反常。应急职业（医护/物流）基线单列排除。',
    params: { night_start: 23, night_end: 5, baseline_days: 90, ratio_min: 3.0, streak_days: 3 },
  },
  {
    id: 'R6', stage: 'gu_shi', title: '时间窗碰撞（资金+通讯+轨迹）', dimension: ['资金', '通讯', '时间'],
    jian_types: ['生间', '因间'], enabled: true, function: 'time_window_collision',
    rule_text: '资金转出后短时间窗（默认 60 分钟）内，付款方与收款方之间存在通话记录或双方在同一基站 500 米范围同现；三类事件在时间轴上两两碰撞且独立来源，符合资金操作即时联络/见面确认的行为结构，列为候选反常。时间窗排除公司公开办公地址同现。',
    params: { window_minutes: 60, co_location_radius_m: 500, require_two_channels: true },
  },
  {
    id: 'R9', stage: 'xu_shi', title: '知识包关系断言落地核查（示例停用）', dimension: '关系',
    jian_types: ['内间'], enabled: false, function: 'relation_asserted',
    rule_text: '知识包中声明的利益/亲属关系断言（如法定代表人、股东、配偶），在资金/通讯/轨迹数据中无任何支撑事件（无往来转账、无通话、无轨迹重合）超过 180 天；已声明的密切关系长期零接触可能意味着关系人刻意规避或声明失实，列为待核实提示。本规则为示例，默认停用。',
    params: { silent_days: 180 },
  },
]

const mockObjects = [
  { name: 'person', title: '自然人', pk: 'person_id', kind: 'entity', name_property: 'raw_name', jian: '生间', properties: { person_id: 'string', raw_name: 'string', id_type: 'string', id_masked: 'string' } },
  { name: 'org', title: '组织/企业', pk: 'org_id', kind: 'entity', name_property: 'raw_name', jian: '生间', properties: { org_id: 'string', raw_name: 'string', credit_code: 'string', legal_rep: 'string', status: 'string' } },
  { name: 'account', title: '资金账户', pk: 'account_id', kind: 'entity', name_property: 'account_no_masked', jian: '反间', properties: { account_id: 'string', account_no_masked: 'string', bank: 'string', holder_raw: 'string' } },
  { name: 'transaction', title: '交易流水', pk: 'txn_id', kind: 'event', name_property: 'from_raw', jian: '反间', jian_source: '银行流水', properties: { txn_id: 'string', from_raw: 'string', to_raw: 'string', amount: 'decimal', date: 'date', channel: 'string' } },
  { name: 'call', title: '通话记录', pk: 'call_id', kind: 'event', name_property: 'caller_raw', jian: '生间', jian_source: '通话记录', properties: { call_id: 'string', caller_raw: 'string', callee_raw: 'string', start_at: 'date', duration_sec: 'integer', base_station: 'string' } },
  { name: 'trackpoint', title: '轨迹点', pk: 'track_id', kind: 'event', name_property: 'person_raw', jian: '因间', jian_source: '轨迹出行', properties: { track_id: 'string', person_raw: 'string', date: 'date', location: 'string', lng: 'decimal', lat: 'decimal' } },
  { name: 'clue', title: '线索', pk: 'clue_id', kind: 'entity', name_property: 'title', jian: '生间', properties: { clue_id: 'string', title: 'string', status: 'string', level: 'string', last_operator: 'string', updated_at: 'date' } },
]

const mockLinks = [
  { name: 'transfers', title: '转账关系', from_obj: 'account', to_obj: 'account', jian: '反间' },
  { name: 'owns', title: '持有账户', from_obj: 'person', to_obj: 'account', jian: '反间' },
  { name: 'calls_to', title: '通话联系', from_obj: 'person', to_obj: 'person', jian: '生间' },
  { name: 'co_located', title: '同现关系', from_obj: 'person', to_obj: 'person', jian: '因间' },
  { name: 'time_window', title: '时间窗碰撞', from_obj: 'transaction', to_obj: 'call', jian: '生间' },
  { name: 'involved_in', title: '涉案关系', from_obj: 'person', to_obj: 'clue', jian: '生间' },
]

const mockPolicies = {
  object_policies: [
    { object: 'person', roles: ['见习', '正兵', '偏将', '主办', 'human'], min_clearance: 0 },
    { object: 'org', roles: ['见习', '正兵', '偏将', '主办', 'human'], min_clearance: 0 },
    { object: 'account', roles: ['见习', '正兵', '偏将', '主办', 'human'], min_clearance: 0 },
    { object: 'transaction', roles: ['正兵', '偏将', '主办', 'human'], min_clearance: 1 },
    { object: 'call', roles: ['正兵', '偏将', '主办', 'human'], min_clearance: 1 },
    { object: 'trackpoint', roles: ['偏将', '主办', 'human'], min_clearance: 2 },
    { object: 'clue', roles: ['见习', '正兵', '偏将', '主办', 'human'], min_clearance: 0 },
  ],
  link_policies: [
    { link: 'transfers', roles: ['正兵', '偏将', '主办', 'human'], min_clearance: 1 },
    { link: 'calls_to', roles: ['正兵', '偏将', '主办', 'human'], min_clearance: 1 },
    { link: 'co_located', roles: ['偏将', '主办', 'human'], min_clearance: 2 },
  ],
  property_policies: [
    { object: 'person', property: 'id_masked', default: 'allow', mask: 'partial' },
    { object: 'account', property: 'account_no_masked', default: 'allow', mask: 'partial' },
    { object: 'org', property: 'legal_rep', default: 'allow', mask: 'none' },
    { object: 'call', property: 'base_station', default: 'deny', allow_roles: ['偏将', '主办', 'human'], mask: 'none' },
  ],
}

const mockViews = [
  { name: 'person_directory', base_object: 'person', properties: ['person_id', 'raw_name'], roles: ['见习', '正兵', '偏将', '主办', 'human', 'system'], description: '人员花名册视图：仅暴露代理键与姓名' },
  { name: 'org_overview', base_object: 'org', properties: ['org_id', 'raw_name', 'status'], roles: ['见习', '正兵', '偏将', '主办', 'human', 'system'], description: '组织概览视图：去掉法人等需进一步核实的字段' },
  { name: 'transaction_audit', base_object: 'transaction', properties: ['txn_id', 'from_raw', 'to_raw', 'amount', 'date'], roles: ['正兵', '偏将', '主办', 'human', 'system'], description: '资金流水审计视图：跨案件对账与溯源' },
  { name: 'trackpoint_minimal', base_object: 'trackpoint', properties: ['track_id', 'person_raw', 'date'], roles: ['偏将', '主办', 'human', 'system'], description: '轨迹最小视图：行踪信息严格授权' },
]

const mockKnowledge: {
  subject_aliases: Record<string, string[]>
  relation_assertions: Array<{ from: string; to: string; type: string; source?: string; valid_until: string | null }>
} = {
  subject_aliases: {
    蓝海贸易有限公司: ['蓝海贸易', '蓝海贸易（上海）', '上海蓝海贸易有限公司'],
    张某: ['张某', '张卫国', '老张'],
    李某: ['李某', '李志强'],
  },
  relation_assertions: [
    { from: '蓝海贸易有限公司', to: '张某', type: 'interest', source: '招投标档案', valid_until: null },
    { from: '临港仓储服务有限公司', to: '李某', type: 'legal_rep', source: '工商内档', valid_until: null },
    { from: '宁波蓝海商贸', to: '陈某', type: 'legal_rep', source: '工商注册样本', valid_until: null },
    { from: '旧关联公司', to: '旧关联人', type: 'interest', source: '已过期登记', valid_until: '2019-12-31' },
  ],
}

const mockDataElements = {
  schema_version: 2,
  pack: 'default',
  elements: {
    DE_ID_TYPE: { name: '证件类型', type: 'string', sensitive: false, enum: ['居民身份证', '护照', '军官证', '港澳居民来往内地通行证'], enum_space_dim: '证件类型' },
    DE_ID_NO: { name: '证件号码', type: 'string', length: 18, sensitive: true, mask: 'partial', format: '^[0-9X]{15,18}$' },
    DE_AMOUNT_YUAN: { name: '金额（元）', type: 'decimal', sensitive: false },
    DE_PERSON_NAME: { name: '自然人姓名', type: 'string', length: 50, sensitive: false },
    DE_ORG_NAME: { name: '机构名称', type: 'string', length: 120, sensitive: false },
    DE_PHONE: { name: '手机号码', type: 'string', length: 11, sensitive: true, mask: 'partial', format: '^1[3-9][0-9]{9}$' },
    DE_BANK_ACCOUNT: { name: '银行账号', type: 'string', length: 32, sensitive: true, mask: 'partial' },
    DE_TXN_DATE: { name: '交易日期', type: 'date', sensitive: false, format: 'YYYY-MM-DD' },
  },
}

const mockEtlDrafts: Array<Record<string, unknown>> = []
const mockEtlSources = [
  {
    object: 'transaction', source_table: '银行流水',
    clean: ['trim_whitespace', 'normalize_amount'],
    on_cast_error: { amount: 'quarantine' },
    null_policy: { from_raw: 'reject', to_raw: 'reject', amount: 'quarantine' },
    dedup_key: ['txn_id'], dedup_on_conflict: 'keep_latest',
    composite_props: [
      { prop: 'amount', source_sql: 'COALESCE(转账金额, 现存金额, 手工录入金额)', reason: '三列合一的复合表达式，无法逐列 CAST 与映射' },
    ],
  },
  {
    object: 'call', source_table: '通话记录',
    clean: ['trim_whitespace'],
    on_cast_error: { duration_sec: 'fail' },
    null_policy: { caller_raw: 'quarantine', callee_raw: 'quarantine' },
    dedup_key: ['call_id'], dedup_on_conflict: 'keep_latest',
    composite_props: [],
  },
]

const mockQualityReport = {
  available: true,
  check_id: 'QC-20260908-1000',
  created_at: '2026-09-08 10:00:00',
  created_by: '王检察官',
  data_version: 3,
  summary: { total: 8, passed: 4, warnings: 2, violations: 2 },
  checks: [
    { category: 'compliance', mode: 'deterministic', rule_id: 'R6', obj: 'transaction', prop: 'amount', severity: 'block', count: 12, message: '12 行金额声明 decimal 但源值无法转换（中文大写/手工录入），按 fail 策略应阻断装载', samples_masked: ['¥ 肆拾捌万元整', '480,000.00 元'] },
    { category: 'sensitive', mode: 'deterministic', rule_id: undefined, obj: 'person', prop: 'id_masked', severity: 'block', count: 1, message: '证件号码列未在 property_policies 声明遮蔽策略，敏感面裸奔', samples_masked: [] },
    { category: 'freshness', mode: 'deterministic', rule_id: undefined, obj: 'call', prop: 'start_at', severity: 'warn', count: 47, message: '通话记录最新数据停留在 2026-08-01，距今 38 天未更新（阈值 30 天）', samples_masked: [] },
    { category: 'unit', mode: 'heuristic', rule_id: undefined, obj: 'transaction', prop: 'amount', severity: 'suggest', count: 6, message: '金额疑似混入「万元」单位记录（数值分布出现两个量级），建议人工核对单位口径', samples_masked: ['48.00（万元？）', '50.00（万元？）'] },
    { category: 'compliance', mode: 'deterministic', rule_id: undefined, obj: 'trackpoint', prop: 'lng', severity: 'ok', count: 0, message: '经纬度取值范围合法', samples_masked: [] },
  ],
}

const mockQuarantine = [
  { object: 'transaction', property: 'amount', rule: 'cast:decimal', src_column: '转账金额', reason: 'cast_error', source_table: '银行流水', samples_masked: ['¥ 肆拾捌万元整', '480,000.00 元（手工录入）'], name_value: '张某', quarantined_at: '2026-09-08 09:58:11' },
  { object: 'transaction', property: 'amount', rule: 'cast:decimal', src_column: '现存金额', reason: 'cast_error', source_table: '银行流水', samples_masked: ['约伍拾万元', '现金 50 万'], name_value: '李某', quarantined_at: '2026-09-08 09:58:12' },
  { object: 'call', property: 'caller_raw', rule: 'not_null', src_column: '主叫号码', reason: 'null_value', source_table: '通话记录', samples_masked: ['（空）'], name_value: '', quarantined_at: '2026-09-08 09:59:03' },
]

const mockCleanTrace = [
  { object: 'transaction', property: 'amount', rules: ['cast:decimal', 'dedup:txn_id'], rows_before: 1203, rows_after: 1189, dropped_rows: 14, rate: 0.0116, samples_masked: ['¥ 肆拾捌万元整'], source: 'build', created_at: '2026-09-08 10:00:00' },
  { object: 'call', property: 'caller_raw', rules: ['not_null'], rows_before: 3420, rows_after: 3417, dropped_rows: 3, rate: 0.0009, samples_masked: ['（空）'], source: 'build', created_at: '2026-09-08 10:00:00' },
]

// ---------- MVP-5 mock 状态（门户/跨案/案件包/逃生舱/设置）----------

/**
 * 演示开关（仅 mock 管道）：
 * - sessionStorage 'sunzi.mock.admin'='1' → 管理员（可编辑平台设置）；
 *   默认非管理员，进设置页看到 fail-closed 锁定面板（B2）。
 * - 建案/归档/跨案等写入操作的审计留痕（reason 必填）与后端同构。
 */
function isMockAdmin(): boolean {
  try {
    return sessionStorage.getItem('sunzi.mock.admin') === '1'
  } catch {
    return false
  }
}

const mvp5Cases = [
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
    name: '演示案件·临港仓储（待建案）',
    status: '待建案',
    pack_id: 'default',
    pack_snapshot_at: '',
    created_at: '2026-09-07T09:00:00',
    created_by: '王检察官',
  },
]

const crossHistory: Array<{
  id: number
  ts: string
  case_ids: string[]
  sql: string
  reason: string
  result_rows: number
}> = [
  {
    id: 1,
    ts: '2026-09-08T15:02:00',
    case_ids: ['c1', 'c2'],
    sql: "SELECT 'c1' AS source_case, t.* FROM case_c1.obj_person t UNION ALL SELECT 'c2' AS source_case, t.* FROM case_c2.obj_person t",
    reason: '并案排查：两案人员是否存在重合主体',
    result_rows: 4,
  },
]
let crossHistorySeq = crossHistory.length

const HATCH_TEMPLATES: Record<string, { path: string; content: string; points: string[] }> = {
  function: {
    path: 'ontology/default/functions.json (+core/functions.py)',
    content:
      '// functions.json 声明片段：\n' +
      '{\n  "name": "{name}",\n  "kind": "sql",\n  "description": "{desc}",\n  "parameters": [],\n  "sql": "SELECT * FROM obj_person LIMIT 100"\n}\n' +
      '# core/functions.py：在 FUNCTION_IMPLS 注册同名实现（只读）。',
    points: ['ontology/default/functions.json 加声明', 'core/functions.py FUNCTION_IMPLS 注册', 'rules.json 以 function 名挂钩'],
  },
  value_type: {
    path: 'ontology/default/objects.json (+TYPE_SQL)',
    content:
      '// objects.json properties 增加类型：\n' +
      '"properties": { "{name}": "{desc}" }\n' +
      '# 值类型经 core/ontology_loader.py TYPE_SQL 驱动物化列类型；脏值 TRY_CAST 降级 NULL + 诊断。',
    points: ['ontology/default/objects.json properties 声明', 'bindings.json source 列别名对齐', '重建语义层生效'],
  },
  clean_rule: {
    path: 'ontology/default/bindings.json (+core/clean_rules.py)',
    content:
      '// bindings.json clean 引用清洗规则：\n' +
      '"clean": ["{name}"]\n' +
      '# core/clean_rules.py 注册同名纯函数（输入行 dict，输出行 dict）。\n// 规则说明：{desc}',
    points: ['core/clean_rules.py 注册规则函数', 'ontology/default/bindings.json clean 引用', '重建语义层生效'],
  },
  side_effect: {
    path: 'ontology/default/actions.json (+core/action_executor.py)',
    content:
      '// actions.json 声明写动作：\n' +
      '{\n  "name": "{name}",\n  "description": "{desc}",\n  "roles": ["human"],\n  "required_params": ["reason"],\n  "transitions": []\n}\n' +
      '# core/action_executor.py 注册副作用实现（唯一写路径）。',
    points: ['ontology/default/actions.json 加声明', 'core/action_executor.py 注册副作用', 'policies.json 同步声明策略（漏了 fail-closed）'],
  },
}

const hatchStats: Record<string, number> = { function: 12, value_type: 3, clean_rule: 5, side_effect: 2 }

const mockSettings = {
  queue: { max_workers: 2, poll_interval_ms: 2000 },
  resources: { max_rows_default: 10000, query_timeout_ms: 30000 },
  thresholds: { cross_level_min_sources: 3, cross_level_min_clues: 2, stale_days: 30 },
  features: { ui_density: 'comfortable' as 'comfortable' | 'compact' },
}

const SETTINGS_SCOPE_KEY: Record<string, string> = {
  queue: 'queue',
  resources: 'resources',
  policies_thresholds: 'policies_thresholds',
  features: 'features',
}

async function putSettings(
  request: Request,
  scope: 'queue' | 'resources' | 'policies_thresholds' | 'features',
  allowedKeys: string[],
  target: Record<string, unknown>,
): Promise<Response> {
  if (!isMockAdmin()) return fail('FORBIDDEN', '平台设置仅管理员可操作', 403)
  const body = (await request.json().catch(() => ({}))) as { reason?: string; values?: Record<string, unknown> }
  if (!body.reason?.trim()) return fail('VALIDATION', '修改平台设置必须填写原因（审计留痕）', 400)
  const values = body.values ?? {}
  const unknown = Object.keys(values).filter((k) => !allowedKeys.includes(k))
  if (unknown.length) return fail('VALIDATION', `不允许修改的键：${unknown.join('、')}（红线键平台硬拒）`, 400)
  for (const [k, v] of Object.entries(values)) {
    if (v === null || v === undefined) continue
    if (scope === 'features' && k === 'ui_density' && !['comfortable', 'compact'].includes(String(v))) {
      return fail('VALIDATION', 'ui_density 仅允许 comfortable/compact', 400)
    }
    target[k] = v
  }
  const key = SETTINGS_SCOPE_KEY[scope]
  const payload: Record<string, unknown> = { [key]: { ...target } }
  if (scope === 'resources') payload.resources = { ...target, storage_root: '/data/sunzi/cases' }
  if (scope === 'policies_thresholds') {
    payload.note = '平台值仅用于新建案件默认；案件生效阈值以案件快照 thresholds.json 为准'
  }
  return ok(payload)
}
