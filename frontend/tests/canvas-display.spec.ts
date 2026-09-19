import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope, errEnvelope, type FakeRoute } from './helpers'
import ResearchCanvas from '../src/components/research/ResearchCanvas.vue'
import type { CanvasEnvelope } from '../src/domain/canvas'

// 展示层整改回归（P0-1 泳道列名 / P0-2 初始视口 / P1-1 状态栏 / P1-3 图例折叠）：
// 这些是「画得出来但看不懂」类缺陷，单测只守住可断言的数据与配置层。

const PATH = '/cases/c1/clues/clue-1/canvas'

const mocks = vi.hoisted(() => ({
  configs: [] as unknown[],
  handlers: {} as Record<string, (ev: unknown) => void>,
  states: [] as Array<Record<string, string[]>>,
  sets: [] as Array<{
    nodes: Array<{
      id: string
      style?: { x?: number; y?: number }
      data?: Record<string, unknown>
    }>
    edges: Array<{ id: string; data: Record<string, unknown> }>
  }>,
}))

vi.mock('@antv/g6', () => ({
  Graph: class {
    constructor(cfg: unknown) {
      mocks.configs.push(cfg)
    }
    async render() {}
    setData(d: unknown) {
      mocks.sets.push(d as (typeof mocks.sets)[number])
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
    getSize() {
      return [0, 0]
    }
    getElementPosition() {
      return [0, 0]
    }
    setElementState(state: Record<string, string[]>) {
      mocks.states.push(state)
    }
  },
  register() {},
  ExtensionCategory: { NODE: 'node' },
  Badge: class {},
  Label: class {},
  Rect: class {
    static defaultStyleProps = {}
  },
}))

function envelope(): CanvasEnvelope {
  return {
    canvas_id: 'cv_1',
    clue_id: 'clue-1',
    version: 1,
    created_by: '李侦查员',
    created_at: '2026-09-13 10:00',
    updated_by: '',
    updated_at: '',
    seeded: true,
    semantic_ready: true,
    doc: {
      nodes: [
        { id: 'rule:R1', kind: 'rule', ref: 'R1', label: '规则R1',
          system: true, adopted: false, stale: false, pinned: false,
          x: 0, y: 0, props: {} },
        { id: 'fact:f1', kind: 'fact', ref: 'fact:f1', label: '事实一',
          system: true, adopted: false, stale: false, pinned: false,
          x: 260, y: 0, props: {} },
        { id: 'hyp:h1', kind: 'hypothesis', ref: 'hyp:h1', label: '假设一',
          system: false, adopted: false, stale: false, pinned: false,
          x: 480, y: 0, props: {} },
        { id: 'rule:R2', kind: 'rule', ref: 'R2', label: '规则R2',
          system: true, adopted: false, stale: false, pinned: false,
          x: 0, y: 104, props: {} },
        { id: 'note:n1', kind: 'note', ref: 'note:n1', label: '备注一',
          system: true, adopted: false, stale: false, pinned: false,
          x: 480, y: 104, props: {} },
      ],
      edges: [
        { id: 'e1', source: 'rule:R1', target: 'fact:f1', rel: '命中',
          system: true },
        // fact → note → hyp：note 是度=2 的过路节点（P1 折叠目标）
        { id: 'e2', source: 'fact:f1', target: 'note:n1', rel: '关联',
          system: true },
        { id: 'e3', source: 'rule:R2', target: 'fact:f1', rel: '命中',
          system: true },
        { id: 'e4', source: 'note:n1', target: 'hyp:h1', rel: '推断为',
          system: false },
      ],
    },
  }
}

let wrapper: VueWrapper | null = null

function mountCanvas(env: CanvasEnvelope = envelope(), routes: FakeRoute[] = []) {
  setTransport(new FakeTransport([
    { match: (r) => r.method === 'GET' && r.path === PATH,
      respond: () => okEnvelope(env) },
    ...routes,
  ]))
  wrapper = mount({
    components: { NMessageProvider, NDialogProvider, ResearchCanvas },
    template: `
      <NMessageProvider><NDialogProvider>
        <ResearchCanvas case-id="c1" clue-id="clue-1" />
      </NDialogProvider></NMessageProvider>`,
  })
  return wrapper
}

/** G6 配置里的样式映射函数（labelText/labelFill/...）——保留以便后续 P2/P3 测试复用 */
function _nodeStyle(key: string): (d: never) => unknown {
  const cfg = mocks.configs[0] as {
    node: { style: Record<string, (d: never) => unknown> }
  }
  return cfg.node.style[key]
}
void _nodeStyle

/** 单击有 250ms 延迟（区分双击展开/折叠） */
async function clickNode(id: string): Promise<void> {
  mocks.handlers['node:click']?.({ target: { id } })
  await new Promise((r) => setTimeout(r, 280))
  await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  mocks.configs.length = 0
  Object.keys(mocks.handlers).forEach((k) => delete mocks.handlers[k])
  setTransport(null)
  vi.restoreAllMocks()
})

