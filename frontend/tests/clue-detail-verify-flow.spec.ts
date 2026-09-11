import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { h } from 'vue'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { setTransport, getTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import ClueDetailView from '../src/views/ClueDetailView.vue'
import { useAuthStore } from '../src/stores/auth'
import { useCaseStore } from '../src/stores/case'
import { DEFAULT_ONTOLOGY_CONFIG } from '../src/stores/ontologyConfig'
import type { VerifyItemsPage } from '../src/domain/verify'

// REQ-V-008 联动闭环（P1+P2）：
// 202 只代表入队，页面必须等到任务终态（waitForTerminal 轮询 GET /tasks/{id}）
// 才允许刷新读面/提示成败。FakeTransport 以「PENDING → 终态」序列应答模拟 Worker
// 秒级 FIFO 的真实时序，配合假时钟推进轮询间隔：
//   A 裁决 SUCCEEDED 后工作区刷新、门禁警示按新 pending=0 消失；
//   B confirm FAILED/VERIFY_PENDING：红条提示含服务端文案，线索详情不重读，
//     工作区回拉同步计数；
//   C confirm SUCCEEDED：成功提示 + 线索详情重读。

type ItemSeed = [text: string, status: string]

function verifyPage(items: ItemSeed[]): VerifyItemsPage {
  const byStatus: Record<string, number> = {}
  for (const [, s] of items) byStatus[s] = (byStatus[s] ?? 0) + 1
  const concluded = (byStatus['已证实'] ?? 0)
    + (byStatus['已查否'] ?? 0) + (byStatus['无法核实'] ?? 0)
  return {
    available: true,
    items: items.map(([text, status], i) => ({
      item_id: `vi-${i + 1}`,
      kind: 'manual',
      text,
      origin: 'manual',
      status,
      conclusion: status === '已证实' ? '流水与公告时间耦合，予以证实' : '',
      operator: status === '待核查' ? '' : '王检察官',
      updated_at: '2026-09-12T10:00:00',
      channel: '',
      ref_function: '',
      external: null,
      falsification: '',
    })),
    progress: {
      total: items.length,
      concluded,
      pending: (byStatus['待核查'] ?? 0) + (byStatus['核查中'] ?? 0),
      suggested: byStatus['建议'] ?? 0,
      ignored: byStatus['已忽略'] ?? 0,
      by_status: byStatus,
    },
  }
}

function taskRow(id: string, status: string, extra: Record<string, unknown> = {}) {
  return {
    id, case_id: 'c1', task_type: 'DISPOSE', params: {}, status,
    progress_pct: 0, progress_stage: '', progress_label: '', progress_detail: '',
    retry_count: 0, max_retries: 3, idem_key: '',
    created_at: '', updated_at: '', started_at: '', finished_at: '',
    error_code: '', error_message: '', created_by: '王检察官',
    ...extra,
  }
}

/** 终态序列：每次 GET /tasks/{id} 推进一帧；进入终态帧时先跑 onTerminal（模拟 Worker 先落 state） */
interface TaskScript {
  seq: Array<Record<string, unknown>>
  onTerminal?: () => void
}

interface Harness {
  wrapper: VueWrapper
  /** 动态改写 verify-items 应答（模拟 Worker 消费后的新 state） */
  setItems: (items: ItemSeed[]) => void
  setDisposeScript: (script: TaskScript) => void
  setVerifyScript: (script: TaskScript) => void
}

function countCalls(pred: (method: string, path: string) => boolean): number {
  const t = getTransport() as unknown as { calls?: Array<{ method: string; path: string }> }
  return (t.calls ?? []).filter((c) => pred(c.method, c.path)).length
}

async function mountHarness(initialItems: ItemSeed[]): Promise<Harness> {
  let items: ItemSeed[] = [...initialItems]
  const disposeScript: { current: TaskScript } = {
    current: { seq: [taskRow('t-d1', 'PENDING'), taskRow('t-d1', 'SUCCEEDED')] },
  }
  const verifyScript: { current: TaskScript } = {
    current: { seq: [taskRow('t-v1', 'PENDING', { task_type: 'VERIFY' }),
      taskRow('t-v1', 'SUCCEEDED', { task_type: 'VERIFY' })] },
  }
  const pollCalls: Record<string, { n: number }> = {}

  const routes = [
    {
      match: (r: { method: string; path: string }) =>
        r.method === 'GET' && r.path === '/cases/c1/clues/clue-1',
      respond: () => okEnvelope({
        clue_id: 'clue-1',
        title: '张某与中标方窗口期资金耦合',
        status: '查证中',
        skill_id: 'time_window',
        source_rows: [],
        evidence: [],
      }),
    },
    {
      match: (r: { method: string; path: string }) =>
        r.method === 'GET' && r.path.includes('/clues/clue-1/verify-items'),
      respond: () => okEnvelope(verifyPage(items)),
    },
    {
      match: (r: { method: string; path: string }) =>
        r.method === 'GET' && r.path === '/cases/c1/ontology-config',
      respond: () => okEnvelope(DEFAULT_ONTOLOGY_CONFIG),
    },
    {
      // DISPOSE 入队回执（202 信封只看 body.ok）
      match: (r: { method: string; path: string }) =>
        r.method === 'POST' && r.path.endsWith('/actions'),
      respond: () => ({ status: 202, data: { ok: true, data: taskRow('t-d1', 'PENDING') } }),
    },
    {
      // VERIFY 裁决入队回执
      match: (r: { method: string; path: string }) =>
        r.method === 'POST' && r.path.endsWith('/transitions'),
      respond: () => ({
        status: 202,
        data: { ok: true, data: taskRow('t-v1', 'PENDING', { task_type: 'VERIFY' }) },
      }),
    },
    {
      match: (r: { method: string; path: string }) =>
        r.method === 'GET' && (r.path === '/tasks/t-d1' || r.path === '/tasks/t-v1'),
      respond: (r: { path: string }) => {
        const id = r.path.endsWith('t-d1') ? 't-d1' : 't-v1'
        const script = id === 't-d1' ? disposeScript.current : verifyScript.current
        const cur = pollCalls[id] ?? (pollCalls[id] = { n: 0 })
        const frame = script.seq[Math.min(cur.n, script.seq.length - 1)]
        cur.n += 1
        const status = String(frame.status)
        if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(status) && cur.n <= script.seq.length) {
          script.onTerminal?.()
        }
        return okEnvelope(frame)
      },
    },
  ]

  setTransport(new FakeTransport(routes))

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/c/clue/:clueId', component: { template: '<div />' } },
      { path: '/c/clues', component: { template: '<div />' } },
      { path: '/c/audit-chain', component: { template: '<div />' } },
    ],
  })
  await router.push('/c/clue/clue-1')
  await router.isReady()

  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(ClueDetailView) },
    global: { plugins: [router] },
  })
  await flushPromises()
  await flushPromises()

  return {
    wrapper,
    setItems: (next: ItemSeed[]) => { items = [...next] },
    setDisposeScript: (s: TaskScript) => { disposeScript.current = s },
    setVerifyScript: (s: TaskScript) => { verifyScript.current = s },
  }
}

