import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope, errEnvelope } from './helpers'
import ResearchCanvas from '../src/components/research/ResearchCanvas.vue'
import type { CanvasDoc, CanvasEnvelope, ExpandEnvelope } from '../src/domain/canvas'

// RC-102/103/104 画布 M2（UX P0 后折叠可见性迁 src/domain/canvas-view.ts，
// 纯函数测试见 canvas-view.spec.ts）：
//  - 规则业务视图不含技术标识；审计视图按需装载 function/params + 工坊外链；
//  - fact expand 合并新节点边、双击/抽屉折叠仅前端投影隐藏；
//  - 数据行 denied 字段只渲染 ****（RC-104，不接触真值）；
//  - 展开失败可重试（单层失败不破坏已渲染层）。

const PATH = '/cases/c1/clues/clue-1/canvas'
const EXPAND = PATH + '/expand'
const AUDIT = '/cases/c1/canvas/rules/R1/audit'

const mocks = vi.hoisted(() => ({
  handlers: {} as Record<string, (ev: unknown) => void>,
  setDataCalls: [] as unknown[],
  stateCalls: [] as Record<string, string[]>[],
}))

vi.mock('@antv/g6', () => ({
  Graph: class {
    constructor(cfg: unknown) {
      // 初始数据走构造参数（维度懒取可能先于 Graph 构造完成）；同样记录
      const data = (cfg as { data?: unknown } | undefined)?.data
      if (data) mocks.setDataCalls.push(data)
    }
    async render() {}
    setData(d: unknown) {
      mocks.setDataCalls.push(d)
    }
    destroy() {}
    on(event: string, cb: (ev: unknown) => void) {
      mocks.handlers[event] = cb
    }
    off() {}
    async zoomTo() {}
    async fitView() {}
    getZoom() {
      return 1
    }
    getElementPosition(): [number, number] {
      return [0, 0]
    }
    setElementState(state: Record<string, string[]>) {
      mocks.stateCalls.push(state)
    }
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

function envelope(over: Partial<CanvasEnvelope> = {}): CanvasEnvelope {
  return {
    canvas_id: 'cv_1',
    clue_id: 'clue-1',
    version: 1,
    created_by: '李侦查员',
    created_at: '',
    updated_by: '',
    updated_at: '',
    seeded: true,
    semantic_ready: true,
    doc: {
      nodes: [
        { id: 'rule:R1', kind: 'rule', ref: 'R1', label: '整数现金规则',
          system: true, adopted: false, stale: false, pinned: false,
          x: 0, y: 0,
          props: {
            rule_id: 'R1',
            rule_text: '单笔现金存入金额为整数万元，疑似结构化存款',
            basis: '整数现金存入',
          } },
        { id: 'fact:f1', kind: 'fact', ref: 'fact:f1', label: '事实一',
          system: true, adopted: false, stale: false, pinned: false,
          x: 260, y: 0, props: {} },
        { id: 'source_row:r1', kind: 'source_row', ref: '银行流水@local#row/aaa',
          label: '银行流水 #1', system: true, adopted: false, stale: false,
          pinned: false, x: 720, y: 0, props: {} },
      ],
      edges: [
        { id: 'e1', source: 'rule:R1', target: 'fact:f1', rel: '命中',
          system: true },
        { id: 'e2', source: 'fact:f1', target: 'source_row:r1',
          rel: '来源行', system: true },
      ],
    },
    ...over,
  }
}

const OBJ_ID = 'object:person:person_p1'
const EDGE_ID = 'e:fact:f1--涉及--object:person:person_p1'

function factExpandEnvelope(
  detail: Record<string, unknown> = {},
  notices: string[] = [],
): ExpandEnvelope {
  const base = envelope()
  const doc: CanvasDoc = {
    nodes: [
      ...base.doc.nodes,
      { id: OBJ_ID, kind: 'object', ref: 'person:person_p1',
        label: '张卫国', system: true, pinned: false, x: 480, y: 0,
        props: { type: 'person', pk: 'person_p1' } },
    ],
    edges: [
      ...base.doc.edges,
      { id: EDGE_ID, source: 'fact:f1', target: OBJ_ID, rel: '涉及',
        system: true },
    ],
  }
  return {
    ...base,
    version: 2,
    doc,
    added_nodes: [OBJ_ID],
    added_edges: [EDGE_ID],
    details: detail as ExpandEnvelope['details'],
    notices: notices as ExpandEnvelope['notices'],
    truncated: false,
    leaf: false,
  }
}

let wrapper: VueWrapper | null = null

function mountCanvas(routes: ConstructorParameters<typeof FakeTransport>[0]) {
  // ResearchCanvas 经 useCaseOntologyConfig 触达 pinia store（拉取失败回落 DEFAULT，不阻塞）
  setActivePinia(createPinia())
  setTransport(new FakeTransport(routes))
  // useMessage/useDialog 需要 provider 外壳（同 m3/m4 规格）
  wrapper = mount({
    components: { NMessageProvider, NDialogProvider, ResearchCanvas },
    template: `
      <NMessageProvider><NDialogProvider>
        <ResearchCanvas case-id="c1" clue-id="clue-1" />
      </NDialogProvider></NMessageProvider>`,
  })
  return wrapper
}

/** 单击有 250ms 延迟（区分双击）；测试中等一个动画帧周期 */
async function fireClick(id: string): Promise<void> {
  mocks.handlers['node:click']?.({ target: { id } })
  await new Promise((r) => setTimeout(r, 280))
  await flushPromises()
}

function fireDblClick(id: string): void {
  mocks.handlers['node:dblclick']?.({ target: { id } })
}

function lastGraphData(): { nodes: { id: string }[]; edges: { id: string }[] } {
  return mocks.setDataCalls.at(-1) as {
    nodes: { id: string }[]
    edges: { id: string }[]
  }
}

// NDrawer 默认 teleport 到 body（与 m3/m4 规格同口径）
function bodyEl(testid: string): HTMLElement {
  const el = document.body.querySelector(`[data-testid="${testid}"]`)
  if (!el) throw new Error(`element not found in body: ${testid}`)
  return el as HTMLElement
}

async function clickBody(testid: string): Promise<void> {
  bodyEl(testid).click()
  await flushPromises()
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  mocks.handlers = {}
  mocks.setDataCalls = []
  mocks.stateCalls = []
  setTransport(null)
  vi.restoreAllMocks()
})

// 折叠可见性纯函数（生产者并集/边过滤/layer 开关）见 canvas-view.spec.ts

// ----------------------------------------------------------------------
// RC-102：规则双视图
// ----------------------------------------------------------------------
describe('RC-102 规则节点业务/审计双视图', () => {
  it('业务视图不含技术标识；切审计视图按需装载 function/params 与工坊外链', async () => {
    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      {
        match: (r) => r.method === 'GET' && r.path === AUDIT,
        respond: () => okEnvelope({
          rule_id: 'R1',
          title: '整数现金存入',
          stage: 'xu_shi',
          dimension: '资金',
          function: 'time_window_collision',
          params: { min_amount: 10000, round_multiple: 10000 },
          hit_when: 'rows_nonempty',
          jian_types: ['生间'],
          assumption: 'H1',
          rule_text: '单笔现金存入金额为整数万元，疑似结构化存款',
          basis_text: '整数现金存入',
          ontology_version: '2.12.10',
          pack_id: 'default',
          rule_workshop_href: '/c/rules',
        }),
      },
    ])
    await flushPromises()

    await fireClick('rule:R1')

    expect(bodyEl('canvas-node-drawer')).toBeTruthy()

    // 业务视图：有判据文本，无 rule_id / function / params 等技术词
    const bizText = () => bodyEl('rule-business').textContent ?? ''
    expect(bizText()).toContain('疑似结构化存款')
    expect(bizText()).toContain('整数现金存入')
    expect(bizText()).not.toContain('rule_id')
    expect(bizText()).not.toContain('function')
    expect(bizText()).not.toContain('time_window_collision')
    expect(bizText()).not.toContain('params')
    expect(bizText()).not.toContain('R1')

    // 切审计视图 → 触发 GET audit
    await clickBody('rule-view-audit')

    const auditText = () => bodyEl('rule-audit').textContent ?? ''
    expect(auditText()).toContain('time_window_collision')
    expect(auditText()).toContain('min_amount')
    expect(auditText()).toContain('10000')
    expect(auditText()).toContain('2.12.10')
    expect(auditText()).toContain('R1')
    expect(bodyEl('rule-workshop-link').getAttribute('href')).toBe('/c/rules')

    // 切回业务视图不再出现技术标识
    await clickBody('rule-view-business')
    expect(bizText()).not.toContain('time_window_collision')
  })

  it('审计声明缺失（404）展示缺失态，不抛系统错误', async () => {
    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      { match: (r) => r.method === 'GET' && r.path === AUDIT,
        respond: () => errEnvelope(404, 'NOT_FOUND', '规则声明缺失') },
    ])
    await flushPromises()
    await fireClick('rule:R1')
    await clickBody('rule-view-audit')
    const missingText = bodyEl('audit-missing').textContent ?? ''
    expect(missingText).toContain('规则声明缺失')
  })
})

