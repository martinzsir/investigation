import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { h } from 'vue'
import { NCheckbox, NMessageProvider } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import GraphView from '../src/views/GraphView.vue'
import { useCaseStore } from '../src/stores/case'
import type { GraphDto } from '../src/api/endpoints/graph'

// P7 知识图谱投影交互链：边类型过滤（relation 模式勾选子集）与
// 资金链路图（fund 模式固定 transfers）必须经由同一 /graph 端点重新取数。
// 本文件锁定「点击复选框/模式按钮 → 新请求参数」的前端响应链（不碰 G6）。

const GRAPH_PATH = '/cases/c1/graph'

function graphDto(): GraphDto {
  return {
    available: true,
    truncated: { nodes: false, edges: false, dropped_edges: 0 },
    edge_kinds: [
      { name: 'transfers', title: '转账关系', present: true },
      { name: 'calls', title: '通话关系', present: true },
      { name: 'bid_participation', title: '中标参与', present: true },
      { name: 'colocation', title: '同地点出现', present: true },
      { name: 'report_target', title: '举报针对人员', present: true },
      { name: 'report_source', title: '举报来源指向', present: true },
      // 未 BUILD 对应边表的类型不进过滤勾选器
      { name: 'not_built', title: '未建表关系', present: false },
    ],
    highlights: { clue_count: 0, nodes: 0, edges: 0 },
    nodes: [
      { id: 'person:1', label: '张卫国', type: 'person', type_title: '自然人', jian: [], hit: false, clue_ids: [] },
      { id: 'account:1', label: '测试商户', type: 'account', type_title: '账户', jian: [], hit: false, clue_ids: [] },
    ],
    edges: [
      { source: 'person:1', target: 'account:1', label: '持有', type: 'holds', hit: false, clue_ids: [], props: {} },
    ],
  }
}

let root: VueWrapper | null = null
let view: VueWrapper | null = null

async function mountView() {
  const t = new FakeTransport([
    {
      match: (r) => r.method === 'GET' && r.path.startsWith(GRAPH_PATH),
      respond: () => okEnvelope(graphDto()),
    },
  ])
  setTransport(t)

  setActivePinia(createPinia())
  useCaseStore().selectCase('c1')

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/c/graph', component: { template: '<div />' } }],
  })
  await router.push('/c/graph')
  await router.isReady()

  root = mount(NMessageProvider, {
    slots: { default: () => h(GraphView) },
    global: {
      plugins: [router],
      // 隔离 @antv/g6 动态 import；本套件只验证取数响应链
      stubs: { GraphCanvas: true },
    },
  })
  view = root.findComponent(GraphView)
  await flushPromises()
  await flushPromises()
  return t
}

function graphCalls(t: FakeTransport) {
  return t.calls.filter((c) => c.path.startsWith(GRAPH_PATH))
}

function lastCall(t: FakeTransport) {
  return graphCalls(t).at(-1)!
}

function checkboxByLabel(label: string) {
  const cb = view!.findAllComponents(NCheckbox).find((c) => c.text().includes(label))
  if (!cb) throw new Error(`checkbox not found: ${label}`)
  return cb
}

function kindCheckboxes() {
  return view!.findAllComponents(NCheckbox)
}

function buttonByText(text: string): DOMWrapper<Element> | undefined {
  return view!.findAll('button').find((b) => b.text().includes(text))
}

async function clickAndSettle(el: VueWrapper | DOMWrapper<Element>): Promise<void> {
  await el.trigger('click')
  await flushPromises()
}

afterEach(() => {
  root?.unmount()
  root = null
  view = null
  setTransport(null)
  document.body.innerHTML = ''
})

describe('知识图谱：初始加载与过滤区', () => {
  it('初始恰好 1 次无 edge_kinds 请求；过滤区只列 present 边类型', async () => {
    const t = await mountView()
    const calls = graphCalls(t)
    expect(calls).toHaveLength(1)
    expect(calls[0].path).not.toContain('edge_kinds')
    // 6 个 present 类型，not_built 不出现
    expect(kindCheckboxes()).toHaveLength(6)
    expect(checkboxByLabel('转账关系').props('checked')).toBe(false)
    // 无勾选时不显示清空入口
    expect(buttonByText('全部边')).toBeUndefined()
  })
})

