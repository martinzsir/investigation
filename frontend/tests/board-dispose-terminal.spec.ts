import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { h } from 'vue'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { setTransport, getTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import BoardView from '../src/views/BoardView.vue'
import { useAuthStore } from '../src/stores/auth'
import { useCaseStore } from '../src/stores/case'
import { DEFAULT_ONTOLOGY_CONFIG } from '../src/stores/ontologyConfig'
import type { ClueListItem } from '../src/api/endpoints/clues'

// BoardView「入队即成功」缺口修复（REQ-V-008 闭环，与 ClueDetailView 同源纪律）：
// 202 只代表入队，看板必须轮询 GET /tasks/{id} 到终态后才提示成败/重读列表。
// FakeTransport 以「PENDING → 终态」序列应答模拟 Worker 秒级 FIFO：
//   A SUCCEEDED：终态前不提示成功、不抢刷列表且互斥置灰；终态后提示并重拉，卡片移列；
//   B FAILED/VERIFY_PENDING：红条含服务端文案，列表不重读（状态未变），置灰释放；
//   C CANCELLED：警示提示，列表不重读，置灰释放。

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

interface TaskScript {
  seq: Array<Record<string, unknown>>
  onTerminal?: () => void
}

interface Controls {
  wrapper: VueWrapper
  setStatus: (s: string) => void
  setScript: (s: TaskScript) => void
  listGets: () => number
  taskPolls: () => number
  actionPosts: () => number
}

function countCalls(pred: (method: string, path: string) => boolean): number {
  const t = getTransport() as unknown as { calls?: Array<{ method: string; path: string }> }
  return (t.calls ?? []).filter((c) => pred(c.method, c.path)).length
}

function clueItem(status: string): ClueListItem {
  return {
    clue_id: 'clue-1',
    title: '张某与中标方窗口期资金耦合',
    status: status as ClueListItem['status'],
    skill_id: 'time_window',
    operator: '王检察官',
    updated_at: '2026-09-12T10:00:00',
  }
}

const bodyButton = (label: string) =>
  Array.from(document.body.querySelectorAll('button'))
    .find((b) => b.textContent?.trim() === label) as HTMLButtonElement | undefined

async function mountBoard(script: TaskScript): Promise<Controls> {
  let listStatus = '查证中'
  let curScript = script
  const pollState: { n: number } = { n: 0 }

  const routes = [
    {
      // 看板列表（BoardView load：page=1&page_size=200）
      match: (r: { method: string; path: string }) =>
        r.method === 'GET'
        && r.path.startsWith('/cases/c1/clues')
        && !r.path.includes('verify-items'),
      respond: () => okEnvelope({
        items: [clueItem(listStatus)], total: 1, page: 1, page_size: 200,
      }),
    },
    {
      match: (r: { method: string; path: string }) =>
        r.method === 'GET' && r.path === '/cases/c1/ontology-config',
      respond: () => okEnvelope(DEFAULT_ONTOLOGY_CONFIG),
    },
    {
      // DISPOSE 入队回执（202 只看信封 ok；body 为 PENDING 任务行）
      match: (r: { method: string; path: string }) =>
        r.method === 'POST' && r.path.endsWith('/actions'),
      respond: () => ({ status: 202, data: { ok: true, data: taskRow('t-b1', 'PENDING') } }),
    },
    {
      match: (r: { method: string; path: string }) =>
        r.method === 'GET' && r.path === '/tasks/t-b1',
      respond: () => {
        const frame = curScript.seq[Math.min(pollState.n, curScript.seq.length - 1)]
        pollState.n += 1
        const status = String(frame.status)
        if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(status)
          && pollState.n <= curScript.seq.length) {
          curScript.onTerminal?.()
        }
        return okEnvelope(frame)
      },
    },
  ]

  setTransport(new FakeTransport(routes))

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/board', component: { template: '<div />' } }],
  })
  await router.push('/board')
  await router.isReady()

  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(BoardView) },
    global: { plugins: [router] },
  })
  await flushPromises()
  await flushPromises()

  return {
    wrapper,
    setStatus: (s: string) => { listStatus = s },
    setScript: (s: TaskScript) => { curScript = s; pollState.n = 0 },
    listGets: () => countCalls((m, p) => m === 'GET' && p.startsWith('/cases/c1/clues?')),
    taskPolls: () => countCalls((m, p) => m === 'GET' && p === '/tasks/t-b1'),
    actionPosts: () => countCalls((m, p) => m === 'POST' && p.endsWith('/actions')),
  }
}