// ----------------------------------------------------------------------
// RC-103：fact expand 合并 + 折叠
// ----------------------------------------------------------------------
describe('RC-103 逐层溯源展开', () => {
  it('fact 展开合并对象层；折叠仅前端隐藏（不删服务端文档）', async () => {
    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      { match: (r) => r.method === 'POST' && r.path === EXPAND,
        respond: () => okEnvelope(factExpandEnvelope()) },
    ])
    await flushPromises()
    mocks.setDataCalls = []

    await fireClick('fact:f1')
    await clickBody('expand-fact')

    // 新对象节点已并入渲染
    const last = lastGraphData()
    expect(last.nodes.map((n) => n.id)).toContain(OBJ_ID)
    expect(last.edges.map((e) => e.id)).toContain(EDGE_ID)

    // 折叠按钮出现 → 折叠后前端视图隐藏明细（doc 副本仍含，未发 PATCH）
    expect(document.body.querySelector('[data-testid="collapse-fact"]')).toBeTruthy()
    await clickBody('collapse-fact')
    const folded = lastGraphData()
    expect(folded.nodes.map((n) => n.id)).not.toContain(OBJ_ID)
    expect(folded.edges.map((e) => e.id)).not.toContain(EDGE_ID)
    // 简洁视图折叠后只剩骨干 rule/fact（lane 伪节点不计）
    expect(folded.nodes.filter((n) => !String(n.id).startsWith('lane:')))
      .toHaveLength(2)
  })

  it('双击 fact 直接展开并懒加载；再次双击折叠（无需开抽屉）', async () => {
    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      { match: (r) => r.method === 'POST' && r.path === EXPAND,
        respond: () => okEnvelope(factExpandEnvelope()) },
    ])
    await flushPromises()
    mocks.setDataCalls = []

    fireDblClick('fact:f1')
    await flushPromises()
    expect(lastGraphData().nodes.map((n) => n.id)).toContain(OBJ_ID)

    fireDblClick('fact:f1')
    await flushPromises()
    const ids = lastGraphData().nodes.map((n) => n.id)
    expect(ids).not.toContain(OBJ_ID)
    expect(ids.filter((id) => !id.startsWith('lane:'))).toHaveLength(2)
  })

  it('展开失败进入失败态，重试成功后合并图层（不破坏已渲染层）', async () => {
    let failed = false
    const w = mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      {
        match: (r) => r.method === 'POST' && r.path === EXPAND,
        respond: () => {
          if (!failed) {
            failed = true
            throw new Error('network down')
          }
          return okEnvelope(factExpandEnvelope())
        },
      },
    ])
    await flushPromises()

    await fireClick('fact:f1')
    await clickBody('expand-fact')
    expect(document.body.querySelector('[data-testid="expand-failed"]')).toBeTruthy()
    // 原图层仍在
    expect(w.find('[data-testid="canvas-graph"]').exists()).toBe(true)

    mocks.setDataCalls = []
    const retryBtn = bodyEl('expand-failed').querySelector('button')
    if (!retryBtn) throw new Error('retry button not found')
    retryBtn.click()
    await flushPromises()
    expect(document.body.querySelector('[data-testid="expand-failed"]')).toBeFalsy()
    const last = mocks.setDataCalls.at(-1) as { nodes: { id: string }[] }
    expect(last.nodes.map((n) => n.id)).toContain(OBJ_ID)
  })
})

