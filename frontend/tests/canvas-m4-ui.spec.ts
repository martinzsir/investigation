import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import type { RawResponse } from '../src/api/transport/types'
import { FakeTransport, okEnvelope } from './helpers'
import ResearchCanvas from '../src/components/research/ResearchCanvas.vue'
import {
  validateFunctionParams,
  type CanvasDoc,
  type CanvasEnvelope,
  type CanvasNode,
  type FunctionForm,
  type FunctionResultDetail,
} from '../src/domain/canvas'

// M4 画布交互（RC-105 / RC-204）：
//  - 规则节点生成手册建议（pb 虚节点，G6 虚线标记；无新增提示）；
//  - 采纳 202 → waitForTerminal 终态 → sync 协调；终态失败标红且无 sync
//    （无假成功），改写文本重试产生新任务；sync pending 保持建议态；
//  - 人工假设转待核实：确认弹窗 → 202 → 终态 → created 新节点；
//  - 扩展查询：白名单目录 + 参数表单 + 结果节点（预览/入参快照/查询自）；
//    DATASOURCE_UNAVAILABLE 不落节点、不刷新画布。

const PATH = '/cases/c1/clues/clue-1/canvas'
const PB_ID = 'verify_item:pb:pb1'
const VI_ID = 'verify_item:vi_x'
const FR_ID = 'function_result:fr1'

const mocks = vi.hoisted(() => ({
  configs: [] as any[],
  handlers: {} as Record<string, (ev: unknown) => void>,
  positions: new Map<string, [number, number]>(),
  sets: [] as any[],
}))

vi.mock('@antv/g6', () => ({
  Graph: class {
    constructor(cfg: unknown) {
      mocks.configs.push(cfg)
    }
    async render() {}
    setData(d: unknown) {
      mocks.sets.push(d)
    }
    destroy() {}
    on(event: string, cb: (ev: unknown) => void) {
      mocks.handlers[event] = cb
    }
    off() {}
    fitView() {}
    zoomTo() {}
    getZoom() {
      return 1
    }
    getElementPosition(id: string) {
      return mocks.positions.get(id) ?? [0, 0]
    }
    setElementState() {}
  },
  // g6-card-node 静态 import 所需命名导出
  register() {},
  ExtensionCategory: { NODE: 'node' },
  Badge: class {},
  Label: class {},
  Rect: class {
    static defaultStyleProps = {}
  },
}))

function n(
  id: string,
  kind: CanvasNode['kind'],
  label: string,
  x: number,
  y: number,
  extra: Partial<CanvasNode> = {},
): CanvasNode {
  return {
    id, kind, ref: extra.ref ?? '', label, system: extra.system ?? true,
    adopted: false, stale: false, pinned: extra.pinned ?? false, x, y,
    props: extra.props ?? {}, ...extra,
  }
}

function pbNode(): CanvasNode {
  return n(PB_ID, 'verify_item', '建议：核实整数转账', 720, 0, {
    ref: 'pb:pb1',
    adopted: false,
    props: {
      text: '核实整数转账规律',
      channel: 'function',
      ref_function: 'time_window_collision',
      falsification: '多主体无重合即为查否',
    },
  })
}

function viNode(): CanvasNode {
  return n(VI_ID, 'verify_item', '核查项：核实整数转账', 720, 0, {
    ref: 'vi_x',
    adopted: true,
    props: {
      text: '核实整数转账规律',
      status: '待核查',
      channel: 'function',
    },
  })
}

function frNode(): CanvasNode {
  return n(FR_ID, 'function_result', '整数转账聚合结果', 260, 208, {
    props: {
      executed_at: '2026-09-13 09:00',
      executed_by: '李侦查员',
      params: { round_unit: 10000 },
    },
  })
}

function frDetail(): FunctionResultDetail {
  return {
    kind: 'function_result',
    function: 'integer_transfer_aggregates',
    function_title: '整数转账聚合',
    params: { round_unit: 10000 },
    executed_at: '2026-09-13 09:00',
    executed_by: '李侦查员',
    output_type: 'rows',
    summary: {
      kind: 'rows',
      row_count: 1,
      columns: ['资金主体', '金额'],
      preview_rows: [{ 资金主体: '张卫国', 金额: 100000 }],
    },
    source_node_ids: ['rule:R1'],
    input_tables: ['obj_transaction'],
  }
}

