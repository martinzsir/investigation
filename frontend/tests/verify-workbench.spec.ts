import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { NMessageProvider, NSelect } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope, type FakeRoute } from './helpers'
import VerifyWorkbench from '../src/components/research/VerifyWorkbench.vue'
import EvidencePanel from '../src/components/research/EvidencePanel.vue'
import {
  assembleVerifyText,
  entityCandidates,
  replayResultExpandable,
  replayResultJson,
  replayResultSummary,
  replaySourceLabel,
} from '../src/domain/verify'
import type {
  EvidenceMaterial,
  VerifyItem,
  VerifyItemsPage,
  VerifyReplay,
} from '../src/domain/verify'

// REQ-V-006 AC-2~6（happy-dom 真实挂载；写动作只 emit，API 由父层编排）。
// REQ-V-017：复跑按钮 emit replay、结果卡片渲染（replay_json 投影）。

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
    replay: null,
    ...over,
  }
}

/** REQ-V-017 复跑结果种子（worker _replay 回填 replay_json 的投影契约） */
function replaySeed(over: Partial<VerifyReplay>): VerifyReplay {
  return {
    function: 'integer_transfer_aggregates',
    fallback_used: false,
    mapping_source: 'playbook',
    params_used: {},
    output_type: 'rows',
    result: [],
    degraded: false,
    degraded_reason: null,
    version: 'v1',
    source_row_ids: ['vi_x'],
    operator: '王检察官',
    replayed_at: '2026-09-12 11:00:00',
    ...over,
  }
}