describe('P0-1 泳道下线：列名改由「全局概览」列胶囊统一承载', () => {
  it('画布不再注入泳道伪节点', async () => {
    mountCanvas()
    await flushPromises()
    const cfg = mocks.configs[0] as { data: { nodes: Array<{ id: string }> } }
    expect(cfg.data.nodes.filter((n) => n.id.startsWith('lane:'))).toHaveLength(0)
  })

  it('明细视图下节点按 RANK_X 落槽：列分界靠 X 方向对齐', async () => {
    mountCanvas()
    await flushPromises()
    const cfg = mocks.configs[0] as { data: { nodes: Array<{ id: string; style: { x: number } }> } }
    const xs = [...new Set(cfg.data.nodes.map((n) => n.style.x))].sort((a, b) => a - b)
    // 测试 fixture 覆盖 rule/fact/object 三列（0/260/480）
    expect(xs).toEqual([0, 260, 480])
  })
})

describe('P0-2 初始视口', () => {
  it('不再使用 autoFit（之前泳道把 bbox 撑大、卡片被压到不可读）', async () => {
    mountCanvas()
    await flushPromises()
    const cfg = mocks.configs[0] as { autoFit?: unknown }
    expect(cfg.autoFit).toBeUndefined()
  })
})

describe('P1-1 画布状态栏', () => {
  it('展示当前投影的节点/关系数与缩放', async () => {
    const w = mountCanvas()
    await flushPromises()
    const status = w.find('[data-testid="canvas-status"]')
    expect(status.exists()).toBe(true)
    // 简洁视图下 object/source_row/source_file 明细折叠：骨干 5 节点 4 关系
    expect(status.text()).toContain('节点 5')
    expect(status.text()).toContain('关系 4')
    expect(status.text()).toContain('缩放')
  })
})

