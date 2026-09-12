import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import VerifyWorkbench from '../src/components/research/VerifyWorkbench.vue'
import {
  canRequestTransition,
  legalRequestTargets,
  REQUEST_TRANSITIONS,
  type VerifyItem,
  type VerifyItemsPage,
  type VerifyRequestRow,
  type VerifyRequestsPage,
} from '../src/domain/verify'

// REQ-V-013 调取清单台账（VerifyWorkbench 子面板）：
// - 状态机镜像表与 core/verify_machine REQUEST_TRANSITIONS 同构（按钮显隐唯一依据）；
// - 台账行渲染：状态徽标 + 超期红徽（服务端派生列透传）+ 动作按钮按状态现算；
// - 写动作只 emit（requestCreate / requestTransition），API 由父层薄编排；
// - 「转调取台账」入口预填外部渠道路由数据（REQ-V-018 占位按钮接线）。

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

function req(over: Partial<VerifyRequestRow>): VerifyRequestRow {
  return {
    request_id: 'vr_aaaabbbbcccc',
    clue_id: 'clue-1',
    item_id: null,
    target: '海州银行营业部',
    material: '2025 年 1-6 月账户流水',
    legal_instrument: '调取函（2026）12 号',
    handler: '李侦查员',
    due_date: '',
    status: '待发起',
    note: '',
    created_by: '王检察官',
    created_at: '2026-09-12 10:00:00',
    updated_at: '',
    overdue: false,
    ...over,
  }
}

function pageOf(items: VerifyItem[]): VerifyItemsPage {
  const byStatus: Record<string, number> = {}
  for (const it of items) byStatus[it.status] = (byStatus[it.status] ?? 0) + 1
  return {
    items,
    progress: {
      total: items.length, concluded: 0,
      pending: (byStatus['待核查'] ?? 0) + (byStatus['核查中'] ?? 0),
      suggested: byStatus['建议'] ?? 0, ignored: byStatus['已忽略'] ?? 0,
      by_status: byStatus,
    },
    available: true,
  }
}

let wrapper: VueWrapper | null = null

function mountWith(items: VerifyItem[], rows: VerifyRequestRow[]) {
  const transport = new FakeTransport([
    {
      match: (r) => r.method === 'GET'
        && r.path.includes('/clues/clue-1/verify-items'),
      respond: () => okEnvelope(pageOf(items)),
    },
    {
      match: (r) => r.method === 'GET' && r.path.includes('/verify-requests'),
      respond: () => okEnvelope({ items: rows, available: true }),
    },
  ])
  setTransport(transport)
  wrapper = mount(VerifyWorkbench, {
    props: {
      caseId: 'c1', clueId: 'clue-1',
      operator: '王检察官', role: 'human',
      degraded: false, submitting: false,
    },
  })
  return { w: wrapper!, transport }
}

function bodyEl<T extends Element>(selector: string): T {
  const el = document.body.querySelector(selector) as T | null
  if (!el) throw new Error(`body 中未找到：${selector}`)
  return el
}

async function clickRowAction(rowIdx: number, label: string): Promise<void> {
  const btn = wrapper!.findAll('.vw-req-item')[rowIdx]
    .findAll('button')
    .find((b) => b.text().trim() === label)
  if (!btn) throw new Error(`台账行 ${rowIdx} 未渲染动作：${label}`)
  await btn.trigger('click')
  await flushPromises()
}

async function confirmReqModal(): Promise<void> {
  await new DOMWrapper(bodyEl<HTMLButtonElement>('[data-testid="vw-req-confirm-btn"]'))
    .trigger('click')
  await flushPromises()
}

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('REQ-V-013 请求状态机镜像（domain/verify.ts）', () => {
  it('镜像表与后端白名单逐字同构', () => {
    expect(REQUEST_TRANSITIONS).toEqual({
      待发起: ['已发起'],
      已发起: ['材料已回', '关闭'],
      材料已回: ['关闭'],
    })
    expect(canRequestTransition('待发起', '已发起')).toBe(true)
    expect(canRequestTransition('已发起', '关闭')).toBe(true)
    expect(canRequestTransition('材料已回', '关闭')).toBe(true)
    // 非法/未知迁移
    expect(canRequestTransition('待发起', '材料已回')).toBe(false)
    expect(canRequestTransition('关闭', '已发起')).toBe(false)
    expect(canRequestTransition('不存在', '已发起')).toBe(false)
  })

  it('legalRequestTargets：未知状态返回空数组（按钮不渲染）', () => {
    expect(legalRequestTargets('已发起')).toEqual(['材料已回', '关闭'])
    expect(legalRequestTargets('不存在')).toEqual([])
  })
})