/** REQ-V-010 书证种子（clue_evidence 清单/行内投影契约） */
function evidenceSeed(over: Partial<EvidenceMaterial> = {}): EvidenceMaterial {
  return {
    material_id: 'm_x',
    item_id: '',
    material_type: '其他',
    filename: 'm_x_a.pdf',
    orig_name: 'a.pdf',
    sha256: 'sha-x',
    size: 1024,
    note: '',
    uploaded_by: '王检察官',
    uploaded_at: '2026-09-13 10:00:00',
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
// NMessageProvider 根（VerifyWorkbench setup 调 useMessage，生产侧由 App 包裹）
let rootWrapper: VueWrapper | null = null

function mountWith(
  page: VerifyItemsPage,
  props: Record<string, unknown> = {},
  extraRoutes: FakeRoute[] = [],
  evidenceItems: EvidenceMaterial[] = [],
) {
  const transport = new FakeTransport([
    {
      match: (r) => r.method === 'GET'
        && r.path.includes('/clues/clue-1/verify-items'),
      respond: () => okEnvelope(page),
    },
    // REQ-V-010：EvidencePanel 自加载书证清单（默认空，用例可注入）
    {
      match: (r) => r.method === 'GET'
        && r.path === '/cases/c1/clues/clue-1/evidence',
      respond: () => okEnvelope({ items: evidenceItems }),
    },
    // REQ-V-013：refresh() 级联自加载调取台账（默认空清单；不 mock 会落错误条）
    {
      match: (r) => r.method === 'GET' && r.path.includes('/verify-requests'),
      respond: () => okEnvelope({ items: [], available: true }),
    },
    ...extraRoutes,
  ])
  setTransport(transport)
  // useMessage() 要求外层 n-message-provider（同 evidence-panel.spec 惯例）
  rootWrapper = mount(NMessageProvider, {
    slots: {
      default: () => h(VerifyWorkbench, {
        caseId: 'c1',
        clueId: 'clue-1',
        operator: '王检察官',
        role: 'human',
        degraded: false,
        submitting: false,
        ...props,
      }),
    },
  })
  wrapper = rootWrapper.findComponent(VerifyWorkbench)
  return { w: wrapper!, root: rootWrapper, transport }
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
  // 构造器经 useCaseOntologyConfig → pinia store（拉取失败回落 DEFAULT，不阻塞）
  setActivePinia(createPinia())
})

afterEach(() => {
  rootWrapper?.unmount()
  rootWrapper = null
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

// ---------- REQ-V-018：结构化构造器 + 采纳后路由按钮 ----------

describe('REQ-V-018 构造器纯函数：实体抽取与文本拼装', () => {
  it('entityCandidates：主体列命中/去重/排序；非主体列与空白值不收', () => {
    expect(entityCandidates([
      { 人: '张某', 户名: ' 李某 ', 金额: 100, from_raw: '张某' },
      { 姓名: '', 无关: 'x', person: '王某' },
    ])).toEqual(['张某', '李某', '王某']) // 码元序：张 U+5F20 < 李 U+674E < 王 U+738B
    expect(entityCandidates([{ 金额: 1 }])).toEqual([])
    expect(entityCandidates(undefined)).toEqual([])
  })

  it('assembleVerifyText：实体/维度/核查点/渠道全矩阵（无前缀不输出冒号）', () => {
    // 全量（function 渠道）
    expect(assembleVerifyText({
      entity: '张某', dimension: '资金', point: '整数资金是否对公往来',
      channel: 'function',
    })).toBe('对「张某」开展资金维度核查：整数资金是否对公往来（渠道：库内可复跑）')
    // 仅核查点：不输出前导冒号
    expect(assembleVerifyText({ point: '整数资金是否对公往来', channel: 'function' }))
      .toBe('整数资金是否对公往来（渠道：库内可复跑）')
    // 外部调取：对象+材料 / 仅对象 / 仅材料 / 均缺
    expect(assembleVerifyText({
      entity: '张某', point: '底档', channel: 'external',
      extTarget: '住建局招标办', extMaterial: '招投标底档',
    })).toBe('对「张某」：底档（渠道：外部调取·向住建局招标办调取招投标底档）')
    expect(assembleVerifyText({ channel: 'external', extTarget: '住建局招标办' }))
      .toBe('（渠道：外部调取·向住建局招标办）')
    expect(assembleVerifyText({ channel: 'external', extMaterial: '底档' }))
      .toBe('（渠道：外部调取·调取底档）')
    expect(assembleVerifyText({ channel: 'external' })).toBe('（渠道：外部调取）')
    // 全空兜底
    expect(assembleVerifyText({ channel: 'function' })).toBe('（渠道：库内可复跑）')
  })
})

describe('REQ-V-018 结构化构造器渲染与链路（AC4/5）', () => {
  it('sourceRows 缺省：构造器不渲染，回落纯手填（textarea 仍在）', async () => {
    mountWith(pageOf([]))
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-ctor"]').exists()).toBe(false)
    expect(wrapper!.findAll('textarea')).toHaveLength(1)
  })

  it('sourceRows 有候选：构造器渲染；拼装 → textarea 预填 → 提交只发 text', async () => {
    mountWith(pageOf([]), {
      sourceRows: [{ 人: '张某' }, { 金额: 1 }],
    })
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-ctor"]').exists()).toBe(true)

    // 核查点（NInput → 原生 input）+ 默认渠道 function → 拼装
    await wrapper!.find('[data-testid="vw-ctor-point"] input')
      .setValue('窗口期整数资金是否为对公工程往来')
    await wrapper!.find('[data-testid="vw-ctor-assemble"]').trigger('click')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value)
      .toBe('窗口期整数资金是否为对公工程往来（渠道：库内可复跑）')

    // 改一改再提交：emit 只携带最终 text
    await wrapper!.find('textarea')
      .setValue('对「张某」核查窗口期整数资金是否为对公工程往来')
    const addBtn = wrapper!.findAll('button')
      .find((b) => b.text().includes('添加核查项'))!
    await addBtn.trigger('click')
    expect(wrapper!.emitted('add')![0][0]).toEqual({
      text: '对「张某」核查窗口期整数资金是否为对公工程往来',
    })
  })

  it('渠道切外部调取：目标/材料输入框出现，拼装文本带外部渠道段', async () => {
    mountWith(pageOf([]), { sourceRows: [{ 人: '张某' }] })
    await flushPromises()
    // 默认 function：外部输入框不渲染
    expect(wrapper!.find('[data-testid="vw-ctor-target"]').exists()).toBe(false)

    await wrapper!.find('input[type="radio"][value="external"]').setValue()
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-ctor-target"]').exists()).toBe(true)
    await wrapper!.find('[data-testid="vw-ctor-target"] input').setValue('住建局招标办')
    await wrapper!.find('[data-testid="vw-ctor-material"] input').setValue('招投标底档')
    await wrapper!.find('[data-testid="vw-ctor-point"] input').setValue('招投标底档联签单')
    await wrapper!.find('[data-testid="vw-ctor-assemble"]').trigger('click')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value)
      .toBe('招投标底档联签单（渠道：外部调取·向住建局招标办调取招投标底档）')
  })

  it('degraded=true：构造器全部控件禁用', async () => {
    mountWith(pageOf([]), {
      degraded: true,
      sourceRows: [{ 人: '张某' }],
    })
    await flushPromises()
    for (const sel of ['vw-ctor-point', 'vw-ctor-target', 'vw-ctor-material']) {
      const el = wrapper!.find(`[data-testid="${sel}"] input`)
      if (el.exists()) expect((el.element as HTMLInputElement).disabled).toBe(true)
    }
    expect((wrapper!.find('[data-testid="vw-ctor-assemble"]').element as HTMLButtonElement)
      .disabled).toBe(true)
  })
})

describe('REQ-V-018 采纳后路由按钮（REQ-V-017 复跑已接线）', () => {
  const ADOPTED_FN = item({
    item_id: 'vi_af', kind: 'suggested', origin: 'suggested',
    status: '待核查', text: '复跑时间窗核查',
    channel: 'function', ref_function: 'time_window_collision',
  })
  const ADOPTED_EXT = item({
    item_id: 'vi_ae', kind: 'suggested', origin: 'suggested',
    status: '核查中', text: '调取招投标底档',
    channel: 'external',
    external: { target: '住建局招标办', material: '招投标底档' },
  })

  it('建议态无路由按钮；采纳后 function 项显示运行按钮（启用，点击 emit replay）', async () => {
    mountWith(pageOf([S1, ADOPTED_FN]))
    await flushPromises()
    const rows = wrapper!.findAll('.vw-item')
    // S1（建议态）：仅 采纳/忽略，无路由按钮
    expect(rows[0].findAll('.vw-route-btn')).toHaveLength(0)
    // ADOPTED_FN：function 渠道复跑按钮已接线（REQ-V-017）
    const btn = rows[1].find('[data-testid="vw-route-function"]')
    expect(btn.exists()).toBe(true)
    expect((btn.element as HTMLButtonElement).disabled).toBe(false)
    expect(btn.attributes('title')).toContain('time_window_collision')
    expect(rows[1].find('[data-testid="vw-route-external"]').exists()).toBe(false)
    await btn.trigger('click')
    expect(wrapper!.emitted('replay')![0][0]).toEqual({ item_id: 'vi_af' })
  })

  it('degraded=true：复跑按钮禁用且不 emit', async () => {
    mountWith(pageOf([ADOPTED_FN]), { degraded: true })
    await flushPromises()
    const btn = wrapper!.find('[data-testid="vw-route-function"]')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)
    await btn.trigger('click')
    expect(wrapper!.emitted('replay')).toBeUndefined()
  })

  it('external 采纳项：转调取台账按钮预填调取登记表单（REQ-V-013 接线）', async () => {
    mountWith(pageOf([ADOPTED_EXT]))
    await flushPromises()
    const btn = wrapper!.find('[data-testid="vw-route-external"]')
    expect(btn.exists()).toBe(true)
    expect((btn.element as HTMLButtonElement).disabled).toBe(false)
    expect(btn.attributes('title')).toContain('住建局招标办')
    expect(wrapper!.find('[data-testid="vw-route-function"]').exists()).toBe(false)
    await btn.trigger('click')
    await flushPromises()
    // 预填进台账登记表单：目标/材料随行内徽标数据
    expect(wrapper!.find('[data-testid="vw-req-form"]').exists()).toBe(true)
    expect((wrapper!.find('[data-testid="vw-req-target"] input').element as HTMLInputElement).value)
      .toBe('住建局招标办')
    expect((wrapper!.find('[data-testid="vw-req-material"] input').element as HTMLInputElement).value)
      .toBe('招投标底档')
  })

  it('已结态（已证实）不再显示路由按钮', async () => {
    mountWith(pageOf([item({
      item_id: 'vi_done', status: '已证实', conclusion: '证据确凿',
      channel: 'function', ref_function: 'time_window_collision',
    })]))
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-route-function"]').exists()).toBe(false)
  })
})