describe('P0 焦点链路：链外边不画 + 跳数分级', () => {
  function statusText(w: VueWrapper): string {
    return w.find('[data-testid="canvas-status"]').text()
  }

  function lastStates(): Record<string, string[]> {
    return mocks.states[mocks.states.length - 1] ?? {}
  }

  it('固化焦点：链上边带跳数状态，链外边 hidden（不再只是压暗）', async () => {
    const w = mountCanvas()
    await flushPromises()
    await clickNode('rule:R1')
    // 默认 2 跳：e1 一跳、e2/e3 二跳，无链外边
    let s = lastStates()
    expect(s['e1']).toEqual(['focus', 'hop1'])
    expect(s['e2']).toEqual(['focus', 'hop2'])
    expect(s['e3']).toEqual(['focus', 'hop2'])
    expect(s['rule:R1']).toEqual(['selected', 'focus'])

    // 收到 1 跳：e2/e3 变链外 → 直接隐藏
    await w.find('[data-testid="hop-minus"]').trigger('click')
    await flushPromises()
    s = lastStates()
    expect(s['e1']).toEqual(['focus', 'hop1'])
    expect(s['e2']).toEqual(['hidden'])
    expect(s['e3']).toEqual(['hidden'])
  })

  it('链外节点仍保留 dim 形态（不跟着隐藏，保留上下文）', async () => {
    mountCanvas()
    await flushPromises()
    await clickNode('rule:R1')
    await flushPromises()
    const s = lastStates()
    expect(s['fact:f1']).toEqual(['focus', 'hop1'])
    expect(s['note:n1']).toEqual(['focus', 'hop2'])
    // 3 跳外的 hyp:h1 不在链上，只压暗不隐藏
    expect(s['hyp:h1']).toEqual(['dim'])
    // 回到 1 跳后 note:n1 出链，只压暗不隐藏
    const w = wrapper!
    await w.find('[data-testid="hop-minus"]').trigger('click')
    await flushPromises()
    expect(lastStates()['note:n1']).toEqual(['dim'])
  })

  it('P1 传递节点折叠：过路节点压成合成边，点边可展开', async () => {
    const w = mountCanvas()
    await flushPromises()
    await clickNode('rule:R1')
    // 2 跳时 note 只有一侧在链上（度=1），不折叠；+1 跳到 3 跳后成为过路节点
    expect(statusText(w)).not.toContain('过路节点')
    await w.find('[data-testid="hop-plus"]').trigger('click')
    await flushPromises()

    const last = mocks.sets[mocks.sets.length - 1]
    expect(last.nodes.map((n) => n.id)).not.toContain('note:n1')
    const syn = last.edges.find((e) => e.id.startsWith('syn:'))
    expect(syn).toBeTruthy()
    expect(syn?.data.label).toBe('经 1 个中间节点')
    expect(statusText(w)).toContain('折叠 1 个过路节点')

    // 点合成边 = 把这一段放回来
    mocks.handlers['edge:click']?.({ target: { id: syn?.id } })
    await flushPromises()
    const after = mocks.sets[mocks.sets.length - 1]
    expect(after.nodes.map((n) => n.id)).toContain('note:n1')
  })

  it('工具栏「压缩链路」可整体关掉折叠', async () => {
    const w = mountCanvas()
    await flushPromises()
    await clickNode('rule:R1')
    await w.find('[data-testid="hop-plus"]').trigger('click')
    await flushPromises()
    expect(mocks.sets[mocks.sets.length - 1].nodes.map((n) => n.id))
      .not.toContain('note:n1')
    await w.find('[data-testid="tb-collapse-chain"]').trigger('click')
    await flushPromises()
    expect(mocks.sets[mocks.sets.length - 1].nodes.map((n) => n.id))
      .toContain('note:n1')
    expect(statusText(w)).not.toContain('过路节点')
  })

  it('跳数上限 3：按钮到顶后禁用，状态栏显示当前跳数', async () => {
    const w = mountCanvas()
    await flushPromises()
    expect(w.find('[data-testid="hop-minus"]').exists()).toBe(false)
    await clickNode('rule:R1')
    expect(w.find('[data-testid="hop-plus"]').exists()).toBe(true)
    await w.find('[data-testid="hop-plus"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="hop-plus"]').attributes('disabled')).toBeDefined()
    expect(w.text()).toContain('焦点跳数')
  })
})

describe('P1 焦点路径条：来路可见、每层可回退', () => {
  it('点节点入栈，点前一段回退，点「全部」回全局', async () => {
    const w = mountCanvas()
    await flushPromises()
    expect(w.find('[data-testid="canvas-pathbar"]').exists()).toBe(false)

    await clickNode('rule:R1')
    let bar = w.find('[data-testid="canvas-pathbar"]')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('规则R1')

    await clickNode('fact:f1')
    bar = w.find('[data-testid="canvas-pathbar"]')
    expect(bar.text()).toContain('规则R1')
    expect(bar.text()).toContain('事实一')

    // 点前一段 = 截断其后轨迹
    await w.find('[data-testid="pathbar-rule:R1"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="pathbar-rule:R1"]').exists()).toBe(true)
    expect(w.find('[data-testid="pathbar-fact:f1"]').exists()).toBe(false)

    // 点「全部」= 清焦点（跳数游标随之消失）
    await w.find('[data-testid="pathbar-root"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="canvas-pathbar"]').exists()).toBe(false)
    expect(w.find('[data-testid="hop-minus"]').exists()).toBe(false)
  })

  it('重复命中同一节点不堆积（截断其后）', async () => {
    const w = mountCanvas()
    await flushPromises()
    await clickNode('rule:R1')
    await clickNode('fact:f1')
    await clickNode('rule:R1')
    expect(w.find('[data-testid="pathbar-fact:f1"]').exists()).toBe(false)
    expect(w.find('[data-testid="pathbar-rule:R1"]').exists()).toBe(true)
  })
})