function seedDoc(extra: CanvasNode[] = []): CanvasDoc {
  return {
    nodes: [
      n('rule:R1', 'rule', '规则R1', 0, 0, { ref: 'R1' }),
      n('fact:f1', 'fact', '事实一', 260, 0, { ref: 'fact:f1' }),
      n('cn_1', 'hypothesis', '假设一', 480, 104, {
        system: false,
        props: { title: '假设一', content: '多账户过渡' },
      }),
      ...extra,
    ],
    edges: [],
  }
}

function envelope(over: Partial<CanvasEnvelope> = {}): CanvasEnvelope {
  return {
    canvas_id: 'cv_1',
    clue_id: 'clue-1',
    version: 1,
    created_by: '李侦查员',
    created_at: '',
    updated_by: '',
    updated_at: '',
    seeded: false,
    semantic_ready: true,
    doc: seedDoc(),
    ...over,
  }
}

function task(id: string, status: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    case_id: 'c1',
    task_type: 'TASK_VERIFY',
    params: {},
    status,
    progress_pct: 0,
    progress_stage: '',
    progress_label: '',
    progress_detail: '',
    retry_count: 0,
    max_retries: 3,
    idem_key: '',
    created_at: '',
    updated_at: '',
    started_at: '',
    finished_at: '',
    error_code: '',
    error_message: '',
    created_by: '李侦查员',
    ...extra,
  }
}

function accepted202(data: unknown): RawResponse {
  return { status: 202, data: { ok: true, data } }
}

const CATALOG = {
  functions: [
    {
      name: 'integer_transfer_aggregates',
      title: '整数转账聚合',
      description: '按取整单位聚合疑似整数化转账',
      output_type: 'rows',
      params: [
        {
          key: 'round_unit', label: '取整单位', type: 'integer',
          enum: null, default: 10000, required: true,
        },
      ],
    },
    {
      name: 'time_window_collision',
      title: '时间窗碰撞',
      description: '多主体时间窗重合检测',
      output_type: 'report',
      params: [
        {
          key: 'round_unit', label: '取整单位', type: 'integer',
          enum: null, default: null, required: true,
        },
        {
          key: 'exclude_org_suffix', label: '排除机构后缀', type: 'string',
          enum: ['公司'], default: '公司', required: false,
        },
      ],
    },
  ] satisfies FunctionForm[],
  available: true,
}

let wrapper: VueWrapper | null = null

function mountWith(t: FakeTransport): VueWrapper {
  setTransport(t)
  wrapper = mount({
    components: { NMessageProvider, NDialogProvider, ResearchCanvas },
    template: `
      <NMessageProvider><NDialogProvider>
        <ResearchCanvas case-id="c1" clue-id="clue-1" />
      </NDialogProvider></NMessageProvider>`,
  })
  return wrapper
}

function setValue(el: Element | null, value: string): void {
  const target = el as HTMLInputElement | HTMLTextAreaElement | null
  if (!target) throw new Error('input element not found')
  target.value = value
  target.dispatchEvent(new Event('input', { bubbles: true }))
}

function clickBody(testid: string): void {
  const el = document.body.querySelector(`[data-testid="${testid}"]`)
  if (!el) throw new Error(`element not found: ${testid}`)
  ;(el as HTMLButtonElement).click()
}

async function openNode(id: string): Promise<void> {
  // 单击有 250ms 延迟（区分双击展开/折叠）
  mocks.handlers['node:click']({ target: { id } })
  await new Promise((r) => setTimeout(r, 280))
  await flushPromises()
}

