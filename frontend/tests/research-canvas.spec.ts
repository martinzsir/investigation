import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import ResearchCanvas from '../src/components/research/ResearchCanvas.vue'
import type { CanvasEnvelope } from '../src/domain/canvas'

// RC-101/207 研判画布 M1：
//  - 加载骨架 → G6 成图（节点/边数与 doc 一致，preset x/y 落位）；
//  - 加载错误可重试；
//  - G6 装载/渲染失败降级「节点-关系列表」（禁空白），可重试图形视图；
//  - semantic_ready=false 展示横幅；空文档展示空态。

const PATH = '/cases/c1/clues/clue-1/canvas'

const mocks = vi.hoisted(() => ({
  configs: [] as unknown[],
  failRender: false,
  handlers: {} as Record<string, (ev: unknown) => void>,
  positions: new Map<string, [number, number]>(),
}))

vi.mock('@antv/g6', () => ({
  Graph: class {
    constructor(cfg: unknown) {
      mocks.configs.push(cfg)
    }
    async render() {
      if (mocks.failRender) throw new Error('g6 render boom')
    }
    setData() {}
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

function envelope(over: Partial<CanvasEnvelope> = {}): CanvasEnvelope {
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

let wrapper: VueWrapper | null = null

function mountCanvas(routes: ConstructorParameters<typeof FakeTransport>[0]) {
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

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  mocks.configs.length = 0
  mocks.failRender = false
  Object.keys(mocks.handlers).forEach((k) => delete mocks.handlers[k])
  mocks.positions.clear()
  setTransport(null)
  vi.restoreAllMocks()
})

describe('ResearchCanvas RC-101 种子成图', () => {
  it('shows skeleton then renders graph at preset x/y with all nodes', async () => {
    const w = mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
    ])
    // 挂载瞬间是骨架态
    expect(w.find('[data-testid="canvas-skeleton"]').exists()).toBe(true)
    expect(w.text()).toContain('正在根据线索事实初始化画布')

    await flushPromises()

    // G6 成功成图：降级/错误/空态均不存在
    expect(w.find('[data-testid="canvas-graph"]').exists()).toBe(true)
    expect(w.find('[data-testid="canvas-fallback"]').exists()).toBe(false)
    expect(w.find('[data-testid="canvas-error"]').exists()).toBe(false)
    expect(w.find('[data-testid="canvas-empty"]').exists()).toBe(false)

    // 交给 G6 的数据与后端 doc 同量
    expect(mocks.configs).toHaveLength(1)
    const cfg = mocks.configs[0] as {
      data: {
        nodes: Array<{ id: string; style: { x: number; y: number } }>
        edges: unknown[]
      }
      layout?: Record<string, unknown>
      behaviors: Array<string | { type: string }>
    }
    // UX P0：默认简洁视图只渲染骨干（source_row 明细随 fact 折叠）。
    // 泳道下线：不再注入 lane: 前缀伪节点；列名由「全局概览」列胶囊统一承载。
    const docNodes = cfg.data.nodes
    expect(docNodes).toHaveLength(2)
    expect(docNodes.map((n: { id: string }) => n.id)).toEqual([
      'rule:R1',
      'fact:f1',
    ])
    expect(cfg.data.edges).toHaveLength(1)
    // 无泳道伪节点
    expect(
      cfg.data.nodes.filter((n: { id: string }) => n.id.startsWith('lane:')),
    ).toHaveLength(0)
    // M3：preset 按数据 x/y 渲染（不再挂 dagre，重排走 RC-201 纯函数）
    expect(cfg.layout).toBeUndefined()
    expect(cfg.data.nodes[0].style).toEqual({ x: 0, y: 0 })
    expect(cfg.data.nodes[1].style).toEqual({ x: 260, y: 0 })
    // 平移/缩放 + M3 节点拖拽/点击连线
    const behaviorTypes = cfg.behaviors.map((b) =>
      typeof b === 'string' ? b : b.type)
    expect(behaviorTypes).toContain('drag-canvas')
    expect(behaviorTypes).toContain('zoom-canvas')
    expect(behaviorTypes).toContain('drag-element')
    expect(behaviorTypes).toContain('create-edge')
  })

  it('shows semantic banner when semantic_ready=false', async () => {
    const w = mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope({ semantic_ready: false })) },
    ])
    await flushPromises()
    const banner = w.find('[data-testid="canvas-semantic-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('语义层未构建')
    expect(banner.text()).toContain('数据行溯源仍可使用')
  })

  it('shows empty state when seed doc has no nodes', async () => {
    const w = mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(
          envelope({ doc: { nodes: [], edges: [] } })) },
    ])
    await flushPromises()
    const empty = w.find('[data-testid="canvas-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('暂无可溯源事实')
    expect(w.find('[data-testid="canvas-graph"]').exists()).toBe(false)
  })
})

describe('ResearchCanvas 错误重试', () => {
  it('shows error then retries successfully', async () => {
    let failures = 1
    const w = mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => {
          if (failures-- > 0) throw new Error('network down')
          return okEnvelope(envelope())
        } },
    ])
    await flushPromises()
    const box = w.find('[data-testid="canvas-error"]')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('画布加载失败，请重试')

    await w.find('button').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="canvas-error"]').exists()).toBe(false)
    expect(w.find('[data-testid="canvas-graph"]').exists()).toBe(true)
  })
})

describe('ResearchCanvas RC-207 降级列表', () => {
  beforeEach(() => {
    mocks.failRender = true
  })

  it('falls back to node-edge list with full counts and allows retry', async () => {
    const w = mountCanvas([
      { match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope()) },
    ])
    await flushPromises()

    const fb = w.find('[data-testid="canvas-fallback"]')
    expect(fb.exists()).toBe(true)
    expect(fb.text()).toContain('已降级为节点-关系列表')
    // 节点/边数量与 doc 一致（禁空白、不丢信息）
    expect(w.findAll('.fb-node')).toHaveLength(3)
    expect(w.findAll('.fb-edge')).toHaveLength(2)
    expect(fb.text()).toContain('事实一')
    expect(fb.text()).toContain('命中')
    expect(fb.text()).toContain('来源行')
    // 图形视图未挂载
    expect(w.find('[data-testid="canvas-graph"]').exists()).toBe(false)

    // 重试图形视图：G6 恢复后回到图形
    mocks.failRender = false
    const buttons = fb.findAll('button')
    await buttons[buttons.length - 1].trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="canvas-graph"]').exists()).toBe(true)
    expect(w.find('[data-testid="canvas-fallback"]').exists()).toBe(false)
  })
})