// ---------- REQ-V-017：一键复跑回填（结果卡片 + 展示纯函数） ----------

describe('REQ-V-017 复跑展示纯函数', () => {
  it('replayResultSummary：行数组/标量/对象/空+降级 全矩阵', () => {
    expect(replayResultSummary(replaySeed({ result: [{ a: 1 }, { a: 2 }] })))
      .toBe('返回 2 行结果')
    expect(replayResultSummary(replaySeed({ result: [] })))
      .toBe('返回 0 行结果')
    expect(replayResultSummary(replaySeed({ result: 42 })))
      .toBe('42')
    expect(replayResultSummary(replaySeed({ result: { total: 3 } })))
      .toBe('计算完成（对象结果）')
    expect(replayResultSummary(replaySeed({ result: null })))
      .toBe('无结果')
    expect(replayResultSummary(replaySeed({
      result: null, degraded: true, degraded_reason: '空库缺 obj_transaction',
    }))).toBe('无结果（结构降级，见降级原因）')
  })

  it('replayResultExpandable：非空对象/非空行数组可展开；空值/标量/空容器不可', () => {
    expect(replayResultExpandable(replaySeed({ result: { hit: true } }))).toBe(true)
    expect(replayResultExpandable(replaySeed({ result: [{ a: 1 }] }))).toBe(true)
    expect(replayResultExpandable(replaySeed({ result: [] }))).toBe(false)
    expect(replayResultExpandable(replaySeed({ result: {} }))).toBe(false)
    expect(replayResultExpandable(replaySeed({ result: 42 }))).toBe(false)
    expect(replayResultExpandable(replaySeed({ result: '' }))).toBe(false)
    expect(replayResultExpandable(replaySeed({ result: null }))).toBe(false)
  })

  it('replayResultJson：2 空格缩进序列化（与卡片 <pre> 展示契约一致）', () => {
    expect(replayResultJson(replaySeed({ result: { hit: true, n: 2 } })))
      .toBe('{\n  "hit": true,\n  "n": 2\n}')
    expect(replayResultJson(replaySeed({ result: [{ a: 1 }] })))
      .toBe('[\n  {\n    "a": 1\n  }\n]')
  })

  it('replaySourceLabel：playbook=手册映射 / fallback=关键词兜底 / 未知透传', () => {
    expect(replaySourceLabel('playbook')).toBe('手册映射')
    expect(replaySourceLabel('fallback')).toBe('关键词兜底')
    expect(replaySourceLabel('custom')).toBe('custom')
  })
})

