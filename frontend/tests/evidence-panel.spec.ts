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

// ---- P8 回流：材料卡 AI 图像分析 + findings 子列表（发起→待核→人验闭环） ----
const FINDINGS_PATH = '/cases/c1/vlm/findings'
const DRAFT_PATH = '/cases/c1/vlm/draft'

function imgMat(over: Partial<EvidenceMaterial> = {}): EvidenceMaterial {
  return mat({
    material_id: 'm_img', material_type: '缴款单',
    filename: 'm_img_缴款单.jpg', orig_name: '缴款单.jpg',
    ...over,
  })
}

function pendingFinding(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    proposal_id: 'pp-1', title: '异常缴款', detail: '备注含现金字样',
    severity: 'warn', image_uri: 'evidence/m_img/m_img_缴款单.jpg',
    model: 'qwen-vl-max', model_score: 0.9, stale: false,
    created_at: '2026-09-19 10:00:00',
    ...over,
  }
}

function verifiedFinding(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    image_evidence_id: 'imgev_1', title: '异常缴款', detail: '备注含现金字样',
    severity: 'warn', image_uri: 'evidence/m_img/m_img_缴款单.jpg',
    model: 'qwen-vl-max', model_score: 0.9, verifier: '王检察官',
    verify_conclusion: '与原件一致', subject_type: 'person', subject_id: 'p1',
    clue_id: 'clue-1', created_at: '2026-09-19 11:00:00',
    ...over,
  }
}

function findingsRoute(
  findings: Record<string, unknown>,
): FakeRoute {
  return {
    match: (r) => r.method === 'GET' && r.path === FINDINGS_PATH,
    respond: () => okEnvelope({ findings }),
  }
}

describe('P8 回流：材料卡发起 AI 图像分析', () => {
  it('图像材料显示发起入口；非图像材料不显示', async () => {
    const { panel } = mountPanel(
      [imgMat(), mat({ material_id: 'm_pdf' })],
      { routes: [findingsRoute({})] },
    )
    await flushPromises()
    expect(panel.find('[data-material-id="m_img"]')
      .find('[data-testid="ep-ai-new"]').exists()).toBe(true)
    expect(panel.find('[data-material-id="m_pdf"]')
      .find('[data-testid="ep-ai-new"]').exists()).toBe(false)
  })

  it('展开表单 → 发起分析：POST draft（image_uri 按材料构造，类别默认按材料类型）', async () => {
    const { panel, transport } = mountPanel([imgMat()], {
      routes: [
        findingsRoute({}),
        {
          match: (r) => r.method === 'POST' && r.path === DRAFT_PATH,
          respond: () => okEnvelope({
            ok: true, mode: 'local', model: 'qwen-vl-max',
            proposals: [{
              proposal_id: 'pp-1', title: '异常缴款', detail: '备注含现金字样',
              severity: 'warn', model_score: 0.9, stale: false,
              model: 'qwen-vl-max', prompt_version: 'vlm-invoice-v1',
            }],
            dropped: [],
          }),
        },
      ],
    })
    await flushPromises()
    await panel.find('[data-material-id="m_img"]')
      .find('[data-testid="ep-ai-new"]').trigger('click')
    await flushPromises()
    expect(panel.find('[data-testid="ep-ai-form"]').exists()).toBe(true)

    await panel.find('[data-testid="ep-ai-submit"]').trigger('click')
    await flushPromises()

    const post = transport.calls.find((c) => c.method === 'POST'
      && c.path === DRAFT_PATH)
    expect(post).toBeDefined()
    expect((post!.body as Record<string, unknown>).image_uri)
      .toBe('evidence/m_img/m_img_缴款单.jpg')
    expect((post!.body as Record<string, unknown>).content_class)
      .toBe('invoice') // 缴款单 → 默认 invoice
    // 发起成功后 findings 重拉（GET findings 两次：挂载 + 发起后）
    const gets = transport.calls.filter((c) => c.method === 'GET'
      && c.path === FINDINGS_PATH)
    expect(gets.length).toBeGreaterThanOrEqual(2)
    expect(panel.find('[data-testid="ep-ai-form"]').exists()).toBe(false)
  })

  it('degraded：发起入口禁用', async () => {
    const { panel } = mountPanel([imgMat()], {
      degraded: true, routes: [findingsRoute({})],
    })
    await flushPromises()
    expect(panel.find('[data-testid="ep-ai-new"]').attributes('disabled'))
      .toBeDefined()
  })
})