/** 打开卡片「固证」确认弹窗并点确认（emit submit 后 CSM 弹窗关闭） */
async function clickConfirmDispose(wrapper: VueWrapper): Promise<void> {
  const btn = wrapper.findAll('button').find((b) => b.text().trim() === '固证')
  if (!btn) throw new Error('固证按钮未渲染')
  await btn.trigger('click')
  await flushPromises()
  const ok = bodyButton('确认提交')
  if (!ok) throw new Error('处置确认按钮未渲染')
  await new DOMWrapper(ok).trigger('click')
}

function actionButton(wrapper: VueWrapper, label: string) {
  return wrapper.findAll('button').find((b) => b.text().trim() === label)
}

/** 查证中列卡片上的固证按钮 */
const guzhengButton = (wrapper: VueWrapper) => actionButton(wrapper, '固证')

/** 按列头名称取看板列 section */
function columnByName(wrapper: VueWrapper, name: string) {
  return wrapper.findAll('.kcol').find((el) => el.find('.kcol-name').text() === name)
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

describe('BoardView 处置闭环：202 入队 → 任务终态 → 提示/刷新', () => {
  it('A SUCCEEDED：终态前不报成功/不抢刷/互斥置灰；终态后提示并重拉，卡片移入已固证列', async () => {
    const h = await mountBoard({
      seq: [taskRow('t-b1', 'PENDING'), taskRow('t-b1', 'SUCCEEDED')],
      onTerminal: () => h.setStatus('已固证'),
    })
    expect(h.listGets()).toBe(1)

    vi.useFakeTimers()
    await clickConfirmDispose(h.wrapper)
    await flushPromises() // POST 入队 + 首轮轮询（PENDING）

    // 202 不等于成功：无成功/入队提示，列表未抢刷，写按钮互斥置灰
    expect(h.actionPosts()).toBe(1)
    expect(h.taskPolls()).toBe(1)
    expect(h.listGets()).toBe(1)
    const bodyText = document.body.textContent ?? ''
    expect(bodyText).not.toContain('处置完成')
    expect(bodyText).not.toContain('已入队')
    expect(guzhengButton(h.wrapper)?.attributes('disabled')).toBeDefined()

    // Worker 落终态（onTerminal 已翻转列表应答）→ 再越一个轮询间隔
    await vi.advanceTimersByTimeAsync(600)
    await flushPromises()

    expect(h.taskPolls()).toBe(2)
    expect(h.listGets()).toBe(2) // 终态后恰好重拉一次
    expect(document.body.textContent).toContain('处置完成')
    // 互斥释放：卡片已在已固证列，该列「排除线索」按钮恢复可点
    expect(actionButton(h.wrapper, '排除线索')?.attributes('disabled')).toBeUndefined()
    // 卡片由「查证中」列移入「已固证」列
    expect(columnByName(h.wrapper, '已固证')?.text()).toContain('clue-1')
    expect(columnByName(h.wrapper, '查证中')?.text()).not.toContain('clue-1')
  })

  it('B FAILED/VERIFY_PENDING：红条含服务端文案；列表不重读；置灰释放', async () => {
    const h = await mountBoard({
      seq: [
        taskRow('t-b1', 'PENDING'),
        taskRow('t-b1', 'FAILED', {
          error_code: 'VERIFY_PENDING',
          error_message: '尚有 2 项核查未结：未结事项甲；未结事项乙…（先逐项得出结论）',
        }),
      ],
    })

    vi.useFakeTimers()
    await clickConfirmDispose(h.wrapper)
    await flushPromises()
    expect(h.taskPolls()).toBe(1)
    expect(h.listGets()).toBe(1)

    await vi.advanceTimersByTimeAsync(600)
    await flushPromises()

    const text = document.body.textContent ?? ''
    expect(text).toContain('处置被核查门禁拦截')
    expect(text).toContain('尚有 2 项核查未结')
    expect(text).not.toContain('处置完成')
    // 拒绝不产生状态变更 → 不重读看板；按钮恢复可点
    expect(h.listGets()).toBe(1)
    expect(guzhengButton(h.wrapper)?.attributes('disabled')).toBeUndefined()
  })

  it('C CANCELLED：警示提示且列表不重读；置灰释放', async () => {
    const h = await mountBoard({
      seq: [taskRow('t-b1', 'PENDING'), taskRow('t-b1', 'CANCELLED')],
    })

    vi.useFakeTimers()
    await clickConfirmDispose(h.wrapper)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(600)
    await flushPromises()

    expect(document.body.textContent).toContain('处置任务已取消')
    expect(document.body.textContent).not.toContain('处置完成')
    expect(h.listGets()).toBe(1)
    expect(guzhengButton(h.wrapper)?.attributes('disabled')).toBeUndefined()
    expect(columnByName(h.wrapper, '查证中')?.text()).toContain('clue-1')
  })
})
