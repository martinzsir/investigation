import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { NMessageProvider, NSelect } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope, type FakeRoute } from './helpers'
import EvidencePanel from '../src/components/research/EvidencePanel.vue'
import {
  EVIDENCE_MAX_SIZE,
  EVIDENCE_MATERIAL_TYPES,
  evidenceSizeLabel,
  type EvidenceMaterial,
  type VerifyItem,
} from '../src/domain/verify'

// REQ-V-010/011 书证面板：清单渲染、multipart 上传（20MB 预检）、blob 下载、
// 挂接/解除 emit（202 编排由视图层负责，本组件只发 payload）。

function mat(over: Partial<EvidenceMaterial> = {}): EvidenceMaterial {
  return {
    material_id: 'm_x',
    item_id: '',
    material_type: '其他',
    filename: 'm_x_证据.pdf',
    orig_name: '证据.pdf',
    sha256: 'abc123',
    size: 2048,
    note: '',
    uploaded_by: '王检察官',
    uploaded_at: '2026-09-13 10:00:00',
    ...over,
  }
}

function vItem(over: Partial<VerifyItem> = {}): VerifyItem {
  return {
    item_id: 'vi_1',
    kind: 'manual',
    text: '核查中标方与张卫国的资金往来',
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

const LIST_PATH = '/cases/c1/clues/clue-1/evidence'

let wrapper: VueWrapper | null = null
let rootWrapper: VueWrapper | null = null

function mountPanel(
  materials: EvidenceMaterial[],
  opts: {
    routes?: FakeRoute[]
    items?: VerifyItem[]
    degraded?: boolean
    submitting?: boolean
  } = {},
) {
  // 可变清单：上传路由 push 新项后，refresh 能读到（模拟后端同步落盘）
  const list = [...materials]
  const transport = new FakeTransport([
    {
      match: (r) => r.method === 'GET' && r.path === LIST_PATH,
      respond: () => okEnvelope({ items: list }),
    },
    ...(opts.routes ?? []),
  ])
  setTransport(transport)
  rootWrapper = mount(NMessageProvider, {
    slots: {
      default: () => h(EvidencePanel, {
        caseId: 'c1',
        clueId: 'clue-1',
        degraded: opts.degraded ?? false,
        submitting: opts.submitting ?? false,
        items: opts.items ?? [vItem()],
      }),
    },
  })
  wrapper = rootWrapper.findComponent(EvidencePanel)
  return { panel: wrapper!, root: rootWrapper, transport, list }
}

/** 给 input[type=file] 塞文件并触发 change（happy-dom files 只读，defineProperty 兜底） */
async function pickFile(panel: VueWrapper, file: File): Promise<void> {
  const input = panel.find<HTMLInputElement>('[data-testid="ep-file"]').element
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  // blob 下载副作用：happy-dom 的 createObjectURL/anchor.click 兜底
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn(() => 'blob:test'),
    revokeObjectURL: vi.fn(),
  })
})

