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
  action: 'disposal' | 'proposal' | 'parameter_set' | 'generic'
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
    })
  }),

  http.post('*/api/v1/auth/logout', () => ok({ revoked: true })),

  http.get('*/api/v1/auth/me', () =>
    ok({ operator: '王检察官', role: 'human', clearance: 4, tenant_id: 't1' }),
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
        name: '演示案件·临港仓储（待建案）',
        status: '待建案',
        pack_id: 'default',
        pack_snapshot_at: '',
        created_at: '2026-09-07T09:00:00',
        created_by: '王检察官',
      },
    ]),
  ),

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
      detail: { evidence: clue.evidence },
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
        const stages: Array<[number, string, string, string]> = isImport
          ? [
              [20, 'parse', '解析数据', t.params.filename ? String(t.params.filename) : '读取上传件'],
              [60, 'cold', '写冷层 parquet', String(t.params.target_table ?? '银行流水')],
              [100, 'done', '导入完成', `${t.params.target_table ?? '银行流水'} 1203 行 → BUILD 已入队`],
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