describe('REQ-V-017 复跑结果卡片渲染', () => {
  const REPLAYED = item({
    item_id: 'vi_rp', kind: 'suggested', origin: 'suggested',
    status: '待核查', text: '复跑整数转账聚合',
    channel: 'function', ref_function: 'integer_transfer_aggregates',
    replay: replaySeed({
      fallback_used: true,
      degraded: true,
      degraded_reason: '空库缺 obj_transaction',
      result: [],
      version: 'v7',
    }),
  })

  it('未复跑（replay=null）不渲染结果卡片', async () => {
    mountWith(pageOf([item({
      item_id: 'vi_af', kind: 'suggested', origin: 'suggested',
      status: '待核查', text: '复跑时间窗核查',
      channel: 'function', ref_function: 'time_window_collision',
    })]))
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-replay-card"]').exists()).toBe(false)
  })

  it('已复跑：卡片渲染 function/徽标/摘要/溯源（不改状态/结论展示）', async () => {
    mountWith(pageOf([REPLAYED]))
    await flushPromises()
    const card = wrapper!.find('[data-testid="vw-replay-card"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('integer_transfer_aggregates')
    expect(card.text()).toContain('备选接管') // fallback_used
    expect(card.text()).toContain('结构降级') // degraded（原因在 title）
    expect(card.find('.vw-replay-badge[title*="obj_transaction"]').exists()).toBe(true)
    expect(card.text()).toContain('返回 0 行结果')
    expect(card.text()).toContain('手册映射') // mapping_source=playbook
    expect(card.text()).toContain('2026-09-12 11:00:00')
    expect(card.text()).toContain('王检察官')
    expect(card.text()).toContain('v7')
  })

  it('干净复跑（无备选/无降级）不渲染警示徽标', async () => {
    mountWith(pageOf([item({
      item_id: 'vi_rp2', kind: 'suggested', origin: 'suggested',
      status: '核查中', text: '复跑时间窗核查',
      channel: 'function', ref_function: 'time_window_collision',
      replay: replaySeed({ result: [{ from_raw: '张某' }], fallback_used: false }),
    })]))
    await flushPromises()
    const card = wrapper!.find('[data-testid="vw-replay-card"]')
    expect(card.text()).toContain('返回 1 行结果')
    expect(card.text()).not.toContain('备选接管')
    expect(card.text()).not.toContain('结构降级')
  })

  // 方案A：对象/行数组结果支持折叠查看原始 JSON（默认收起，纯前端开关）
  it('对象结果：点击「查看原始结果」展开完整 JSON，再点收起', async () => {
    mountWith(pageOf([item({
      item_id: 'vi_rp3', kind: 'suggested', origin: 'suggested',
      status: '待核查', text: '补查通话频次突增',
      channel: 'function', ref_function: 'call_frequency_spike',
      replay: replaySeed({
        function: 'call_frequency_spike', output_type: 'object',
        result: {
          hit: true,
          basis: '138****0001→139****0002 通话 12 次，为常态中位数 4 的 3.0 倍',
          subject: '138****0001',
          pairs: [{ 主体: '138****0001', 对端: '139****0002', 次数: 12 }],
          diagnostics: { is_degraded: false, median_value: 4 },
        },
      }),
    })]))
    await flushPromises()
    const card = wrapper!.find('[data-testid="vw-replay-card"]')
    const toggle = card.find('[data-testid="vw-replay-toggle"]')
    const detail = card.find('[data-testid="vw-replay-detail"]')
    expect(card.text()).toContain('计算完成（对象结果）')
    expect(toggle.exists()).toBe(true)
    expect(toggle.text()).toBe('查看原始结果')
    expect(detail.exists()).toBe(true)
    expect(detail.isVisible()).toBe(false) // 默认收起

    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.text()).toBe('收起原始结果')
    expect(detail.isVisible()).toBe(true)
    const json = detail.text()
    expect(json).toContain('"hit": true')
    expect(json).toContain('138****0001→139****0002 通话 12 次')
    expect(json).toContain('"median_value": 4')

    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.text()).toBe('查看原始结果')
    expect(detail.isVisible()).toBe(false)
  })

  it('行数组结果同样可展开；空数组/标量/空结果不渲染折叠入口', async () => {
    mountWith(pageOf([
      item({
        item_id: 'vi_rows', kind: 'suggested', origin: 'suggested',
        status: '待核查', text: '行结果',
        channel: 'function', ref_function: 'f_rows',
        replay: replaySeed({ result: [{ n: 1 }, { n: 2 }] }),
      }),
      item({
        item_id: 'vi_empty', kind: 'suggested', origin: 'suggested',
        status: '待核查', text: '空数组',
        channel: 'function', ref_function: 'f_empty',
        replay: replaySeed({ result: [] }),
      }),
      item({
        item_id: 'vi_scalar', kind: 'suggested', origin: 'suggested',
        status: '待核查', text: '标量',
        channel: 'function', ref_function: 'f_scalar',
        replay: replaySeed({ result: 42 }),
      }),
      item({
        item_id: 'vi_none', kind: 'suggested', origin: 'suggested',
        status: '待核查', text: '无结果',
        channel: 'function', ref_function: 'f_none',
        replay: replaySeed({ result: null }),
      }),
    ]))
    await flushPromises()
    const cards = wrapper!.findAll('[data-testid="vw-replay-card"]')
    expect(cards).toHaveLength(4)
    expect(cards[0].find('[data-testid="vw-replay-toggle"]').exists()).toBe(true)
    expect(cards[1].find('[data-testid="vw-replay-toggle"]').exists()).toBe(false)
    expect(cards[2].find('[data-testid="vw-replay-toggle"]').exists()).toBe(false)
    expect(cards[3].find('[data-testid="vw-replay-toggle"]').exists()).toBe(false)
  })
})