afterEach(() => {
  rootWrapper?.unmount()
  rootWrapper = null
  wrapper = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('REQ-V-010 书证面板：清单', () => {
  it('空清单渲染空态文案', async () => {
    const { panel } = mountPanel([])
    await flushPromises()
    expect(panel.find('[data-testid="evidence-panel"]').exists()).toBe(true)
    expect(panel.text()).toContain('暂无书证材料')
    expect(panel.findAll('.ep-item')).toHaveLength(0)
  })

  it('已挂接/未挂接两种行态：徽标、原名、大小、挂接入口差异', async () => {
    const { panel } = mountPanel([
      mat({
        material_id: 'm_linked', item_id: 'vi_1',
        material_type: '合同', orig_name: '中标合同.pdf', size: 1536,
      }),
      mat({
        material_id: 'm_free', material_type: '缴款单',
        orig_name: '缴款单.jpg', size: 512, note: '窗口期初',
      }),
    ])
    await flushPromises()

    const linked = panel.find('[data-material-id="m_linked"]')
    expect(linked.text()).toContain('中标合同.pdf')
    expect(linked.text()).toContain('合同')
    expect(linked.text()).toContain('2 KB') // evidenceSizeLabel(1536) → round(1.5)
    expect(linked.find('[data-testid="ep-linked"]').exists()).toBe(true)
    expect(linked.text()).toContain('挂于「核查中标方与张卫国的资金往来」')
    expect(linked.find('[data-testid="ep-unlink"]').exists()).toBe(true)
    expect(linked.find('[data-testid="ep-link"]').exists()).toBe(false)

    const free = panel.find('[data-material-id="m_free"]')
    expect(free.text()).toContain('缴款单.jpg')
    expect(free.text()).toContain('窗口期初')
    expect(free.find('[data-testid="ep-linked"]').exists()).toBe(false)
    expect(free.find('[data-testid="ep-unlink"]').exists()).toBe(false)
    expect(free.find('[data-testid="ep-link"]').exists()).toBe(true)
    expect(free.find('[data-testid="ep-link"]').attributes('disabled')).toBeDefined()
  })

  it('GET 失败显示错误条与重试，不抛出', async () => {
    // 清单端点传输层异常 → ApiError NETWORK → 面板内 catch 落错误条
    setTransport(new FakeTransport([{
      match: (r) => r.method === 'GET' && r.path === LIST_PATH,
      respond: () => {
        throw new Error('network down')
      },
    }]))
    rootWrapper = mount(NMessageProvider, {
      slots: {
        default: () => h(EvidencePanel, {
          caseId: 'c1', clueId: 'clue-1', degraded: false, items: [vItem()],
        }),
      },
    })
    wrapper = rootWrapper.findComponent(EvidencePanel)
    await flushPromises()
    expect(wrapper.find('.ep-error').exists()).toBe(true)
    expect(wrapper.text()).toContain('书证清单加载失败')
  })
})

describe('REQ-V-010 书证面板：上传（multipart 同步）', () => {
  it('选文件 → 提交：POST FormData（file/material_type/note），成功后刷新并收起表单', async () => {
    const { panel, transport, list } = mountPanel([], {
      routes: [{
        match: (r) => r.method === 'POST' && r.path === LIST_PATH,
        respond: () => {
          const created = mat({ material_id: 'm_new', orig_name: '新证据.png' })
          list.push(created)
          return okEnvelope(created)
        },
      }],
    })
    await flushPromises()
    await panel.find('[data-testid="ep-new"]').trigger('click')
    await flushPromises()

    await pickFile(panel, new File(['png-bytes'], '新证据.png', { type: 'image/png' }))
    expect(panel.find('[data-testid="ep-file-name"]').text()).toContain('新证据.png')

    await panel.find('[data-testid="ep-submit"]').trigger('click')
    await flushPromises()

    const post = transport.calls.find((c) => c.method === 'POST' && c.path === LIST_PATH)
    expect(post).toBeDefined()
    expect(post!.body).toBeInstanceOf(FormData)
    const fd = post!.body as FormData
    expect(fd.get('file')).toBeInstanceOf(File)
    expect((fd.get('file') as File).name).toBe('新证据.png')
    expect(fd.get('material_type')).toBe('其他') // 默认类型
    expect(post!.timeoutKind).toBe('upload')

    // 成功后二次 GET 刷新 + 新材料渲染 + 表单收起
    const gets = transport.calls.filter((c) => c.method === 'GET' && c.path === LIST_PATH)
    expect(gets.length).toBeGreaterThanOrEqual(2)
    expect(panel.text()).toContain('新证据.png')
    expect(panel.find('[data-testid="ep-form"]').exists()).toBe(false)
  })

  it('超过 20MB：前端拦截，不发 POST，并清空选择', async () => {
    const { panel, transport } = mountPanel([])
    await flushPromises()
    await panel.find('[data-testid="ep-new"]').trigger('click')
    await flushPromises()

    const big = new File(['x'], 'big.mp4')
    Object.defineProperty(big, 'size', {
      value: EVIDENCE_MAX_SIZE + 1, configurable: true,
    })
    await pickFile(panel, big)

    expect(panel.find('[data-testid="ep-file-name"]').exists()).toBe(false)
    expect(transport.calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('未选文件时上传按钮禁用', async () => {
    const { panel } = mountPanel([])
    await flushPromises()
    await panel.find('[data-testid="ep-new"]').trigger('click')
    await flushPromises()
    expect(panel.find('[data-testid="ep-submit"]').attributes('disabled')).toBeDefined()
  })
})

describe('REQ-V-010 书证面板：下载（blob 原名保存）', () => {
  it('点下载：GET .../download 拿 Blob 触发浏览器保存', async () => {
    const { panel, transport } = mountPanel([mat({ material_id: 'm_dl' })], {
      routes: [{
        match: (r) => r.method === 'GET'
          && r.path === `${LIST_PATH}/m_dl/download`,
        respond: () => ({ status: 200, data: new Blob(['pdf-bytes'], { type: 'application/pdf' }) }),
      }],
    })
    await flushPromises()
    await panel.find('[data-material-id="m_dl"]').find('[data-testid="ep-download"]').trigger('click')
    await flushPromises()

    expect(transport.calls.some((c) => c.method === 'GET'
      && c.path === `${LIST_PATH}/m_dl/download`)).toBe(true)
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
  })
})

describe('REQ-V-011 书证面板：挂接/解除（emit，202 由视图层编排）', () => {
  it('未挂接行：选核查项前挂接按钮禁用；选择后 emit link 载荷', async () => {
    const { panel } = mountPanel([mat({ material_id: 'm2' })], {
      items: [vItem({ item_id: 'vi_a', text: '核查事项 A' })],
    })
    await flushPromises()
    const row = panel.find('[data-material-id="m2"]')
    const linkBtn = row.find('[data-testid="ep-link"]')
    expect(linkBtn.attributes('disabled')).toBeDefined()

    await row.findComponent(NSelect).vm.$emit('update:value', 'vi_a')
    await flushPromises()
    expect(linkBtn.attributes('disabled')).toBeUndefined()

    await linkBtn.trigger('click')
    await flushPromises()
    expect(panel.emitted('link')![0][0]).toEqual({
      item_id: 'vi_a', material_id: 'm2',
    })
  })

  it('已挂接行：解除按钮 emit unlink 载荷', async () => {
    const { panel } = mountPanel([mat({ material_id: 'm3', item_id: 'vi_a' })])
    await flushPromises()
    await panel.find('[data-material-id="m3"]').find('[data-testid="ep-unlink"]').trigger('click')
    await flushPromises()
    expect(panel.emitted('unlink')![0][0]).toEqual({ material_id: 'm3' })
  })

  it('degraded：上传/挂接/解除控件全禁用', async () => {
    const { panel } = mountPanel(
      [
        mat({ material_id: 'm_free' }),
        mat({ material_id: 'm_linked', item_id: 'vi_1' }),
      ],
      { degraded: true },
    )
    await flushPromises()
    expect(panel.find('[data-testid="ep-new"]').attributes('disabled')).toBeDefined()
    expect(panel.find('[data-material-id="m_free"]')
      .find('[data-testid="ep-link"]').attributes('disabled')).toBeDefined()
    expect(panel.find('[data-material-id="m_linked"]')
      .find('[data-testid="ep-unlink"]').attributes('disabled')).toBeDefined()
  })

  it('无核查项时挂接下拉占位"无核查项"', async () => {
    const { panel } = mountPanel([mat({ material_id: 'm4' })], { items: [] })
    await flushPromises()
    const row = panel.find('[data-material-id="m4"]')
    expect(row.findComponent(NSelect).props('placeholder')).toBe('无核查项')
    expect(row.findComponent(NSelect).props('disabled')).toBe(true)
  })
})

describe('REQ-V-010 展示纯函数', () => {
  it('evidenceSizeLabel：B/KB/MB 三档', () => {
    expect(evidenceSizeLabel(0)).toBe('0 B')
    expect(evidenceSizeLabel(512)).toBe('512 B')
    expect(evidenceSizeLabel(2048)).toBe('2 KB')
    expect(evidenceSizeLabel(1024 * 1024)).toBe('1.0 MB')
    expect(evidenceSizeLabel(1536 * 1024)).toBe('1.5 MB')
  })

  it('类型白名单与后端 MATERIAL_TYPES 逐字对齐', () => {
    expect([...EVIDENCE_MATERIAL_TYPES]).toEqual(
      ['缴款单', '监控截图', '合同', '付款凭证', '审批文件', '其他'])
  })
})
