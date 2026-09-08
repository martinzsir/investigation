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
      updated_at: '2026-09-08 11:05:33',
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
      updated_at: '2026-09-08 08:15:00',
      status_source: 'artifact',
      suppressed_log: [],
      source_rows: [],
      evidence: [
        { id: 'p1', kind: 'pending', text: '张/李/王三人 8 月内 4 个周末轨迹在临港仓储点 500 米范围内重合，待实地核验。' },
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
          total: 5,
          by_status: { 待查: 2, 查证中: 1, 已排除: 1, 已固证: 1, 已立案: 0 },
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
]
