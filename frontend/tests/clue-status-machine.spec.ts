import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ClueStatusMachine from '../src/components/research/ClueStatusMachine.vue'

// REQ-V-008 固证/排除核查门禁前端提示（非阻断；真值拦截在 Worker）：
//  AC-5 verifyPending>0：confirm/exclude 确认弹窗顶部出现琥珀警示；=0 不出现；
//  非门禁动作（reset/file）不出现；警示不置灰确认按钮（服务端兜底）。

let wrapper: VueWrapper | null = null

function mountWith(props: Record<string, unknown> = {}) {
  wrapper = mount(ClueStatusMachine, {
    props: {
      status: '查证中',
      role: 'human',
      operator: '王检察官',
      degraded: false,
      ...props,
    },
  })
  return wrapper
}

async function openModal(label: string): Promise<void> {
  const btn = wrapper!.findAll('button')
    .find((b) => b.text().trim() === label)
  if (!btn) throw new Error(`未渲染动作按钮：${label}`)
  await btn.trigger('click')
  await flushPromises()
}

const warn = () => document.body.querySelector('[data-testid="csm-verify-warn"]')
/** 每例单弹窗且 body 已清空，直接在 body 范围按文案找按钮 */
const findBodyButton = (label: string) =>
  Array.from(document.body.querySelectorAll('button'))
    .find((b) => b.textContent?.trim() === label) as HTMLButtonElement | undefined
const confirmBtn = () => findBodyButton('确认提交')!

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('REQ-V-008 核查门禁弹窗警示', () => {
  it('verifyPending=2：固证弹窗出现琥珀警示且文案含数量；确认按钮仍可点（非阻断）', async () => {
    mountWith({ verifyPending: 2 })
    await openModal('固证')

    const el = warn()
    expect(el).not.toBeNull()
    expect(el!.textContent).toContain('2 项核查未结')
    expect(el!.textContent).toContain('服务端将拒绝')

    // 非拦截：确认按钮不置灰，提交正常 emit
    const btn = confirmBtn()
    expect(btn.disabled).toBe(false)
    await new DOMWrapper(btn).trigger('click')
    await flushPromises()
    expect(wrapper!.emitted('submit')![0][0]).toMatchObject({ action: 'confirm' })
  })

  it('verifyPending=0（缺省）：固证弹窗不出现警示', async () => {
    mountWith()
    await openModal('固证')
    expect(warn()).toBeNull()
    expect(confirmBtn().disabled).toBe(false)
  })

  it('verifyPending>0：排除弹窗同样警示；填理由后仍可提交', async () => {
    mountWith({ verifyPending: 1 })
    await openModal('排除线索')
    const el = warn()
    expect(el).not.toBeNull()
    expect(el!.textContent).toContain('1 项核查未结')

    // 排除理由必填：未填时禁用，补齐后可提交（警示不改变门禁序）
    const btn0 = confirmBtn()
    expect(btn0.disabled).toBe(true)
    const ta = document.body.querySelector('textarea') as HTMLTextAreaElement
    expect(ta).toBeTruthy()
    await new DOMWrapper(ta).setValue('经查证不成立')
    await flushPromises()
    expect(confirmBtn().disabled).toBe(false)
    await new DOMWrapper(confirmBtn()).trigger('click')
    await flushPromises()
    expect(wrapper!.emitted('submit')![0][0]).toMatchObject({
      action: 'exclude',
      reason: '经查证不成立',
    })
  })

  it('非门禁动作不出现警示：退回待查', async () => {
    mountWith({ verifyPending: 3 })
    await openModal('退回待查')
    expect(warn()).toBeNull()
  })

  it('非门禁动作不出现警示：立案（file 四重门禁弹窗保持原样）', async () => {
    mountWith({ status: '已固证', role: 'human', verifyPending: 3 })
    const fileBtn = wrapper!.findAll('button')
      .find((b) => b.text().includes('立案'))
    expect(fileBtn).toBeDefined()
    await fileBtn!.trigger('click')
    await flushPromises()
    expect(warn()).toBeNull()
    // file 弹窗原有法定依据门禁不变
    expect(document.body.querySelector('input')).toBeTruthy()
  })

  it('verifyPending 动态归零后重新打开弹窗：警示消失（随裁决刷新）', async () => {
    const w = mountWith({ verifyPending: 2 })
    await openModal('固证')
    expect(warn()).not.toBeNull()
    // 取消并等待裁决回传
    await new DOMWrapper(findBodyButton('取消')!).trigger('click')
    await flushPromises()
    await w.setProps({ verifyPending: 0 })
    await openModal('固证')
    expect(warn()).toBeNull()
  })
})