// ---------- REQ-V-010/011：书证展示/下载/解除 + EvidencePanel 挂接透传 ----------

describe('REQ-V-010 核查项卡片内已挂书证投影', () => {
  it('item.evidence：行内渲染原名，下载/解除按钮带 material_id', async () => {
    const page = pageOf([item({
      item_id: 'vi_ev',
      text: '核查资金往来',
      evidence: [
        evidenceSeed({
          material_id: 'm_card',
          item_id: 'vi_ev',
          orig_name: '付款凭证.png',
          material_type: '付款凭证',
          size: 4096,
        }),
      ],
    })])
    const { w } = mountWith(page)
    await flushPromises()

    const card = w.find('.vw-item[data-text="核查资金往来"]')
    const evBox = card.find('[data-testid="vw-item-evidence"]')
    expect(evBox.exists()).toBe(true)
    const chip = evBox.find('.vw-ev-chip')
    expect(chip.attributes('data-material-id')).toBe('m_card')
    expect(chip.text()).toContain('付款凭证.png')
    expect(chip.find('[data-testid="vw-ev-download"]').exists()).toBe(true)
    expect(chip.find('[data-testid="vw-ev-unlink"]').exists()).toBe(true)
  })

  it('解除按钮 → emit evidenceUnlink（202 编排由视图层负责）', async () => {
    const page = pageOf([item({
      item_id: 'vi_ev',
      evidence: [evidenceSeed({ material_id: 'm_go', item_id: 'vi_ev' })],
    })])
    const { w } = mountWith(page)
    await flushPromises()
    await w.find('[data-testid="vw-ev-unlink"]').trigger('click')
    expect(w.emitted('evidenceUnlink')![0][0]).toEqual({ material_id: 'm_go' })
  })

  it('degraded：行内解除按钮禁用；无 evidence 字段的旧数据不渲染书证区', async () => {
    const page = pageOf([
      item({ item_id: 'vi_with', text: '有书证', evidence: [evidenceSeed({ material_id: 'm1', item_id: 'vi_with' })] }),
      item({ item_id: 'vi_plain', text: '无书证' }),
    ])
    const { w } = mountWith(page, { degraded: true })
    await flushPromises()
    expect(w.find('[data-testid="vw-ev-unlink"]').attributes('disabled')).toBeDefined()
    expect(w.find('.vw-item[data-text="无书证"]').find('.vw-ev').exists()).toBe(false)
  })
})