const getRoute = [
  {
    match: (r: { method: string; path: string }) =>
      r.method === 'GET' && r.path === PATH,
    respond: () => okEnvelope(envelope()),
  },
]

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  mocks.configs.length = 0
  mocks.sets.length = 0
  Object.keys(mocks.handlers).forEach((k) => delete mocks.handlers[k])
  mocks.positions.clear()
  setTransport(null)
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('RC-105 生成手册核实建议', () => {
  it('POST suggestions adds dashed pb nodes; G6 data carries suggestion flag', async () => {
    const pb = pbNode()
    const docV2: CanvasDoc = {
      nodes: [...seedDoc().nodes, pb],
      edges: [
        { id: 'e_pb', source: 'rule:R1', target: PB_ID,
          rel: '手册建议', system: true },
      ],
    }
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/suggestions`,
        respond: (req) => {
          const body = req.body as Record<string, unknown>
          return okEnvelope({
            ...envelope({ version: 2, doc: docV2 }),
            added_nodes: [pb],
            added_edges: docV2.edges,
            skipped: [],
            echo_node: body.node_id,
            echo_version: body.version,
          })
        },
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode('rule:R1')

    clickBody('drawer-gen-suggestions')
    await flushPromises()

    const call = t.calls.find(
      (c) => c.method === 'POST' && c.path === `${PATH}/suggestions`)!
    expect(call.body).toMatchObject({ node_id: 'rule:R1', version: 1 })

    const set = mocks.sets.at(-1) as { nodes: Array<Record<string, unknown>> }
    const pbData = set.nodes.find((x) => x.id === PB_ID)!
    expect(pbData.data).toMatchObject({ suggestion: true })
    expect(document.body.textContent).toContain('已生成 1 条手册核实建议')
    w.unmount()
  })

  it('no new suggestions shows empty hint without version bump', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/suggestions`,
        respond: () => okEnvelope({
          ...envelope({ version: 1 }),
          added_nodes: [],
          added_edges: [],
          skipped: [{ playbook_id: 'pb_x', reason: '无匹配' }],
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode('rule:R1')

    clickBody('drawer-gen-suggestions')
    await flushPromises()

    expect(document.body.textContent).toContain(
      '手册暂无该规则的核实建议，可手工添加假设')
    expect(document.body.querySelector('[data-testid="suggestion-empty"]')).toBeTruthy()
    w.unmount()
  })

  it('idempotent dedup shows already-on-canvas hint, not empty hint', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/suggestions`,
        respond: () => okEnvelope({
          ...envelope({ version: 1 }),
          added_nodes: [],
          added_edges: [],
          skipped: [
            { playbook_id: 'pb_r6_a', reason: '画布已存在该建议' },
            { playbook_id: 'pb_r6_b', reason: '画布已存在同文本核查节点' },
          ],
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode('rule:R1')

    clickBody('drawer-gen-suggestions')
    await flushPromises()

    expect(document.body.textContent).toContain(
      '手册建议已在画布上（2 条虚线节点），可直接采纳，无需重复生成')
    expect(document.body.querySelector('[data-testid="suggestion-added"]')).toBeTruthy()
    expect(document.body.querySelector('[data-testid="suggestion-empty"]')).toBeNull()
    w.unmount()
  })
})

describe('RC-105 采纳：202 终态协调', () => {
  it('adopt 202 waits terminal SUCCEEDED then sync migrates pb to vi', async () => {
    const docV1 = seedDoc([pbNode()])
    const docV3: CanvasDoc = {
      nodes: [...seedDoc().nodes, viNode()],
      edges: [],
    }
    const taskMap: Record<string, ReturnType<typeof task>> = {
      t1: task('t1', 'SUCCEEDED'),
    }
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope({ doc: docV1 })),
      },
      {
        match: (r) => r.method === 'POST' && r.path.endsWith('/adopt'),
        respond: (req) => {
          const body = req.body as Record<string, unknown>
          return accepted202({
            task: task('t1', 'PENDING'),
            mode: 'add_manual',
            node_id: PB_ID,
            effective_text: '核实整数转账规律',
            echo_version: body.version,
          })
        },
      },
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/tasks/'),
        respond: (req) =>
          okEnvelope(taskMap[req.path.split('/').pop()!]),
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/suggestions/sync`,
        respond: (req) => {
          const body = req.body as {
            targets: Array<{ node_id: string; text?: string }>
            version: number
          }
          return okEnvelope({
            ...envelope({ version: 3, doc: docV3 }),
            results: [{
              node_id: PB_ID, status: 'adopted',
              item_id: 'vi_x', new_node_id: VI_ID,
            }],
            echo_targets: body.targets,
            echo_version: body.version,
          })
        },
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode(PB_ID)

    expect(document.body.querySelector('[data-testid="suggestion-block"]')).toBeTruthy()
    clickBody('drawer-adopt-suggestion')
    await flushPromises()

    const adoptCall = t.calls.find((c) => c.method === 'POST'
      && c.path.endsWith('/adopt'))!
    expect(adoptCall.body).toMatchObject({ version: 1 })
    // 未改写文本不携带 text（transition/add_manual 空覆盖幂等口径）
    expect((adoptCall.body as Record<string, unknown>).text).toBeUndefined()
    // 202 后必须轮询任务终态，再发 sync
    expect(t.calls.some((c) => c.method === 'GET' && c.path === '/tasks/t1')).toBe(true)

    const syncCall = t.calls.find((c) => c.method === 'POST'
      && c.path === `${PATH}/suggestions/sync`)!
    expect((syncCall.body as any).targets).toEqual([
      { node_id: PB_ID, text: '核实整数转账规律' },
    ])

    // 画布迁移：pb 消失、vi 出现且不再是建议虚线
    const set = mocks.sets.at(-1) as { nodes: Array<Record<string, unknown>> }
    expect(set.nodes.find((x) => x.id === PB_ID)).toBeFalsy()
    const vi = set.nodes.find((x) => x.id === VI_ID)!
    expect(
      (vi.data as { suggestion?: boolean } | undefined)?.suggestion,
    ).toBeFalsy()
    // 抽屉跟随到已采纳核查项
    expect(document.body.textContent).toContain('已采纳核查项')
    expect(document.body.querySelector('[data-testid="suggestion-block"]')).toBeFalsy()
    w.unmount()
  })

  it('terminal FAILED shows red error, no sync; rewrite text creates new task', async () => {
    const docV1 = seedDoc([pbNode()])
    const docV3: CanvasDoc = {
      nodes: [...seedDoc().nodes, viNode()],
      edges: [],
    }
    const taskMap: Record<string, ReturnType<typeof task>> = {
      t1: task('t1', 'FAILED', {
        error_code: 'NO_VERSION',
        error_message: '案件数据版本未就绪',
      }),
      t2: task('t2', 'SUCCEEDED'),
    }
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope({ doc: docV1 })),
      },
      {
        match: (r) => r.method === 'POST' && r.path.endsWith('/adopt'),
        respond: (req) => {
          const body = req.body as Record<string, unknown>
          const tid = body.text ? 't2' : 't1'
          return accepted202({
            task: task(tid, 'PENDING'),
            mode: 'add_manual',
            node_id: PB_ID,
            effective_text: String(body.text || '核实整数转账规律'),
          })
        },
      },
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/tasks/'),
        respond: (req) =>
          okEnvelope(taskMap[req.path.split('/').pop()!]),
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/suggestions/sync`,
        respond: () => okEnvelope({
          ...envelope({ version: 3, doc: docV3 }),
          results: [{
            node_id: PB_ID, status: 'adopted',
            item_id: 'vi_x', new_node_id: VI_ID,
          }],
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode(PB_ID)

    // 第一次采纳：终态 FAILED
    clickBody('drawer-adopt-suggestion')
    await flushPromises()
    const failed = document.body.querySelector('[data-testid="adopt-failed"]')
    expect(failed).toBeTruthy()
    expect(failed!.textContent).toContain('案件数据版本未就绪')
    // 无假成功：失败时绝不发起 sync
    expect(t.calls.some((c) => c.method === 'POST'
      && c.path === `${PATH}/suggestions/sync`)).toBe(false)

    // 改写文本 → 重试 → 新 idem 新任务 t2
    setValue(document.body.querySelector(
      '[data-testid="adopt-text-input"] textarea'), '核实整数转账规律（人工改写）')
    await flushPromises()
    clickBody('drawer-retry-adopt')
    await flushPromises()

    const adopts = t.calls.filter((c) => c.method === 'POST'
      && c.path.endsWith('/adopt'))
    expect(adopts).toHaveLength(2)
    expect((adopts[1].body as any).text).toBe('核实整数转账规律（人工改写）')
    expect(t.calls.some((c) => c.method === 'GET' && c.path === '/tasks/t2')).toBe(true)
    // 成功后 sync 协调，抽屉迁移到已采纳项
    await flushPromises()
    expect(document.body.textContent).toContain('已采纳核查项')
    w.unmount()
  })

  it('sync pending keeps pb suggestion node (no false success)', async () => {
    const docV1 = seedDoc([pbNode()])
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope({ doc: docV1 })),
      },
      {
        match: (r) => r.method === 'POST' && r.path.endsWith('/adopt'),
        respond: () => accepted202({
          task: task('t1', 'PENDING'),
          mode: 'add_manual',
          node_id: PB_ID,
          effective_text: '核实整数转账规律',
        }),
      },
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/tasks/'),
        respond: () => okEnvelope(task('t1', 'SUCCEEDED')),
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/suggestions/sync`,
        respond: () => okEnvelope({
          ...envelope({ version: 1, doc: docV1 }),
          results: [{ node_id: PB_ID, status: 'pending' }],
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode(PB_ID)

    clickBody('drawer-adopt-suggestion')
    await flushPromises()

    // 任务成功但 state 未就绪：sync pending，画布保持建议态
    expect(document.body.querySelector('[data-testid="adopt-failed"]')).toBeTruthy()
    expect(document.body.textContent).toContain('画布保持建议态')
    const set = mocks.sets.at(-1) as { nodes: Array<Record<string, unknown>> }
    expect(set.nodes.find((x) => x.id === PB_ID)).toBeTruthy()
    expect(set.nodes.find((x) => x.id === VI_ID)).toBeFalsy()
    w.unmount()
  })
})

describe('RC-105 人工假设转待核实', () => {
  it('confirm modal → 202 → terminal → sync created verify node', async () => {
    const docV2: CanvasDoc = {
      nodes: [...seedDoc().nodes, n(
        'verify_item:vi_h', 'verify_item', '核查资金过桥账户', 720, 0,
        { ref: 'vi_h', adopted: true,
          props: { text: '核查资金过桥账户', status: '待核查' } })],
      edges: [],
    }
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/nodes/cn_1/to-verify`,
        respond: (req) => accepted202({
          task: task('th1', 'PENDING'),
          mode: 'add_manual',
          node_id: 'cn_1',
          effective_text: (req.body as any).text,
        }),
      },
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/tasks/'),
        respond: () => okEnvelope(task('th1', 'SUCCEEDED')),
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/suggestions/sync`,
        respond: () => okEnvelope({
          ...envelope({ version: 2, doc: docV2 }),
          results: [{
            node_id: 'cn_1', status: 'created',
            item_id: 'vi_h', new_node_id: 'verify_item:vi_h',
          }],
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await openNode('cn_1')

    clickBody('drawer-to-verify')
    await flushPromises()
    const modal = document.body.querySelector('[data-testid="to-verify-modal"]')
    expect(modal).toBeTruthy()
    // 默认带入假设标题
    expect(
      (document.body.querySelector(
        '[data-testid="to-verify-text"] textarea') as HTMLTextAreaElement).value,
    ).toBe('假设一')

    setValue(document.body.querySelector(
      '[data-testid="to-verify-text"] textarea'), '核查资金过桥账户')
    await flushPromises()
    clickBody('to-verify-submit')
    await flushPromises()

    const call = t.calls.find((c) => c.method === 'POST'
      && c.path === `${PATH}/nodes/cn_1/to-verify`)!
    expect(call.body).toMatchObject({
      text: '核查资金过桥账户',
      version: 1,
    })
    // 弹窗已关闭（tvShow=false → vShow display:none）。注：happy-dom 不派发
    // transitionend，NModal display-directive=if 的离场残留节点不会卸载，
    // 与仓库既有口径一致只断言不可见，不断言 DOM 移除。
    const modalAfter = document.body.querySelector(
      '[data-testid="to-verify-modal"]',
    )
    expect(
      modalAfter === null
        || (modalAfter as HTMLElement).style.display === 'none',
    ).toBe(true)
    expect(document.body.textContent).toContain('已生成待核实核查项')
    expect(document.body.textContent).toContain('已采纳核查项')
    w.unmount()
  })
})

describe('RC-204 扩展查询', () => {
  it('catalog submit executes whitelisted function and renders result node', async () => {
    const docV2: CanvasDoc = {
      nodes: [...seedDoc().nodes, frNode()],
      edges: [
        { id: 'e_fr', source: 'rule:R1', target: FR_ID,
          rel: '查询自', system: true },
      ],
    }
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'GET' && r.path === `${PATH}/functions`,
        respond: () => okEnvelope(CATALOG),
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/function-query`,
        respond: (req) => okEnvelope({
          ...envelope({ version: 2, doc: docV2 }),
          executed: true,
          node: frNode(),
          edge: docV2.edges[0],
          result: frDetail().summary,
          params: { round_unit: 10000 },
          echo_body: req.body,
        }),
      },
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/expand`,
        respond: () => okEnvelope({
          ...envelope({ version: 2, doc: docV2 }),
          added_nodes: [],
          added_edges: [],
          details: { [FR_ID]: frDetail() },
          notices: [],
          truncated: false,
          leaf: false,
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()

    await w.find('[data-testid="tb-function-query"]').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[data-testid="function-query-modal"]')).toBeTruthy()

    clickBody('function-query-submit')
    await flushPromises()

    const q = t.calls.find((c) => c.method === 'POST'
      && c.path === `${PATH}/function-query`)!
    expect(q.body).toMatchObject({
      function: 'integer_transfer_aggregates',
      version: 1,
      source_node_id: null,
    })
    expect((q.body as any).params).toEqual({ round_unit: 10000 })

    // 结果节点自动展开：入参快照 + 预览表 + 输入表
    const block = document.body.querySelector(
      '[data-testid="function-result-block"]')
    expect(block).toBeTruthy()
    expect(block!.textContent).toContain('张卫国')
    expect(block!.textContent).toContain('obj_transaction')

    // 「查询自」源节点 → 点击继续 RC-103 溯源，抽屉切到规则节点
    const src = document.body.querySelector('[data-testid="fr-source-btn"]')!
    expect(src.textContent).toContain('规则')
    ;(src as HTMLButtonElement).click()
    await flushPromises()
    expect(
      document.body.querySelector('[data-testid="drawer-node-label"]')!.textContent,
    ).toBe('规则R1')
    w.unmount()
  })

  it('DATASOURCE_UNAVAILABLE alerts without creating node or refreshing doc', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'GET' && r.path === `${PATH}/functions`,
        respond: () => okEnvelope(CATALOG),
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/function-query`,
        respond: () => ({
          status: 200,
          data: {
            ok: true,
            data: {
              executed: false,
              code: 'DATASOURCE_UNAVAILABLE',
              message: '版本库 v1.duckdb 不存在',
              function: 'integer_transfer_aggregates',
              params: { round_unit: 10000 },
            },
          },
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()

    await w.find('[data-testid="tb-function-query"]').trigger('click')
    await flushPromises()
    clickBody('function-query-submit')
    await flushPromises()

    const alert = document.body.querySelector(
      '[data-testid="function-datasource-unavailable"]')
    expect(alert).toBeTruthy()
    expect(alert!.textContent).toContain('数据源未接入')
    // 弹窗保留；未产生结果节点
    expect(document.body.querySelector('[data-testid="function-query-modal"]')).toBeTruthy()
    expect(document.body.querySelector('[data-testid="function-result-block"]')).toBeFalsy()
    // 没有额外拉取画布（只有首屏一次 GET canvas）
    expect(t.calls.filter((c) => c.method === 'GET' && c.path === PATH)).toHaveLength(1)
    w.unmount()
  })
})

describe('RC-204 参数校验纯函数（与后端 merge_query_params 同口径）', () => {
  const base: FunctionForm = {
    name: 'f',
    title: 'F',
    description: '',
    output_type: 'rows',
    params: [],
  }

  it('rejects missing required', () => {
    const form = {
      ...base,
      params: [{ key: 'round_unit', label: 'x', type: 'integer',
        enum: null, default: null, required: true }],
    }
    expect(validateFunctionParams(form, {}).round_unit).toBe('该参数必填')
  })

  it('rejects non-integer and boolean wrong type', () => {
    const form = {
      ...base,
      params: [
        { key: 'a', label: 'a', type: 'integer', enum: null,
          default: null, required: false },
        { key: 'b', label: 'b', type: 'boolean', enum: null,
          default: null, required: false },
      ],
    }
    const errs = validateFunctionParams(form, { a: 1.5, b: 'yes' })
    expect(errs.a).toBe('请填写整数')
    expect(errs.b).toBe('请选择是/否')
  })

  it('rejects string outside enum and free-text string params', () => {
    const form = {
      ...base,
      params: [
        { key: 'a', label: 'a', type: 'string', enum: ['公司'],
          default: '公司', required: false },
        { key: 'b', label: 'b', type: 'string', enum: null,
          default: null, required: false },
      ],
    }
    const errs = validateFunctionParams(form, { a: '个人', b: '任意文本' })
    expect(errs.a).toBe('取值不在允许范围内')
    expect(errs.b).toBe('该参数不支持自由文本')
  })

  it('passes whitelisted values', () => {
    const form = {
      ...base,
      params: [
        { key: 'a', label: 'a', type: 'integer', enum: null,
          default: 10000, required: true },
        { key: 'b', label: 'b', type: 'string', enum: ['公司'],
          default: '公司', required: false },
      ],
    }
    expect(validateFunctionParams(form, { a: 5000, b: '公司' })).toEqual({})
  })
})