describe('知识图谱：边类型过滤复选框响应链', () => {
  it('勾选转账关系：新增恰好 1 次请求且 edge_kinds=transfers；受控 checked 翻转', async () => {
    const t = await mountView()
    const before = graphCalls(t).length
    const cb = checkboxByLabel('转账关系')

    await clickAndSettle(cb)

    expect(graphCalls(t)).toHaveLength(before + 1)
    expect(lastCall(t).path).toContain('edge_kinds=transfers')
    expect(cb.props('checked')).toBe(true)
    expect(buttonByText('全部边')).toBeDefined()
  })

  it('再次点击取消：新增恰好 1 次无 edge_kinds 请求，checked 复原', async () => {
    const t = await mountView()
    const cb = checkboxByLabel('转账关系')

    await clickAndSettle(cb)
    expect(lastCall(t).path).toContain('edge_kinds=transfers')
    const afterCheck = graphCalls(t).length

    await clickAndSettle(cb)

    expect(graphCalls(t)).toHaveLength(afterCheck + 1)
    expect(lastCall(t).path).not.toContain('edge_kinds')
    expect(cb.props('checked')).toBe(false)
    expect(buttonByText('全部边')).toBeUndefined()
  })

  it('勾选多类型后点「全部边」：新增 1 次无 edge_kinds 请求且全部复选框复位', async () => {
    const t = await mountView()
    await clickAndSettle(checkboxByLabel('转账关系'))
    await clickAndSettle(checkboxByLabel('通话关系'))
    expect(lastCall(t).path).toContain('edge_kinds=transfers%2Ccalls')
    const before = graphCalls(t).length

    await clickAndSettle(buttonByText('全部边')!)

    expect(graphCalls(t)).toHaveLength(before + 1)
    expect(lastCall(t).path).not.toContain('edge_kinds')
    expect(kindCheckboxes().every((c) => c.props('checked') === false)).toBe(true)
    expect(buttonByText('全部边')).toBeUndefined()
  })
})

describe('知识图谱：关系/资金模式切换', () => {
  it('模式按钮组渲染两按钮；资金链路图固定 transfers 且隐藏过滤区；切回恢复全量', async () => {
    const t = await mountView()
    const relationBtn = buttonByText('关系图谱')
    const fundBtn = buttonByText('资金链路图')
    // 修复前 NButton.Group 为 undefined：两个按钮均不渲染
    expect(relationBtn).toBeDefined()
    expect(fundBtn).toBeDefined()
    expect(kindCheckboxes().length).toBeGreaterThan(0)

    const beforeFund = graphCalls(t).length
    await clickAndSettle(fundBtn!)
    expect(graphCalls(t)).toHaveLength(beforeFund + 1)
    expect(lastCall(t).path).toContain('edge_kinds=transfers')
    // fund 模式不渲染边类型过滤区
    expect(kindCheckboxes()).toHaveLength(0)

    const beforeRelation = graphCalls(t).length
    await clickAndSettle(relationBtn!)
    expect(graphCalls(t)).toHaveLength(beforeRelation + 1)
    expect(lastCall(t).path).not.toContain('edge_kinds')
    expect(kindCheckboxes().length).toBeGreaterThan(0)
  })

  it('非空勾选下 relation→fund→relation：切回时按当前勾选集合下发 edge_kinds', async () => {
    const t = await mountView()
    // 先勾选 transfers + calls，使 relation 模式持非空集合
    await clickAndSettle(checkboxByLabel('转账关系'))
    await clickAndSettle(checkboxByLabel('通话关系'))
    expect(lastCall(t).path).toContain('edge_kinds=transfers%2Ccalls')

    // 切资金链路图：固定 transfers，过滤区隐藏
    const beforeFund = graphCalls(t).length
    await clickAndSettle(buttonByText('资金链路图')!)
    expect(graphCalls(t)).toHaveLength(beforeFund + 1)
    expect(lastCall(t).path).toContain('edge_kinds=transfers')
    expect(kindCheckboxes()).toHaveLength(0)

    // 切回关系图谱：应按切前的勾选集合（transfers,calls）恢复下发，非空集合
    const beforeRelation = graphCalls(t).length
    await clickAndSettle(buttonByText('关系图谱')!)
    expect(graphCalls(t)).toHaveLength(beforeRelation + 1)
    expect(lastCall(t).path).toContain('edge_kinds=transfers%2Ccalls')
    expect(kindCheckboxes().length).toBeGreaterThan(0)
    // 两项仍处于勾选态（状态在模式切换间保持）
    expect(checkboxByLabel('转账关系').props('checked')).toBe(true)
    expect(checkboxByLabel('通话关系').props('checked')).toBe(true)
  })

  it('响应链不重复取数：初始 1 次 + 勾选/取消/两次模式切换共 4 次交互 = 5 次请求', async () => {
    const t = await mountView()
    await clickAndSettle(checkboxByLabel('转账关系'))
    await clickAndSettle(checkboxByLabel('转账关系'))
    await clickAndSettle(buttonByText('资金链路图')!)
    await clickAndSettle(buttonByText('关系图谱')!)
    expect(graphCalls(t)).toHaveLength(5)
  })
})
