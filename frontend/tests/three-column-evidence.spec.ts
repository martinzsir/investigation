import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount, type VueWrapper } from '@vue/test-utils'
import ThreeColumnEvidence from '../src/components/research/ThreeColumnEvidence.vue'
import StatusBadge from '../src/components/common/StatusBadge.vue'
import type { EvidenceItem } from '../src/domain/clue'

// REQ-V-007 三栏待核实卡片联动：
//  AC-1 默认（不传新 props）渲染与现状兼容——静态卡、无事件；
//  AC-2 interactive：有核查项显示状态徽标、无项显示「点击核查」，点击/键盘 emit verify；
//  事实/推断栏不受影响。

const ITEMS: EvidenceItem[] = [
  { id: 'f1', kind: 'fact', text: '已核事实一' },
  {
    id: 'i1', kind: 'inference', text: '推断一',
    source_rows: [{ row_uri: 'bank:1' }], room: '资金',
  },
  { id: 'p1', kind: 'pending', text: '资金是否存在对价' },
  { id: 'p2', kind: 'pending', text: '通话关系待核实' },
]

function pendingCards(w: VueWrapper) {
  return w.findAll('.ev-col--pending .ev-item')
}

beforeEach(() => {
  // StatusBadge（推断栏 room 徽标）经 useCaseOntologyConfig 触达 pinia store
  setActivePinia(createPinia())
})

describe('REQ-V-007 AC-1 默认模式向后兼容（零新增 props）', () => {
  it('pending 卡为静态卡：无 role/tabindex/联动脚注，点击不 emit verify', async () => {
    const w = mount(ThreeColumnEvidence, { props: { items: ITEMS } })
    const cards = pendingCards(w)
    expect(cards).toHaveLength(2)
    for (const c of cards) {
      expect(c.classes()).toEqual(['ev-item', 'ev-item--pending'])
      expect(c.attributes('role')).toBeUndefined()
      expect(c.attributes('tabindex')).toBeUndefined()
      expect(c.find('.ev-verify-foot').exists()).toBe(false)
      expect(c.find('.ev-item--link').exists()).toBe(false)
    }
    await cards[0].trigger('click')
    expect(w.emitted('verify')).toBeUndefined()
  })
})

describe('REQ-V-007 AC-2 interactive 待核实卡联动', () => {
  it('有核查项的卡显示状态徽标（内联状态色），无项卡显示「点击核查」', () => {
    const w = mount(ThreeColumnEvidence, {
      props: {
        items: ITEMS,
        interactive: true,
        itemStatusByText: new Map([['资金是否存在对价', '核查中']]),
      },
    })
    const cards = pendingCards(w)

    // 可交互语义
    for (const c of cards) {
      expect(c.classes()).toContain('ev-item--link')
      expect(c.attributes('role')).toBe('button')
      expect(c.attributes('tabindex')).toBe('0')
      expect(c.attributes('aria-label')).toContain('跳转核查')
    }

    // 命中映射：状态徽标 + 内联色调（verifyStatusMeta 出样式，hex 经 happy-dom 归一化不断言具体色值）
    const badge = cards[0].find('[data-testid="ev-verify-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('核查中')
    expect(badge.attributes('style') ?? '').toMatch(/color/)

    // 未命中映射：行动引导，无徽标
    expect(cards[1].find('[data-testid="ev-verify-badge"]').exists()).toBe(false)
    expect(cards[1].find('.ev-verify-go').text()).toContain('点击核查')
  })

  it('点击 / Enter / Space 均 emit verify(文本)', async () => {
    const w = mount(ThreeColumnEvidence, {
      props: { items: ITEMS, interactive: true },
    })
    const card = pendingCards(w)[1]
    await card.trigger('click')
    await card.trigger('keydown.enter')
    await card.trigger('keydown.space')
    const emitted = w.emitted('verify')
    expect(emitted).toHaveLength(3)
    expect(emitted?.every((e) => e[0] === '通话关系待核实')).toBe(true)
  })

  it('事实栏/推断栏不受 interactive 影响：推断 room 徽标照常、缺溯源拒绝渲染计数仍在', () => {
    const w = mount(ThreeColumnEvidence, { props: { items: ITEMS, interactive: true } })
    expect(w.findAll('.ev-col--fact .ev-item')).toHaveLength(1)
    expect(w.findAll('.ev-col--inference .ev-item')).toHaveLength(1)
    expect(w.findComponent(StatusBadge).exists()).toBe(true)
    // 事实/推断卡不获得联动样式与语义
    expect(w.find('.ev-col--fact .ev-item--link').exists()).toBe(false)
    expect(w.find('.ev-col--inference .ev-item--link').exists()).toBe(false)
  })

  it('状态映射随 props 更新：采纳后状态变化驱动徽标切换', async () => {
    const w = mount(ThreeColumnEvidence, {
      props: {
        items: ITEMS,
        interactive: true,
        itemStatusByText: new Map(),
      },
    })
    const card = pendingCards(w)[0]
    expect(card.find('[data-testid="ev-verify-badge"]').exists()).toBe(false)
    await w.setProps({
      itemStatusByText: new Map([['资金是否存在对价', '已证实']]),
    })
    const badge = card.find('[data-testid="ev-verify-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('已证实')
  })
})