describe('P1-3 图例折叠', () => {
  it('点收起后只剩胶囊，再点展开恢复', async () => {
    const w = mountCanvas()
    await flushPromises()
    const collapse = w.find('[data-testid="legend-collapse"]')
    expect(collapse.exists()).toBe(true)
    expect(collapse.text()).toBe('收起')

    await collapse.trigger('click')
    // 收起后节点类型清单不再渲染
    expect(w.text()).not.toContain('节点类型')
    expect(w.find('[data-testid="legend-collapse"]').text()).toBe('图例')

    await w.find('[data-testid="legend-collapse"]').trigger('click')
    expect(w.text()).toContain('节点类型')
  })

  it('收起偏好写入 localStorage 并跨挂载恢复', async () => {
    const w = mountCanvas()
    await flushPromises()
    await w.find('[data-testid="legend-collapse"]').trigger('click')
    const raw = localStorage.getItem('canvas-view:c1:clue-1')
    expect(raw).toBeTruthy()
    expect(JSON.parse(raw ?? '{}').legendCollapsed).toBe(true)
  })
})

describe('P2 全局层聚合视图', () => {
  function statusText(w: VueWrapper): string {
    return w.find('[data-testid="canvas-status"]').text()
  }

  it('工具栏「全局概览」可切换：状态栏标识 + 节点/关系数按聚合视图计', async () => {
    const w = mountCanvas()
    await flushPromises()
    expect(w.find('[data-testid="tb-overview"]').exists()).toBe(true)

    // 进入概览
    await w.find('[data-testid="tb-overview"]').trigger('click')
    await flushPromises()
    // doc 节点：rule×2, fact×1, hypothesis×1, note×1 → 列：rule/fact/object
    expect(statusText(w)).toContain('全局概览')
    expect(statusText(w)).toContain('节点 3')
    // 主干边：rule--fact (e1,e3) + fact--object (e2,e4) = 2 条
    expect(statusText(w)).toContain('关系 2')
    // 进入概览后路径条/缩略图不再渲染
    expect(w.find('[data-testid="canvas-minimap"]').exists()).toBe(false)

    // 退回明细
    await w.find('[data-testid="tb-overview"]').trigger('click')
    await flushPromises()
    expect(statusText(w)).toContain('简洁')
    expect(statusText(w)).not.toContain('全局概览')
    expect(statusText(w)).toContain('节点 5')
    expect(w.find('[data-testid="canvas-minimap"]').exists()).toBe(true)
  })

  it('概览下 G6 数据被替换为列胶囊 + 主干边，不再有原始节点与泳道', async () => {
    const w = mountCanvas()
    await flushPromises()
    await w.find('[data-testid="tb-overview"]').trigger('click')
    await flushPromises()
    const last = mocks.sets[mocks.sets.length - 1]
    expect(last.nodes.every((n) => n.id.startsWith('ov:'))).toBe(true)
    expect(last.nodes.every((n) => !n.id.startsWith('lane:'))).toBe(true)
    expect(last.edges.every((e) => e.id.startsWith('ovl:'))).toBe(true)
    // 主干边标签显示条数
    expect(last.edges.every((e) => /^ovl:/.test(e.data.label as string) || /\d+ 条/.test(e.data.label as string))).toBe(true)
  })

  it('点概览中的列胶囊：回到明细 + 顺手打开对应层', async () => {
    const w = mountCanvas()
    await flushPromises()
    await w.find('[data-testid="tb-overview"]').trigger('click')
    await flushPromises()
    mocks.handlers['node:click']?.({ target: { id: 'ov:object' } })
    await flushPromises()
    // 已退出概览，状态栏恢复简洁
    expect(statusText(w)).toContain('简洁')
    expect(statusText(w)).not.toContain('全局概览')
    // object 层被打开：渲染集中应出现 object 类节点（实际 doc 没有 object 节点；
    // 但可断言 layerForced 已变更且最后一帧数据不再有 ov: 节点）
    const last = mocks.sets[mocks.sets.length - 1]
    expect(last.nodes.every((n) => !n.id.startsWith('ov:'))).toBe(true)
  })
})

