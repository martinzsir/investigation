import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ManualNodeModal from '../src/components/research/ManualNodeModal.vue'
import type { CanvasNode } from '../src/domain/canvas'

// RC-202 人工节点表单：字段级校验（与 domain/后端同口径）、编辑态回填、
// 提交事件上抛。NModal teleport 到 body，断言走 document.body。

function setValue(el: Element | null, value: string): void {
  const target = el as HTMLInputElement | HTMLTextAreaElement | null
  if (!target) throw new Error('input element not found')
  target.value = value
  target.dispatchEvent(new Event('input', { bubbles: true }))
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ManualNodeModal 侦查假设新增', () => {
  it('blocks submit when title/content empty and shows field errors', async () => {
    const w = mount(ManualNodeModal, {
      props: { show: true, mode: 'create', kind: 'hypothesis' },
    })
    const root = document.body.querySelector('[data-testid="manual-node-modal"]')
    expect(root).toBeTruthy()
    ;(document.body.querySelector(
      '[data-testid="manual-node-submit"]',
    ) as HTMLButtonElement).click()
    await w.vm.$nextTick()
    expect(document.body.textContent).toContain('请填写假设标题')
    expect(document.body.textContent).toContain('请填写假设内容')
    expect(w.emitted('submit')).toBeUndefined()
  })

  it('emits trimmed payload on valid submit', async () => {
    const w = mount(ManualNodeModal, {
      props: { show: true, mode: 'create', kind: 'hypothesis' },
    })
    setValue(
      document.body.querySelector('[data-testid="manual-node-title"] input'),
      '  张某为账户实际控制人  ',
    )
    setValue(
      document.body.querySelector('[data-testid="manual-node-content"] textarea'),
      '多条整数现金存入与 ATM 轨迹吻合',
    )
    await w.vm.$nextTick()
    ;(document.body.querySelector(
      '[data-testid="manual-node-submit"]',
    ) as HTMLButtonElement).click()
    const submit = w.emitted('submit')
    expect(submit).toHaveLength(1)
    expect(submit?.[0]).toEqual([
      {
        kind: 'hypothesis',
        title: '张某为账户实际控制人',
        content: '多条整数现金存入与 ATM 轨迹吻合',
      },
    ])
  })

  it('rejects over-limit title', async () => {
    const w = mount(ManualNodeModal, {
      props: { show: true, mode: 'create', kind: 'hypothesis' },
    })
    setValue(
      document.body.querySelector('[data-testid="manual-node-title"] input'),
      'a'.repeat(51),
    )
    setValue(
      document.body.querySelector('[data-testid="manual-node-content"] textarea'),
      '内容',
    )
    await w.vm.$nextTick()
    ;(document.body.querySelector(
      '[data-testid="manual-node-submit"]',
    ) as HTMLButtonElement).click()
    await w.vm.$nextTick()
    expect(document.body.textContent).toContain('不超过 50 字')
    expect(w.emitted('submit')).toBeUndefined()
  })
})

describe('ManualNodeModal 备注', () => {
  it('has no title field and only requires content', async () => {
    const w = mount(ManualNodeModal, {
      props: { show: true, mode: 'create', kind: 'note' },
    })
    expect(document.body.querySelector('[data-testid="manual-node-title"]')).toBeFalsy()
    ;(document.body.querySelector(
      '[data-testid="manual-node-submit"]',
    ) as HTMLButtonElement).click()
    await w.vm.$nextTick()
    expect(document.body.textContent).toContain('请填写备注内容')
    setValue(
      document.body.querySelector('[data-testid="manual-node-content"] textarea'),
      '注意：该账户凌晨高频交易',
    )
    await w.vm.$nextTick()
    ;(document.body.querySelector(
      '[data-testid="manual-node-submit"]',
    ) as HTMLButtonElement).click()
    expect(w.emitted('submit')?.[0]).toEqual([
      { kind: 'note', title: '', content: '注意：该账户凌晨高频交易' },
    ])
  })

  it('edit mode prefills existing hypothesis props', async () => {
    const node: CanvasNode = {
      id: 'cn_1',
      kind: 'hypothesis',
      ref: 'cn_1',
      label: '旧标题',
      system: false,
      adopted: false,
      stale: false,
      pinned: false,
      x: 480,
      y: 0,
      props: { title: '旧标题', content: '旧内容' },
    }
    const w = mount(ManualNodeModal, {
      props: { show: true, mode: 'edit', kind: 'hypothesis', node },
    })
    const title = document.body.querySelector(
      '[data-testid="manual-node-title"] input',
    ) as HTMLInputElement
    const content = document.body.querySelector(
      '[data-testid="manual-node-content"] textarea',
    ) as HTMLTextAreaElement
    expect(title.value).toBe('旧标题')
    expect(content.value).toBe('旧内容')
    expect(document.body.textContent).toContain('编辑侦查假设')
    w.unmount()
  })
})
