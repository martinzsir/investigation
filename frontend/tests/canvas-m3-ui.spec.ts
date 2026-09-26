import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { NConfigProvider, NDialogProvider, NMessageProvider, zhCN } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import ResearchCanvas from '../src/components/research/ResearchCanvas.vue'
import type { CanvasDoc, CanvasEnvelope, CanvasNode } from '../src/domain/canvas'

// M3 画布交互（RC-201/202/203/206）：
//  - 工具栏 → 人工节点新增（坐标落点 + 乐观锁版本）；
//  - 拖拽结束自动钉住 + 500ms 防抖 PATCH；重新排版只动未钉住节点；
//  - 连线模式 + G6 onCreate 拦截：矩阵唯一关系直连、非法 toast、
//    多关系气泡选择、重复边拒绝；
//  - 快照列表/创建/回滚二次确认。

const PATH = '/cases/c1/clues/clue-1/canvas'

const mocks = vi.hoisted(() => ({
  configs: [] as any[],
  handlers: {} as Record<string, (ev: unknown) => void>,
  positions: new Map<string, [number, number]>(),
}))

vi.mock('@antv/g6', () => ({
  Graph: class {
    constructor(cfg: unknown) {
      mocks.configs.push(cfg)
    }
    async render() {}
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

function baseDoc(): CanvasDoc {
  return {
    nodes: [
      n('rule:R1', 'rule', '规则R1', 0, 0, { ref: 'R1' }),
      n('fact:f1', 'fact', '事实一', 260, 0, { ref: 'fact:f1' }),
      n('evidence:ev1', 'evidence', '书证一', 480, 0, { ref: 'ev1' }),
      n('cn_1', 'hypothesis', '假设一', 480, 104, { system: false }),
    ],
    edges: [
      { id: 'e1', source: 'rule:R1', target: 'fact:f1', rel: '命中', system: true },
    ],
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
    doc: baseDoc(),
    ...over,
  }
}

let wrapper: VueWrapper | null = null

function mountWith(t: FakeTransport): VueWrapper {
  // ResearchCanvas 经 useCaseOntologyConfig 触达 pinia store（拉取失败回落 DEFAULT，不阻塞）
  setActivePinia(createPinia())
  setTransport(t)
  wrapper = mount({
    components: {
      NConfigProvider, NMessageProvider, NDialogProvider, ResearchCanvas,
    },
    setup() {
      return { zhCN }
    },
    template: `
      <NConfigProvider :locale="zhCN">
        <NMessageProvider><NDialogProvider>
          <ResearchCanvas case-id="c1" clue-id="clue-1" />
        </NDialogProvider></NMessageProvider>
      </NConfigProvider>`,
  })
  return wrapper
}

function setValue(el: Element | null, value: string): void {
  const target = el as HTMLInputElement | HTMLTextAreaElement | null
  if (!target) throw new Error('input element not found')
  target.value = value
  target.dispatchEvent(new Event('input', { bubbles: true }))
}

function onCreate(): (draft: unknown) => unknown {
  const cfg = mocks.configs[0] as {
    behaviors: Array<Record<string, unknown> | string>
  }
  const ce = cfg.behaviors.find(
    (b): b is Record<string, unknown> =>
      typeof b === 'object' && b.type === 'create-edge',
  )
  return ce!.onCreate as (draft: unknown) => unknown
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
  Object.keys(mocks.handlers).forEach((k) => delete mocks.handlers[k])
  mocks.positions.clear()
  setTransport(null)
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('RC-202 工具栏新增人工节点', () => {
  it('creates hypothesis via POST /nodes at next slot with current version', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/nodes`,
        respond: (req) => {
          const body = req.body as {
            kind: string; props: Record<string, unknown>
            x: number; y: number; version: number
          }
          const doc = baseDoc()
          doc.nodes.push(n('cn_2', body.kind as CanvasNode['kind'],
            String(body.props.title), body.x, body.y, { system: false,
            props: body.props }))
          return okEnvelope({
            ...envelope({ version: 2, doc }),
            node: doc.nodes.at(-1),
          })
        },
      },
    ])
    const w = mountWith(t)
    await flushPromises()

    await w.find('[data-testid="tb-add-hypothesis"]').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[data-testid="manual-node-modal"]')).toBeTruthy()

    setValue(document.body.querySelector(
      '[data-testid="manual-node-title"] input'), '张某为实际控制人')
    setValue(document.body.querySelector(
      '[data-testid="manual-node-content"] textarea'), '整数现金与轨迹吻合')
    await flushPromises()
    ;(document.body.querySelector(
      '[data-testid="manual-node-submit"]') as HTMLButtonElement).click()
    await flushPromises()

    const call = t.calls.find((c) => c.method === 'POST' && c.path === `${PATH}/nodes`)
    expect(call).toBeTruthy()
    const body = call!.body as Record<string, unknown>
    expect(body.kind).toBe('hypothesis')
    expect(body.version).toBe(1)
    // 假设列 x=480；同列已有节点最高 y=104（cn_1），新节点落 y=208
    expect(body.x).toBe(480)
    expect(body.y).toBe(208)
    expect(body.props).toEqual({ title: '张某为实际控制人', content: '整数现金与轨迹吻合' })
    expect(document.body.textContent).toContain('侦查假设已添加')
    w.unmount()
  })
})

describe('RC-201 拖拽钉住与重排（防抖 PATCH）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('dragend marks node pinned and saves coordinates after 500ms debounce', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'PATCH' && r.path === PATH,
        respond: (req) => {
          const body = req.body as { doc: CanvasDoc; version: number }
          return okEnvelope(envelope({ version: 2, doc: body.doc }))
        },
      },
    ])
    mountWith(t)
    // fake timers 下 flushPromises 的 setTimeout 也被接管，用 advance 驱动
    await vi.advanceTimersByTimeAsync(10)

    mocks.positions.set('fact:f1', [305.222, 208])
    mocks.handlers['node:dragend']({ target: { id: 'fact:f1' } })
    // 500ms 内无保存
    await vi.advanceTimersByTimeAsync(400)
    expect(t.calls.some((c) => c.method === 'PATCH')).toBe(false)
    await vi.advanceTimersByTimeAsync(200)

    const patch = t.calls.find((c) => c.method === 'PATCH' && c.path === PATH)
    expect(patch).toBeTruthy()
    const body = patch!.body as { doc: CanvasDoc; version: number }
    expect(body.version).toBe(1)
    const f1 = body.doc.nodes.find((x) => x.id === 'fact:f1')!
    expect(f1.pinned).toBe(true)
    expect(f1.x).toBe(305.22)
    expect(f1.y).toBe(208)
  })

  it('relayout only moves unpinned nodes and preserves pinned coordinates', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'PATCH' && r.path === PATH,
        respond: (req) => {
          const body = req.body as { doc: CanvasDoc; version: number }
          return okEnvelope(envelope({ version: 2, doc: body.doc }))
        },
      },
    ])
    const w = mountWith(t)
    await vi.advanceTimersByTimeAsync(10)

    // 把假设一钉在非常规坐标
    mocks.positions.set('cn_1', [999, 555])
    mocks.handlers['node:dragend']({ target: { id: 'cn_1' } })
    await vi.advanceTimersByTimeAsync(500)

    await w.find('[data-testid="tb-relayout"]').trigger('click')
    await vi.advanceTimersByTimeAsync(500)

    const patches = t.calls.filter((c) => c.method === 'PATCH' && c.path === PATH)
    const last = patches.at(-1)!.body as { doc: CanvasDoc }
    const byId = new Map(last.doc.nodes.map((x) => [x.id, x]))
    // 未钉住归列
    expect(byId.get('rule:R1')!).toMatchObject({ x: 0, y: 0 })
    expect(byId.get('fact:f1')!).toMatchObject({ x: 260, y: 0 })
    // 钉住节点原样
    expect(byId.get('cn_1')!).toMatchObject({ x: 999, y: 555, pinned: true })
  })
})

describe('RC-203 连线模式与矩阵拦截', () => {
  it('connect mode toggles hint; single-legal pair auto-creates; illegal rejected', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/edges`,
        respond: (req) => {
          const body = req.body as Record<string, unknown>
          const doc = baseDoc()
          doc.edges.push({
            id: `e:${body.source}--${body.rel}--${body.target}`,
            source: String(body.source),
            target: String(body.target),
            rel: String(body.rel),
            system: false,
          })
          return okEnvelope({
            ...envelope({ version: 2, doc }),
            edge: doc.edges.at(-1),
          })
        },
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await w.find('[data-testid="tb-connect"]').trigger('click')
    expect(w.find('[data-testid="connect-hint"]').exists()).toBe(true)

    // 唯一合法关系：事实 → 假设（推断为），直连
    expect(onCreate()({ source: 'fact:f1', target: 'cn_1' })).toBeUndefined()
    await flushPromises()
    const edgeCall = t.calls.find(
      (c) => c.method === 'POST' && c.path === `${PATH}/edges`)
    expect((edgeCall!.body as Record<string, unknown>).rel).toBe('推断为')
    expect(document.body.textContent).toContain('已建立「推断为」关系')

    // 非法：事实 → 规则
    onCreate()({ source: 'fact:f1', target: 'rule:R1' })
    await flushPromises()
    expect(document.body.textContent).toContain('该两类节点之间不能建立人工关系')

    // 再次点工具栏退出连线模式
    await w.find('[data-testid="tb-connect"]').trigger('click')
    expect(w.find('[data-testid="connect-hint"]').exists()).toBe(false)
    w.unmount()
  })

  it('pops relation chooser when multiple legal rels and submits picked rel', async () => {
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/edges`,
        respond: (req) => {
          const body = req.body as Record<string, unknown>
          const doc = baseDoc()
          doc.edges.push({
            id: 'e_new', source: String(body.source),
            target: String(body.target), rel: String(body.rel), system: false,
          })
          return okEnvelope({
            ...envelope({ version: 2, doc }),
            edge: doc.edges.at(-1),
          })
        },
      },
    ])
    const w = mountWith(t)
    await flushPromises()

    // 书证 → 假设：证实/查否 二选一
    onCreate()({ source: 'evidence:ev1', target: 'cn_1' })
    await flushPromises()
    expect(w.find('[data-testid="edge-create-popover"]').exists()).toBe(true)
    expect(w.find('[data-testid="edge-rel-证实"]').exists()).toBe(true)
    expect(w.find('[data-testid="edge-rel-查否"]').exists()).toBe(true)
    expect(w.find('[data-testid="edge-rel-推断为"]').exists()).toBe(false)

    await w.find('[data-testid="edge-rel-证实"]').trigger('click')
    await flushPromises()
    const edgeCall = t.calls.find(
      (c) => c.method === 'POST' && c.path === `${PATH}/edges`)
    expect((edgeCall!.body as Record<string, unknown>).rel).toBe('证实')
    w.unmount()
  })

  it('rejects duplicate edge without request', async () => {
    const doc = baseDoc()
    doc.edges.push({
      id: 'dup', source: 'fact:f1', target: 'cn_1',
      rel: '推断为', system: false,
    })
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === PATH,
        respond: () => okEnvelope(envelope({ doc })),
      },
      {
        match: () => true,
        respond: () => {
          throw new Error('should not POST')
        },
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    onCreate()({ source: 'fact:f1', target: 'cn_1' })
    await flushPromises()
    expect(document.body.textContent).toContain('该连线已存在')
    expect(t.calls.some((c) => c.method === 'POST')).toBe(false)
    w.unmount()
  })
})

describe('RC-206 快照', () => {
  it('lists snapshots, creates immediately and rolls back with confirm', async () => {
    const snap = {
      snapshot_id: 'snap_1', clue_id: 'clue-1', label: '初查',
      origin: 'manual', created_by: '李侦查员', created_at: '2026-09-13 10:00',
      node_count: 4, edge_count: 1, doc: baseDoc(),
    }
    const t = new FakeTransport([
      ...getRoute,
      {
        match: (r) => r.method === 'GET' && r.path === `${PATH}/snapshots`,
        respond: () => okEnvelope({ snapshots: [snap] }),
      },
      {
        match: (r) => r.method === 'POST' && r.path === `${PATH}/snapshots`,
        respond: () => {
          const created = { ...snap, snapshot_id: 'snap_2', label: '假设定稿' }
          return okEnvelope({ ...envelope(), snapshot: created })
        },
      },
      {
        match: (r) => r.method === 'POST'
          && r.path === `${PATH}/snapshots/snap_1/rollback`,
        respond: () => okEnvelope({
          ...envelope({ version: 3, doc: snap.doc }),
          target_snapshot_id: 'snap_1',
          recovery_snapshot_id: 'snap_3',
          stale_node_ids: [],
          recovery_snapshot: { ...snap, snapshot_id: 'snap_3',
            label: '回滚前自动恢复点（snap_2）' },
        }),
      },
    ])
    const w = mountWith(t)
    await flushPromises()
    await w.find('[data-testid="tb-snapshots"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('初查')

    setValue(document.body.querySelector(
      '[data-testid="snapshot-label"] input'), '假设定稿')
    await flushPromises()
    ;(document.body.querySelector(
      '[data-testid="snapshot-create"]') as HTMLButtonElement).click()
    await flushPromises()
    const createCall = t.calls.find(
      (c) => c.method === 'POST' && c.path === `${PATH}/snapshots`)
    expect(createCall).toBeTruthy()
    expect((createCall!.body as Record<string, unknown>).version).toBe(1)

    // 回滚：确认气泡
    ;(document.body.querySelector(
      '[data-testid="snapshot-rollback-snap_1"]') as HTMLButtonElement).click()
    await flushPromises()
    const confirm = Array.from(document.body.querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === '确认') as HTMLButtonElement
    confirm.click()
    await flushPromises()
    const rollbackCall = t.calls.find(
      (c) => c.method === 'POST'
        && c.path === `${PATH}/snapshots/snap_1/rollback`)
    expect(rollbackCall).toBeTruthy()
    expect(document.body.textContent).toContain('已回滚到所选快照')
    w.unmount()
  })
})