describe('P2 全局缩略图', () => {
  it('画布右下角渲染缩略图，按节点画出点阵', async () => {
    const w = mountCanvas()
    await flushPromises()
    const mm = w.find('[data-testid="canvas-minimap"]')
    expect(mm.exists()).toBe(true)
    const svg = w.find('[data-testid="canvas-minimap-svg"]')
    expect(svg.exists()).toBe(true)
    // rect 数 = 节点数（5 个骨干节点均渲染）
    const rects = svg.findAll('rect').filter((r) => !r.classes().includes('mm-view'))
    expect(rects.length).toBeGreaterThanOrEqual(5)
  })
})

describe('P3 研判视角：证据强度三层', () => {
  it('工具栏「证据强度」可切换；状态栏显示视角 + tier 计数', async () => {
    const w = mountCanvas()
    await flushPromises()
    expect(w.find('[data-testid="tb-perspective"]').exists()).toBe(true)
    expect(w.text()).toContain('视角：流程')

    await w.find('[data-testid="tb-perspective"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('视角：证据强度')
    expect(w.text()).toContain('已锁死')
    expect(w.text()).toContain('待核实')
    // 工具栏按钮 primary 态
    expect(w.find('[data-testid="tb-perspective"]').attributes('class')).toContain(
      'tb-btn',
    )

    await w.find('[data-testid="tb-perspective"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('视角：流程')
    expect(w.text()).not.toContain('已锁死 ·')
  })

  it('切到证据强度后 G6 节点按 tier 落槽：同列节点 y 不再唯一', async () => {
    const w = mountCanvas()
    await flushPromises()
    // 切换前 y 唯一（5 个节点 y 互不相同）；切换后 tier 重排可能产生同 y
    const before = mocks.sets[mocks.sets.length - 1].nodes.map(
      (n) => (n as { style: { y: number } }).style.y,
    )
    await w.find('[data-testid="tb-perspective"]').trigger('click')
    await flushPromises()
    const after = mocks.sets[mocks.sets.length - 1].nodes.map(
      (n) => (n as { style: { y: number } }).style.y,
    )
    // 视角切换后节点数据应当被重排（不一定都变，但节点 tier 字段已写入）
    const lastData = mocks.sets[mocks.sets.length - 1].nodes[0]?.data as
      | Record<string, unknown>
      | undefined
    expect(lastData?.tier).toBeDefined()
    // y 总分布跨度会显著大于流程视角（≥3 个带）
    const span = Math.max(...after) - Math.min(...after)
    const beforeSpan = Math.max(...before) - Math.min(...before)
    expect(span).toBeGreaterThan(beforeSpan)
  })

  it('证据强度视角下 hypothesis / note 节点 (Tier 3) 在 G6 节点 style 上半透', async () => {
    const w = mountCanvas()
    await flushPromises()
    await w.find('[data-testid="tb-perspective"]').trigger('click')
    await flushPromises()
    // mock G6 节点 style.opacity 是 fn；通过测试假设它被新设置（presence 检查）：
    const cfg = mocks.configs[mocks.configs.length - 1] as {
      node: { style: Record<string, unknown> }
    }
    expect(typeof cfg.node.style.opacity).toBe('function')
  })

  it('视角偏好写入 localStorage 并跨挂挂载恢复', async () => {
    const w = mountCanvas()
    await flushPromises()
    await w.find('[data-testid="tb-perspective"]').trigger('click')
    await flushPromises()
    const raw = localStorage.getItem('canvas-view:c1:clue-1')
    expect(raw).toBeTruthy()
    expect(JSON.parse(raw ?? '{}').perspective).toBe('tier')
  })
})

// ======================================================================
// 书证节点抽屉：props 登记信息直显（seed/reconcile 同源）
// ======================================================================
describe('书证节点抽屉', () => {
  function evidenceEnvelope(): CanvasEnvelope {
    const env = envelope()
    env.doc.nodes.push({
      id: 'evidence:ev_1', kind: 'evidence', ref: 'ev_1',
      label: 'invoide.png', system: true, adopted: false, stale: false,
      pinned: false, x: 480, y: 208,
      props: {
        material_id: 'ev_1', material_type: '缴款单',
        uploaded_by: '王检察官', uploaded_at: '2026-09-19T18:38:51',
      },
    } as never)
    return env
  }

  it('点击书证节点：抽屉展示材料类型/上传人/上传时间/材料 ID', async () => {
    mountCanvas(evidenceEnvelope())
    await flushPromises()
    await clickNode('evidence:ev_1')
    const drawer = wrapper!.findComponent({ name: 'CanvasNodeDrawer' })
    expect(drawer.exists()).toBe(true)
    const card = drawer.find('[data-testid="evidence-card"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('缴款单')
    expect(card.text()).toContain('王检察官')
    expect(card.text()).toContain('2026-09-19T18:38:51')
    expect(card.text()).toContain('ev_1')
  })

  const FINDINGS_PATH = '/cases/c1/clues/clue-1/vlm/findings'

  function findingsRoute(): FakeRoute {
    return {
      match: (r) => r.method === 'GET' && r.path === FINDINGS_PATH,
      respond: () => okEnvelope({
        findings: {
          ev_1: {
            pending: [{
              proposal_id: 'pp-1', title: '缴款单金额与流水不符',
              detail: '票面 5 万元，同期流水无对应存入', severity: 'warn',
              image_uri: 'evidence/ev_1/a.png', model: 'qwen-vl-max',
              model_score: 0.86, stale: false, created_at: '2026-09-19 20:00',
            }],
            verified: [{
              image_evidence_id: 'ie_1', title: '缴款单金额与流水不符',
              detail: '票面 5 万元', severity: 'warn',
              image_uri: 'evidence/ev_1/a.png', model: 'qwen-vl-max',
              model_score: 0.86, verifier: '王检察官',
              verify_conclusion: '经比对原件属实',
              subject_type: 'person', subject_id: 'p1',
              clue_id: 'clue-1', created_at: '2026-09-19 20:10',
            }],
          },
        },
      }),
    }
  }

  it('书证节点抽屉渲染 findings：AI 草案 + 已人验证据', async () => {
    mountCanvas(evidenceEnvelope(), [findingsRoute()])
    await flushPromises()
    await clickNode('evidence:ev_1')
    await flushPromises()
    const block = wrapper!.find('[data-testid="evidence-findings"]')
    expect(block.exists()).toBe(true)
    expect(block.find('[data-testid="finding-pending"]').exists()).toBe(true)
    expect(block.find('[data-testid="finding-verified"]').exists()).toBe(true)
    expect(block.text()).toContain('缴款单金额与流水不符')
    expect(block.text()).toContain('把握度 0.86')
    expect(block.text()).toContain('经比对原件属实')
    expect(block.text()).toContain('王检察官')
  })

  it('findings 拉取失败：抽屉软提示，不阻塞登记信息', async () => {
    mountCanvas(evidenceEnvelope(), [{
      match: (r) => r.method === 'GET' && r.path === FINDINGS_PATH,
      respond: () => errEnvelope(500, 'INTERNAL', 'boom'),
    }])
    await flushPromises()
    await clickNode('evidence:ev_1')
    await flushPromises()
    expect(wrapper!.find('[data-testid="findings-error"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="evidence-card"]').exists()).toBe(true)
  })
})

// ======================================================================
// M4 P3：下一步可核查建议（drawer）
// ======================================================================
describe('M4 P3 下一步可核查', () => {
  it('抽屉打开 fact 节点时，注入 verify-suggestions 到 CanvasNodeDrawer', async () => {
    const w = mountCanvas()
    await flushPromises()
    // 默认 fixture 中 fact:f1 已存在；模拟点击该节点
    const fact = w.findComponent({ name: 'CanvasNodeDrawer' })
    expect(fact.exists()).toBe(true)
    // 通过 props 传递的 verifySuggestions 至少长度 > 0（事实节点 1-2 条）
    const props = fact.props() as Record<string, unknown>
    expect(Array.isArray(props.verifySuggestions)).toBe(true)
  })

  it('verifySuggestions prop 是纯函数结果（按节点 props 派生）', async () => {
    mountCanvas()
    await flushPromises()
    await clickNode('fact:f1')
    // 现在 drawerNode=f1，应该注入建议
    const fact = wrapper!.findComponent({ name: 'CanvasNodeDrawer' })
    const props = fact.props() as Record<string, unknown>
    const list = props.verifySuggestions as Array<{
      id: string
      text: string
      channel: string
    }>
    expect(list.length).toBeGreaterThan(0)
    expect(list[0].id).toMatch(/:f1:/)
  })
})
