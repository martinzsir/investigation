import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import VerifyWorkbench from '../src/components/research/VerifyWorkbench.vue'
import type { VerifyItem, VerifyItemsPage } from '../src/domain/verify'

// REQ-V-006 AC-2~6（happy-dom 真实挂载；写动作只 emit，API 由父层编排）。

function item(over: Partial<VerifyItem>): VerifyItem {
  return {
    item_id: 'vi_x',
    kind: 'manual',
    text: '待核实事项',
    origin: 'manual',
    status: '待核查',
    conclusion: '',
    operator: '',
    updated_at: '',
    channel: '',
    ref_function: '',
    external: null,
    falsification: '',
    ...over,
  }
}

const P1 = item({ item_id: 'vi_p1', text: '待核实事项甲', status: '待核查' })
const P2 = item({
  item_id: 'vi_p2', text: '已证实事项乙', status: '已证实',
  conclusion: '流水时间耦合，予以证实', operator: '王检察官',
  updated_at: '2026-09-11 10:00:00',
})
const S1 = item({
  item_id: 'vi_s1', kind: 'suggested', origin: 'suggested',
  text: '调取{subject}账户流水', status: '建议',
  channel: 'function', ref_function: 'time_window_collision',
  falsification: '存在合法合同对价则证伪',
})

function pageOf(items: VerifyItem[], extra?: Partial<VerifyItemsPage>): VerifyItemsPage {
  const byStatus: Record<string, number> = {}
  for (const it of items) byStatus[it.status] = (byStatus[it.status] ?? 0) + 1
  const concluded = ['已证实', '已查否', '无法核实']
    .reduce((n, s) => n + (byStatus[s] ?? 0), 0)
  return {
    items,
    progress: {
      total: items.length,
      concluded,
      pending: (byStatus['待核查'] ?? 0) + (byStatus['核查中'] ?? 0),
      suggested: byStatus['建议'] ?? 0,
      ignored: byStatus['已忽略'] ?? 0,
      by_status: byStatus,
    },
    available: true,
    ...extra,
  }
}

let wrapper: VueWrapper | null = null

function mountWith(page: VerifyItemsPage, props: Record<string, unknown> = {}) {
  const transport = new FakeTransport([
    {
      match: (r) => r.method === 'GET'
        && r.path.includes('/clues/clue-1/verify-items'),
      respond: () => okEnvelope(page),
    },
  ])
  setTransport(transport)
  wrapper = mount(VerifyWorkbench, {
    props: {
      caseId: 'c1',
      clueId: 'clue-1',
      operator: '王检察官',
      role: 'human',
      degraded: false,
      submitting: false,
      ...props,
    },
  })
  return { w: wrapper!, transport }
}

/** NModal teleport 到 body 的元素查询 */
function bodyEl<T extends Element>(selector: string): T {
  const el = document.body.querySelector(selector) as T | null
  if (!el) throw new Error(`body 中未找到：${selector}`)
  return el
}

async function openModal(rowIdx: number, label: string): Promise<void> {
  const btn = wrapper!.findAll('.vw-item')[rowIdx]
    .findAll('button')
    .find((b) => b.text().trim() === label)
  if (!btn) throw new Error(`行 ${rowIdx} 未渲染动作：${label}`)
  await btn.trigger('click')
  await flushPromises()
}

async function setModalTextarea(testid: string, value: string): Promise<void> {
  const ta = bodyEl<HTMLTextAreaElement>(`[data-testid="${testid}"] textarea`)
  await new DOMWrapper(ta).setValue(value)
  await flushPromises()
}

async function confirmModal(): Promise<void> {
  await new DOMWrapper(bodyEl<HTMLButtonElement>('[data-testid="vw-confirm-btn"]'))
    .trigger('click')
  await flushPromises()
}

const confirmBtn = () =>
  bodyEl<HTMLButtonElement>('[data-testid="vw-confirm-btn"]')