// ----------------------------------------------------------------------
// RC-104：数据行字段遮蔽（复用 MaskedField）
// ----------------------------------------------------------------------
describe('RC-104 数据行抽屉字段遮蔽', () => {
  it('denied 字段只渲染 ****，抽屉不出现真值', async () => {
    const rowId = 'source_row:r1'
    const base = envelope()
    const expand: ExpandEnvelope = {
      ...base,
      version: 2,
      doc: base.doc,
      added_nodes: [],
      added_edges: [],
      details: {
        [rowId]: {
          row_uri: '银行流水@b2#row/aaa',
          source: '银行流水',
          archived: true,
          missing: false,
          granularity: '',
          fields: [
            { name: '账号', raw: 'account_name', value: '6222001234',
              policy: 'visible', mask: 'text' },
            { name: '身份证', raw: 'id_card', value: '',
              policy: 'denied', mask: 'idcard' },
          ],
        },
      },
      notices: [],
      truncated: false,
      leaf: false,
    }

    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      { match: (r) => r.method === 'POST' && r.path === EXPAND,
        respond: () => okEnvelope(expand) },
    ])
    await flushPromises()

    // 点击数据行 → 自动 expand（抽屉即字段表）
    await fireClick(rowId)

    const traceText = () => bodyEl('row-trace').textContent ?? ''
    // 可见字段真值渲染；denied 字段只渲染 ****
    expect(traceText()).toContain('6222001234')
    expect(traceText()).toContain('****')
    // 真值（如身份证样例）从未出现在响应/页面
    expect(traceText()).not.toContain('31010419900307888X')
  })
})