describe('REQ-V-010/011 EvidencePanel 集成：清单挂载与事件透传', () => {
  it('面板随工作台挂载，空清单渲染空态', async () => {
    const { w } = mountWith(pageOf([item()]))
    await flushPromises()
    const panel = w.findComponent(EvidencePanel)
    expect(panel.exists()).toBe(true)
    expect(panel.find('[data-testid="evidence-panel"]').exists()).toBe(true)
    expect(panel.text()).toContain('暂无书证材料')
  })

  it('面板内挂接动作透传为 workbench evidenceLink 载荷', async () => {
    const page = pageOf([item({ item_id: 'vi_af', text: '核查事项 AF' })])
    const { w } = mountWith(
      page,
      {},
      [],
      [evidenceSeed({ material_id: 'm_link', item_id: '' })],
    )
    await flushPromises()

    const panel = w.findComponent(EvidencePanel)
    const row = panel.find('[data-material-id="m_link"]')
    await row.findComponent(NSelect).vm.$emit('update:value', 'vi_af')
    await flushPromises()
    await row.find('[data-testid="ep-link"]').trigger('click')
    await flushPromises()

    expect(w.emitted('evidenceLink')![0][0]).toEqual({
      item_id: 'vi_af', material_id: 'm_link',
    })
  })

  it('面板内解除动作透传为 workbench evidenceUnlink 载荷', async () => {
    const page = pageOf([item({ item_id: 'vi_af' })])
    const { w } = mountWith(
      page,
      {},
      [],
      [evidenceSeed({ material_id: 'm_un', item_id: 'vi_af' })],
    )
    await flushPromises()
    const panel = w.findComponent(EvidencePanel)
    await panel.find('[data-material-id="m_un"]').find('[data-testid="ep-unlink"]').trigger('click')
    expect(w.emitted('evidenceUnlink')![0][0]).toEqual({ material_id: 'm_un' })
  })

  it('refresh() 级联刷新面板（暴露方法调用后二次拉清单）', async () => {
    const { w, transport } = mountWith(pageOf([item({})]))
    await flushPromises()
    const countListGets = () => transport.calls.filter(
      (c) => c.method === 'GET' && c.path === '/cases/c1/clues/clue-1/evidence',
    ).length
    const before = countListGets()
    expect(before).toBeGreaterThanOrEqual(1)
    await (w.vm as unknown as { refresh: () => Promise<void> }).refresh()
    expect(countListGets()).toBeGreaterThan(before)
  })
})