describe('P8 回流：findings 子列表渲染与内联核验', () => {
  it('待核草案（AI 草案徽标/明细/severity）+ 已人验（结论/核验人）分区渲染', async () => {
    const { panel } = mountPanel([imgMat()], {
      routes: [findingsRoute({
        m_img: {
          pending: [pendingFinding()],
          verified: [verifiedFinding()],
        },
      })],
    })
    await flushPromises()
    const findings = panel.find('[data-testid="ep-findings"]')
    expect(findings.exists()).toBe(true)
    expect(findings.text()).toContain('AI 草案')
    expect(findings.text()).toContain('异常缴款')
    expect(findings.text()).toContain('备注含现金字样')
    expect(findings.text()).toContain('已人验')
    expect(findings.text()).toContain('结论：与原件一致')
    expect(findings.text()).toContain('王检察官')
    // 待核带内联核验表单；已人验无
    expect(findings.find('[data-testid="ep-inline-verify"]').exists()).toBe(true)
  })

  it('stale 草案不渲染内联核验表单', async () => {
    const { panel } = mountPanel([imgMat()], {
      routes: [findingsRoute({
        m_img: { pending: [pendingFinding({ stale: true })], verified: [] },
      })],
    })
    await flushPromises()
    const findings = panel.find('[data-testid="ep-findings"]')
    expect(findings.text()).toContain('已过期')
    expect(findings.find('[data-testid="ep-inline-verify"]').exists()).toBe(false)
  })

  it('内联核验：结论/主体校验 + 提交走人验端点（clue_id 预填当前线索）', async () => {
    const { panel, transport } = mountPanel([imgMat()], {
      routes: [
        findingsRoute({
          m_img: { pending: [pendingFinding()], verified: [] },
        }),
        {
          match: (r) => r.method === 'POST'
            && r.path === '/cases/c1/vlm/drafts/pp-1/verify',
          respond: () => okEnvelope({
            proposal_id: 'pp-1', status: 'approved',
            image_evidence: { image_evidence_id: 'imgev_9' },
          }),
        },
      ],
    })
    await flushPromises()
    const verifyBox = panel.find('[data-testid="ep-inline-verify"]')

    // 未填结论点击 → 阻断（无 POST）
    await verifyBox.find('[data-testid="ep-inline-verify-submit"]').trigger('click')
    await flushPromises()
    expect(transport.calls.some((c) => c.method === 'POST'
      && c.path === '/cases/c1/vlm/drafts/pp-1/verify')).toBe(false)

    await verifyBox.find('[data-testid="ep-inline-verify-conclusion"] textarea')
      .setValue('与原件一致')
    await verifyBox.findComponent(NSelect).vm.$emit('update:value', 'person')
    await verifyBox.find('[data-testid="ep-inline-verify-subject-id"] input')
      .setValue('p1')
    await verifyBox.find('[data-testid="ep-inline-verify-submit"]').trigger('click')
    await flushPromises()

    const post = transport.calls.find((c) => c.method === 'POST'
      && c.path === '/cases/c1/vlm/drafts/pp-1/verify')
    expect(post).toBeDefined()
    expect(post!.body).toEqual({
      verify_conclusion: '与原件一致',
      subject_type: 'person',
      subject_id: 'p1',
      clue_id: 'clue-1',
    })
    // 核验成功后 findings 重拉
    expect(transport.calls.filter((c) => c.method === 'GET'
      && c.path === FINDINGS_PATH).length).toBeGreaterThanOrEqual(2)
  })

  it('查看图像：按 image_uri 反解 material_id 走书证下载流', async () => {
    const { panel, transport } = mountPanel([imgMat()], {
      routes: [
        findingsRoute({
          m_img: { pending: [], verified: [verifiedFinding()] },
        }),
        {
          match: (r) => r.method === 'GET'
            && r.path === `${LIST_PATH}/m_img/download`,
          respond: () => ({ status: 200, data: new Blob(['jpg'], { type: 'image/jpeg' }) }),
        },
      ],
    })
    await flushPromises()
    await panel.find('[data-testid="ep-findings"]')
      .find('button').trigger('click') // 已人验块的「查看图像」
    await flushPromises()
    expect(transport.calls.some((c) => c.method === 'GET'
      && c.path === `${LIST_PATH}/m_img/download`)).toBe(true)
  })

  it('无 findings 材料不渲染子列表；findings 端点失败不阻塞书证清单', async () => {
    setTransport(new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === LIST_PATH,
        respond: () => okEnvelope({ items: [imgMat()] }),
      },
      {
        match: (r) => r.method === 'GET' && r.path === FINDINGS_PATH,
        respond: () => { throw new Error('findings down') },
      },
    ]))
    rootWrapper = mount(NMessageProvider, {
      slots: {
        default: () => h(EvidencePanel, {
          caseId: 'c1', clueId: 'clue-1', degraded: false, items: [vItem()],
        }),
      },
    })
    wrapper = rootWrapper.findComponent(EvidencePanel)
    await flushPromises()
    // 书证清单正常、材料卡仍在
    expect(wrapper.find('[data-material-id="m_img"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="ep-findings"]').exists()).toBe(false)
    // findings 失败软提示
    expect(wrapper.find('[data-testid="ep-findings-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('图像 findings 加载失败')
  })
})