// ----------------------------------------------------------------------
// UX P0：研判五维色点继承 + focus state 只下发可见元素
// ----------------------------------------------------------------------
describe('UX P0 五维色点与焦点链', () => {
  it('规则审计维度沿「命中」边继承给事实卡（dimensionMap 以 ref 为键、边用全 id）', async () => {
    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      { match: (r) => r.method === 'GET' && r.path === AUDIT,
        respond: () => okEnvelope({ rule_id: 'R1', dimension: '资金' }) },
    ])
    await flushPromises()
    await new Promise((r) => setTimeout(r, 0))
    await flushPromises()

    const data = lastGraphData() as unknown as {
      nodes: { id: string; data?: { dimColor?: string | null } }[]
    }
    const byId = new Map(data.nodes.map((n) => [n.id, n]))
    const ruleDim = byId.get('rule:R1')?.data?.dimColor
    const factDim = byId.get('fact:f1')?.data?.dimColor
    expect(ruleDim).toBeTruthy()
    // 边 source=rule:R1（全 id），dimensionMap 键=R1（ref）：事实必须继承到色
    expect(factDim).toBe(ruleDim)
  })

  it('简洁视图下 focus 只给已渲染元素下发 state（隐藏明细行不触发 Unknown element type）', async () => {
    mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
      { match: (r) => r.method === 'GET' && r.path === AUDIT,
        respond: () => errEnvelope(404, 'NOT_FOUND', '规则声明缺失') },
    ])
    await flushPromises()
    mocks.stateCalls = []

    // compact 默认：source_row:r1 / e2 不在投影中
    mocks.handlers['node:pointerover']?.({ target: { id: 'fact:f1' } })
    await flushPromises()
    const focus = mocks.stateCalls.at(-1) ?? {}
    expect(Object.keys(focus)).toContain('fact:f1')
    expect(focus['fact:f1']).toContain('selected')
    expect(focus['rule:R1']).toContain('focus')
    expect(focus['e1']).toContain('focus')
    expect(Object.keys(focus)).not.toContain('source_row:r1')
    expect(Object.keys(focus)).not.toContain('e2')
    expect(Object.keys(focus).some((k) => k.startsWith('lane:'))).toBe(false)

    mocks.handlers['node:pointerleave']?.({})
    await flushPromises()
    const cleared = mocks.stateCalls.at(-1) ?? {}
    expect(Object.keys(cleared)).not.toContain('source_row:r1')
  })
})