// ---------- 弹窗操作辅助（modal teleport 到 body） ----------
const bodyButton = (label: string) =>
  Array.from(document.body.querySelectorAll('button'))
    .find((b) => b.textContent?.trim() === label) as HTMLButtonElement | undefined

async function openCsmModal(wrapper: VueWrapper, label: string): Promise<void> {
  const btn = wrapper.findAll('button').find((b) => b.text().trim() === label)
  if (!btn) throw new Error(`CSM 按钮未渲染：${label}`)
  await btn.trigger('click')
  await flushPromises()
}

async function clickWorkbenchAction(
  wrapper: VueWrapper, rowIdx: number, label: string,
): Promise<void> {
  const btn = wrapper.findAll('.vw-item')[rowIdx]
    .findAll('button').find((b) => b.text().trim() === label)
  if (!btn) throw new Error(`工作台动作未渲染：${label}`)
  await btn.trigger('click')
  await flushPromises()
}

const warnEl = () => document.body.querySelector('[data-testid="csm-verify-warn"]')

/** 推进假时钟越过若干轮询间隔（waitForTerminal 默认 500ms），并排空微任务 */
async function advancePolls(ticks: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(600 * ticks)
  await flushPromises()
  vi.useRealTimers()
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.me = {
    operator: '王检察官', role: 'human', clearance: 4, tenant_id: 't1',
    is_admin: false,
  } as ReturnType<typeof useAuthStore>['me']
  useCaseStore().$patch({ currentCaseId: 'c1' })
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('REQ-V-008 联动闭环：202 入队 → 任务终态 → 读面刷新', () => {
  it('A 裁决终态后才刷新：门禁警示随 pending 归零消失（终态前读到旧值不误判）', async () => {
    const h = await mountHarness([['未结事项甲', '待核查']])
    const detailGets = () => countCalls((m, p) =>
      m === 'GET' && p === '/cases/c1/clues/clue-1')
    const verifyGets = () => countCalls((m, p) =>
      m === 'GET' && p.includes('/verify-items'))
    expect(detailGets()).toBe(1)
    expect(verifyGets()).toBe(1)

    // 前置：pending=1 时固证弹窗有琥珀警示
    await openCsmModal(h.wrapper, '固证')
    expect(warnEl()?.textContent).toContain('1 项核查未结')
    await new DOMWrapper(bodyButton('取消')!).trigger('click')
    await flushPromises()

    // 工作台裁决：待核查 → 已证实（结论必填）
    await clickWorkbenchAction(h.wrapper, 0, '证实')
    const ta = document.body.querySelector('[data-testid="vw-conclusion"] textarea') as HTMLTextAreaElement
    await new DOMWrapper(ta).setValue('流水与中标公告时间耦合，予以证实')
    await flushPromises()

    // Worker 在终态帧才落新 state（脚本翻转 verify-items 应答）
    h.setVerifyScript({
      seq: [
        taskRow('t-v1', 'PENDING', { task_type: 'VERIFY' }),
        taskRow('t-v1', 'RUNNING', { task_type: 'VERIFY' }),
        taskRow('t-v1', 'SUCCEEDED', { task_type: 'VERIFY' }),
      ],
      onTerminal: () => h.setItems([['未结事项甲', '已证实']]),
    })

    vi.useFakeTimers()
    // happy-dom 不派发 transitionend，已取消的 CSM 弹窗残留在 body（不可见）；
    // 工作台确认按钮必须按 data-testid 精确取，避免误点残留弹窗的同名按钮
    const vwConfirm = document.body.querySelector(
      '[data-testid="vw-confirm-btn"]',
    ) as HTMLButtonElement
    await new DOMWrapper(vwConfirm).trigger('click')
    await advancePolls(2) // PENDING→RUNNING→SUCCEEDED 跨两个间隔

    // 轮询了 3 帧（PENDING/RUNNING/SUCCEEDED），终态后工作区恰好刷新一次
    const taskPolls = countCalls((m, p) => m === 'GET' && p === '/tasks/t-v1')
    expect(taskPolls).toBe(3)
    expect(verifyGets()).toBe(2) // 初始 1 + 终态后刷新 1（入队时未抢跑）
    expect(document.body.textContent).toContain('核查裁决已完成')
    // 行状态已翻为已证实，动作只剩重开
    const row = h.wrapper.findAll('.vw-item')[0]
    expect(row.text()).toContain('已证实')
    expect(row.findAll('button').map((b) => b.text().trim())).toEqual(['重开'])

    // 门禁计数走新 progress.pending：警示消失
    await openCsmModal(h.wrapper, '固证')
    expect(warnEl()).toBeNull()
  })

  it('B confirm 被 VERIFY_PENDING 拒绝：红条含服务端文案；详情不重读；工作区回拉', async () => {
    const h = await mountHarness([
      ['未结事项甲', '待核查'],
      ['未结事项乙', '核查中'],
    ])
    const detailGets = () => countCalls((m, p) =>
      m === 'GET' && p === '/cases/c1/clues/clue-1')
    const verifyGets = () => countCalls((m, p) =>
      m === 'GET' && p.includes('/verify-items'))
    expect(detailGets()).toBe(1)

    h.setDisposeScript({
      seq: [
        taskRow('t-d1', 'PENDING'),
        taskRow('t-d1', 'FAILED', {
          error_code: 'VERIFY_PENDING',
          error_message: '尚有 2 项核查未结：未结事项甲；未结事项乙…（先逐项得出结论）',
        }),
      ],
    })

    await openCsmModal(h.wrapper, '固证')
    expect(warnEl()?.textContent).toContain('2 项核查未结')

    vi.useFakeTimers()
    await new DOMWrapper(bodyButton('确认提交')!).trigger('click')
    await advancePolls(1)

    // 失败如实红条展示，含错误码语义与服务端可操作文案
    const msgText = document.body.textContent ?? ''
    expect(msgText).toContain('处置被核查门禁拦截')
    expect(msgText).toContain('尚有 2 项核查未结')
    // 状态未变更 → 不重读线索详情；门禁失败仍回拉工作区同步计数
    expect(detailGets()).toBe(1)
    expect(verifyGets()).toBe(2)
    // 提交状态已释放：固证动作按钮恢复可点（非死锁）。
    // 注：不断言弹窗 DOM 卸载——happy-dom 不派发 transitionend，NModal 会残留（不可见）。
    const confirmBtn = h.wrapper.findAll('button')
      .find((b) => b.text().trim() === '固证')
    expect(confirmBtn).toBeDefined()
    expect(confirmBtn!.attributes('disabled')).toBeUndefined()
  })

  it('C confirm SUCCEEDED：成功提示 + 线索详情重读 + 工作区刷新', async () => {
    const h = await mountHarness([]) // 无核查项：门禁放行
    const detailGets = () => countCalls((m, p) =>
      m === 'GET' && p === '/cases/c1/clues/clue-1')
    const verifyGets = () => countCalls((m, p) =>
      m === 'GET' && p.includes('/verify-items'))
    expect(detailGets()).toBe(1)

    h.setDisposeScript({
      seq: [taskRow('t-d1', 'PENDING'), taskRow('t-d1', 'SUCCEEDED')],
    })

    await openCsmModal(h.wrapper, '固证')
    expect(warnEl()).toBeNull()
    vi.useFakeTimers()
    await new DOMWrapper(bodyButton('确认提交')!).trigger('click')
    await advancePolls(1)

    expect(document.body.textContent).toContain('处置完成')
    expect(detailGets()).toBe(2)
    expect(verifyGets()).toBe(2)
  })
})