// ---------- REQ-V-019：AI 建议核查方向（草案交互 + AI 成项徽标） ----------

describe('REQ-V-019 AI 建议核查方向（草案交互）', () => {
  const AI_ITEM = item({
    item_id: 'vi_ai', kind: 'manual', origin: 'ai_draft',
    status: '待核查', text: '核查张某与李某账户间的整数资金循环',
    channel: 'function', ref_function: 'time_window_collision',
  })

  function draftRoute(data: Record<string, unknown>): FakeRoute {
    return {
      match: (r) => r.method === 'POST'
        && r.path.includes('/clues/clue-1/verify-items/draft'),
      respond: () => okEnvelope(data),
    }
  }

  async function clickDraft(): Promise<void> {
    await wrapper!.find('[data-testid="vw-ai-draft"]').trigger('click')
    await flushPromises()
  }

  it('ok：绿条展示草案数与待审批提示；草案不刷新核查项列表（未成项）', async () => {
    const { transport } = mountWith(pageOf([P1]), {}, [draftRoute({
      ok: true, mode: 'local', model: 'qwen3:8b',
      proposals: [
        { proposal_id: 'pp-a', text: '草案一', dimension: '资金',
          channel: 'function', ref_function: 'time_window_collision',
          author: 'model:qwen3:8b' },
        { proposal_id: 'pp-b', text: '草案二', dimension: '通讯',
          channel: 'external', ref_function: '', author: 'model:qwen3:8b' },
      ],
      dropped: [], duplicates: 0,
    })])
    await flushPromises()
    expect(transport.calls.filter((c) => c.method === 'GET')).toHaveLength(1)

    await clickDraft()
    const note = wrapper!.find('[data-testid="vw-draft-note"]')
    expect(note.classes()).toContain('vw-draft-note--ok')
    expect(note.text()).toContain('已生成 2 条 AI 草案（mode=local）')
    expect(note.text()).toContain('待审批')
    // shadow：草案只落提案队列，不 refresh 清单（GET 仍 1 次）
    expect(transport.calls.filter((c) => c.method === 'GET')).toHaveLength(1)
  })

  it('零草案 + 去重/过滤：黄条附过滤说明', async () => {
    mountWith(pageOf([P1]), {}, [draftRoute({
      ok: true, mode: 'local', proposals: [], duplicates: 1,
      dropped: [{ index: 0, reason: 'channel 非法' }],
    })])
    await flushPromises()
    await clickDraft()
    const note = wrapper!.find('[data-testid="vw-draft-note"]')
    expect(note.classes()).toContain('vw-draft-note--warn')
    expect(note.text()).toContain('未产出新的 AI 草案')
    expect(note.text()).toContain('1 条与已有草案重复')
    expect(note.text()).toContain('1 条未通过确定性校验已过滤')
  })

  it('degraded（off 档）：黄条带档位提示与原因', async () => {
    mountWith(pageOf([P1]), {}, [draftRoute({
      ok: false, degraded: true, mode: 'off', reason: 'LLM 能力未启用',
    })])
    await flushPromises()
    await clickDraft()
    const note = wrapper!.find('[data-testid="vw-draft-note"]')
    expect(note.classes()).toContain('vw-draft-note--warn')
    expect(note.text()).toContain('AI 草案不可用')
    expect(note.text()).toContain('内核隔离模式')
    expect(note.text()).toContain('LLM 能力未启用')
  })

  it('blocked：红条带拦截原因', async () => {
    mountWith(pageOf([P1]), {}, [draftRoute({
      ok: false, blocked: true, mode: 'local',
      error: 'endpoint_gate: host 越界',
    })])
    await flushPromises()
    await clickDraft()
    const note = wrapper!.find('[data-testid="vw-draft-note"]')
    expect(note.classes()).toContain('vw-draft-note--error')
    expect(note.text()).toContain('AI 草案被拦截')
    expect(note.text()).toContain('endpoint_gate')
  })

  it('请求失败（网络异常）：红条兜底不吞错', async () => {
    mountWith(pageOf([P1])) // 未注册 draft 路由 → transport 抛 no route
    await flushPromises()
    await clickDraft()
    const note = wrapper!.find('[data-testid="vw-draft-note"]')
    expect(note.classes()).toContain('vw-draft-note--error')
    expect(note.text()).toContain('AI 草案请求失败')
  })

  it('AI 草案成项徽标：origin=ai_draft 渲染「AI 草案」，manual 不渲染', async () => {
    mountWith(pageOf([AI_ITEM, P1]))
    await flushPromises()
    const hasAi = wrapper!.findAll('.vw-item').map(
      (row) => row.find('[data-testid="vw-origin-ai"]').exists())
    expect(hasAi).toEqual([true, false])
    expect(wrapper!.text()).toContain('AI 草案')
  })

  it('degraded=true：AI 按钮禁用', async () => {
    mountWith(pageOf([P1]), { degraded: true })
    await flushPromises()
    expect((wrapper!.find('[data-testid="vw-ai-draft"]').element as HTMLButtonElement)
      .disabled).toBe(true)
  })
})