beforeEach(async () => {
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('VerifyWorkbench 列表与行操作（REQ-V-006 AC-2）', () => {
  it('待核查行：开始核查/证实/查否/无法核实，不含重开', async () => {
    mountWith(pageOf([P1]))
    await flushPromises()
    const labels = wrapper!.findAll('.vw-item')[0]
      .findAll('button')
      .map((b) => b.text().trim())
    expect(labels).toEqual(['开始核查', '证实', '查否', '无法核实'])
    expect(labels).not.toContain('重开')
  })

  it('已证实行：仅重开', async () => {
    mountWith(pageOf([P2]))
    await flushPromises()
    const labels = wrapper!.findAll('.vw-item')[0]
      .findAll('button')
      .map((b) => b.text().trim())
    expect(labels).toEqual(['重开'])
  })

  it('建议行：采纳/忽略；建议项展示路由函数与证伪条件', async () => {
    mountWith(pageOf([S1]))
    await flushPromises()
    const labels = wrapper!.findAll('.vw-item')[0]
      .findAll('button')
      .map((b) => b.text().trim())
    expect(labels).toEqual(['采纳', '忽略'])
    expect(wrapper!.text()).toContain('time_window_collision')
    expect(wrapper!.text()).toContain('存在合法合同对价则证伪')
  })
})

describe('VerifyWorkbench 确认门禁（REQ-V-006 AC-3/6）', () => {
  it('已证实缺结论：确认按钮禁用；补齐后提交 emit 正确 payload', async () => {
    mountWith(pageOf([P1]))
    await flushPromises()
    await openModal(0, '证实')

    expect(confirmBtn().disabled).toBe(true)
    await setModalTextarea('vw-conclusion', '证据确凿，时间耦合成立')
    expect(confirmBtn().disabled).toBe(false)

    await confirmModal()
    const emitted = wrapper!.emitted('transition')
    expect(emitted).toHaveLength(1)
    expect(emitted![0][0]).toEqual({
      item_id: 'vi_p1',
      next_status: '已证实',
      conclusion: '证据确凿，时间耦合成立',
    })
  })

  it('采纳建议项：改写文本随 payload 提交；未改写不携带 text', async () => {
    mountWith(pageOf([S1]))
    await flushPromises()

    // 改写采纳
    await openModal(0, '采纳')
    const rewrite = bodyEl<HTMLTextAreaElement>(
      '[data-testid="vw-rewrite"] textarea')
    expect(rewrite.value).toBe('调取{subject}账户流水') // 预填建议原文
    expect(document.body.querySelector('[data-testid="vw-conclusion"]')).toBeNull()
    await setModalTextarea('vw-rewrite', '调取张某窗口期完整流水')
    await confirmModal()
    expect(wrapper!.emitted('transition')![0][0]).toEqual({
      item_id: 'vi_s1',
      next_status: '待核查',
      text: '调取张某窗口期完整流水',
    })

    // 原文采纳（不改）→ payload 不含 text 键
    await openModal(0, '采纳')
    await confirmModal()
    const payload = wrapper!.emitted('transition')![1][0] as Record<string, unknown>
    expect(payload).toEqual({ item_id: 'vi_s1', next_status: '待核查' })
    expect('text' in payload).toBe(false)
  })

  it('弹窗署名：将以 operator（role）名义写入审计链', async () => {
    mountWith(pageOf([P1]))
    await flushPromises()
    await openModal(0, '开始核查')
    const box = bodyEl<HTMLElement>('[data-testid="vw-confirm"]')
    expect(box.textContent).toContain('王检察官（human）')
    expect(box.textContent).toContain('审计链')
  })
})

describe('VerifyWorkbench 进度/空态/降级（AC-4/5）', () => {
  it('进度条按 progress 渲染 x/y 与状态 chips', async () => {
    mountWith(pageOf([P1, P2, S1]))
    await flushPromises()
    expect(wrapper!.text()).toMatch(/已结\s*1\s*\/\s*3/)
    // chips：待核查 1 / 已证实 1 / 建议 1
    expect(wrapper!.findAll('.vw-chip').map((c) => c.text().replace(/\s+/g, '')))
      .toEqual(['待核查1', '已证实1', '建议1'])
    expect(wrapper!.text()).toContain('1 项待结')
    // 结论摘要/署名/时间随已结项展示
    expect(wrapper!.text()).toContain('流水时间耦合，予以证实')
    expect(wrapper!.text()).toContain('王检察官')
  })

  it('total=0：显示暂无核查项 + 人工添加入口', async () => {
    mountWith(pageOf([]))
    await flushPromises()
    expect(wrapper!.text()).toContain('本线索暂无核查项')
    expect(wrapper!.findAll('textarea')).toHaveLength(1)
    expect(wrapper!.text()).toContain('添加核查项')
  })

  it('degraded=true：全部写按钮禁用 + 降级通栏文案', async () => {
    mountWith(pageOf([P1]), { degraded: true })
    await flushPromises()
    expect(wrapper!.text()).toContain('系统降级运行中')
    // 行内动作全禁用
    for (const b of wrapper!.findAll('.vw-item button')) {
      expect((b.element as HTMLButtonElement).disabled).toBe(true)
    }
    // 添加按钮 + 输入框禁用
    const addBtn = wrapper!.findAll('button')
      .find((b) => b.text().includes('添加核查项'))!
    expect((addBtn.element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).disabled).toBe(true)
  })
})

describe('VerifyWorkbench 人工添加与刷新（AC-6）', () => {
  it('输入文本 → 添加 emit {text}，输入框清空', async () => {
    mountWith(pageOf([]))
    await flushPromises()
    await wrapper!.find('textarea').setValue('调取银行开户资料')
    const addBtn = wrapper!.findAll('button')
      .find((b) => b.text().includes('添加核查项'))!
    await addBtn.trigger('click')
    expect(wrapper!.emitted('add')![0][0]).toEqual({ text: '调取银行开户资料' })
    await flushPromises()
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('')
  })

  it('空文本不可提交', async () => {
    mountWith(pageOf([]))
    await flushPromises()
    const addBtn = wrapper!.findAll('button')
      .find((b) => b.text().includes('添加核查项'))!
    expect((addBtn.element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper!.emitted('add')).toBeUndefined()
  })

  it('父层写后调 refresh()：重新 GET 拉新清单', async () => {
    const { transport } = mountWith(pageOf([P1]))
    await flushPromises()
    const getsBefore = transport.calls.filter(
      (c) => c.method === 'GET' && c.path.includes('/verify-items')).length
    expect(getsBefore).toBe(1)
    await (wrapper!.vm as unknown as { refresh: () => Promise<void> }).refresh()
    const getsAfter = transport.calls.filter(
      (c) => c.method === 'GET' && c.path.includes('/verify-items')).length
    expect(getsAfter).toBe(2)
  })

  it('工作区未初始化（available:false）：渲染提示且不显示空清单文案', async () => {
    mountWith(pageOf([], {
      available: false,
      progress: {
        total: 0, concluded: 0, pending: 0, suggested: 0, ignored: 0, by_status: {},
      },
    }))
    await flushPromises()
    expect(wrapper!.text()).toContain('核查工作区尚未初始化')
    expect(wrapper!.findAll('.vw-item')).toHaveLength(0)
  })
})

describe('REQ-V-007 三栏联动：loaded 回传与 jumpTo 定位', () => {
  it('清单加载后 emit loaded：回传统一化快照供父层构建文本→状态映射', async () => {
    const page = pageOf([P1, P2, S1])
    mountWith(page)
    await flushPromises()
    const loaded = wrapper!.emitted('loaded')
    expect(loaded).toBeDefined()
    const snap = loaded![loaded!.length - 1][0] as VerifyItemsPage
    expect(snap.available).toBe(true)
    expect(snap.items.map((i) => i.item_id)).toEqual(['vi_p1', 'vi_p2', 'vi_s1'])
    expect(snap.progress.total).toBe(3)
    expect(snap.progress.pending).toBe(1)
  })

  it('refresh 后再次 emit loaded（父层映射随裁决刷新）', async () => {
    mountWith(pageOf([P1]))
    await flushPromises()
    await (wrapper!.vm as unknown as { refresh: () => Promise<void> }).refresh()
    expect(wrapper!.emitted('loaded')).toHaveLength(2)
  })

  it('jumpTo 命中同文本核查项：匹配项加高亮 class、返回 true（重复点击重置计时不报错）', async () => {
    mountWith(pageOf([P1, P2]))
    await flushPromises()
    const vm = wrapper!.vm as unknown as { jumpTo: (t: string) => Promise<boolean> }

    const hit = await vm.jumpTo('待核实事项甲')
    expect(hit).toBe(true)
    await flushPromises()
    const flash = wrapper!.findAll('.vw-item--flash')
    expect(flash).toHaveLength(1)
    expect(flash[0].attributes('data-text')).toBe('待核实事项甲')
    // 未命中的行不高亮
    expect(wrapper!.findAll('.vw-item')[1].classes()).not.toContain('vw-item--flash')

    const hitAgain = await vm.jumpTo('待核实事项甲')
    expect(hitAgain).toBe(true)
    expect(wrapper!.findAll('.vw-item--flash')).toHaveLength(1)
  })

  it('jumpTo 未命中：文本预填进人工添加框并聚焦，返回 false（空文本安全返回）', async () => {
    mountWith(pageOf([P1]))
    await flushPromises()
    const vm = wrapper!.vm as unknown as { jumpTo: (t: string) => Promise<boolean> }

    const hit = await vm.jumpTo('一条尚未供给的新疑点')
    expect(hit).toBe(false)
    await flushPromises()
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value)
      .toBe('一条尚未供给的新疑点')
    expect(wrapper!.findAll('.vw-item--flash')).toHaveLength(0)
    expect(wrapper!.emitted('add')).toBeUndefined() // 只预填，不替用户提交

    expect(await vm.jumpTo('   ')).toBe(false)
  })
})