describe('REQ-V-013 台账子面板', () => {
  it('台账行渲染：目标/材料/手续/期限/署名 + 动作按钮按状态现算', async () => {
    const rows = [
      req({ request_id: 'vr_r1', status: '待发起' }),
      req({ request_id: 'vr_r2', status: '已发起' }),
      req({ request_id: 'vr_r3', status: '材料已回' }),
      req({ request_id: 'vr_r4', status: '关闭' }),
    ]
    mountWith([item({ item_id: 'vi_p1' })], rows)
    await flushPromises()
    const items = wrapper!.findAll('.vw-req-item')
    expect(items).toHaveLength(4)
    expect(items[0].text()).toContain('海州银行营业部')
    expect(items[0].text()).toContain('调取函（2026）12 号')
    // 动作显隐：待发起→发起；已发起→回执登记+关闭；材料已回→关闭；关闭→无
    const labels = (i: number) => items[i].findAll('button').map((b) => b.text().trim())
    expect(labels(0)).toContain('发起')
    expect(labels(1)).toContain('回执登记')
    expect(labels(1)).toContain('关闭')
    expect(labels(2)).toContain('关闭')
    expect(labels(2)).not.toContain('回执登记')
    expect(labels(3)).toEqual([])
  })

  it('超期红徽：服务端 overdue:true 透传渲染', async () => {
    mountWith([], [req({ status: '已发起', due_date: '2026-01-01', overdue: true })])
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-req-overdue"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="vw-req-overdue"]').text()).toContain('超期')
    // 未超期行不渲染徽标
    wrapper!.unmount()
    mountWith([], [req({ status: '已发起', overdue: false })])
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-req-overdue"]').exists()).toBe(false)
  })

  it('迁移动作走确认弹窗：署名 + emit requestTransition', async () => {
    mountWith([], [req({ request_id: 'vr_r1', status: '待发起' })])
    await flushPromises()
    await clickRowAction(0, '发起')
    const confirmEl = bodyEl('[data-testid="vw-req-confirm"]')
    expect(confirmEl.textContent).toContain('待发起')
    expect(confirmEl.textContent).toContain('已发起')
    expect(confirmEl.textContent).toContain('王检察官（human）')
    await confirmReqModal()
    const emitted = wrapper!.emitted('requestTransition')
    expect(emitted).toHaveLength(1)
    expect(emitted![0][0]).toEqual({
      request_id: 'vr_r1',
      next_status: '已发起',
    })
  })

  it('登记表单：必填双字段齐才可提交，emit requestCreate 并复位收起', async () => {
    mountWith([], [])
    await flushPromises()
    await wrapper!.find('[data-testid="vw-req-new"]').trigger('click')
    expect(wrapper!.find('[data-testid="vw-req-form"]').exists()).toBe(true)
    // target/material 未填齐：提交按钮禁用
    const submit = () => wrapper!.find<HTMLButtonElement>('[data-testid="vw-req-submit"]')
    expect(submit().element.disabled).toBe(true)
    await wrapper!.find('[data-testid="vw-req-target"] input').setValue('市监局')
    expect(submit().element.disabled).toBe(true)
    await wrapper!.find('[data-testid="vw-req-material"] input').setValue('工商档案')
    expect(submit().element.disabled).toBe(false)
    await submit().trigger('click')
    await flushPromises()
    const emitted = wrapper!.emitted('requestCreate')
    expect(emitted).toHaveLength(1)
    expect(emitted![0][0]).toEqual({
      target: '市监局',
      material: '工商档案',
      legal_instrument: '',
      handler: '',
      due_date: '',
    })
    // 提交后复位并收起
    expect(wrapper!.find('[data-testid="vw-req-form"]').exists()).toBe(false)
  })

  it('「转调取台账」预填：外部渠道核查项路由数据填入登记表单', async () => {
    const ext = item({
      item_id: 'vi_ext', status: '待核查', channel: 'external',
      external: { target: '住建局招标办', material: '评标记录' },
    })
    mountWith([ext], [])
    await flushPromises()
    const routeBtn = wrapper!.find('[data-testid="vw-route-external"]')
    expect(routeBtn.exists()).toBe(true)
    expect(routeBtn.attributes('disabled')).toBeUndefined()  // 已启用（REQ-V-013 接线）
    await routeBtn.trigger('click')
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-req-form"]').exists()).toBe(true)
    expect((wrapper!.find('[data-testid="vw-req-target"] input').element as HTMLInputElement).value)
      .toBe('住建局招标办')
    expect((wrapper!.find('[data-testid="vw-req-material"] input').element as HTMLInputElement).value)
      .toBe('评标记录')
  })

  it('降级态：台账动作与登记按钮全部禁用', async () => {
    const transport = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path.includes('/verify-items'),
        respond: () => okEnvelope(pageOf([item({ item_id: 'vi_p1' })])),
      },
      {
        match: (r) => r.method === 'GET' && r.path.includes('/verify-requests'),
        respond: () => okEnvelope({ items: [req({ status: '待发起' })], available: true }),
      },
    ])
    setTransport(transport)
    wrapper = mount(VerifyWorkbench, {
      props: {
        caseId: 'c1', clueId: 'clue-1',
        operator: '王检察官', role: 'human',
        degraded: true, submitting: false,
      },
    })
    await flushPromises()
    expect(wrapper!.find('[data-testid="vw-req-new"]').element.disabled).toBe(true)
    const act = wrapper!.findAll('.vw-req-item')[0]
      .findAll('button')
      .find((b) => b.text().trim() === '发起')
    expect(act).toBeTruthy()
    expect(act!.element.disabled).toBe(true)
  })
})
