import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { NMessageProvider, NSelect } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import type { RawResponse } from '../src/api/transport/types'
import { FakeTransport, okEnvelope } from './helpers'
import VlmView from '../src/views/VlmView.vue'
import { useCaseStore } from '../src/stores/case'
import type { EvidenceMaterial } from '../src/domain/verify'
import type { VlmDraftRecord, VlmDraftResult } from '../src/api/endpoints/vlm'

// P8 人验闭环：模型只产 draft；stale 拒升格；人比对原件填结论才写
// image_evidence（入图入报告）；降级/阻断留痕不影响确定性结果。

const CASE_EVIDENCE = '/cases/c1/evidence'
const DRAFT = '/cases/c1/vlm/draft'
const DRAFTS = '/cases/c1/vlm/drafts'
const VERIFY = '/cases/c1/vlm/drafts/pp-1/verify'

function mat(over: Partial<EvidenceMaterial> = {}): EvidenceMaterial {
  return {
    material_id: 'm_1', item_id: '', material_type: '监控截图',
    filename: 'm_1_a.jpg', orig_name: '发票.jpg', sha256: 'h1',
    size: 2048, note: '', uploaded_by: '王检察官',
    uploaded_at: '2026-09-13 10:00:00', clue_id: 'L1', ...over,
  }
}

function draftRec(over: Partial<VlmDraftRecord> = {}): VlmDraftRecord {
  return {
    proposal_id: 'pp-1', kind: 'image_draft', case_id: 'c1',
    status: 'draft', author: 'model:qwen-vl-max',
    created_at: '2026-09-13T12:00:00', stale: false,
    payload: {
      candidate: { title: '发票抬头', detail: '抬头：宏业建设有限公司', severity: 'info' },
      input: {
        origin: 'ai_draft', purpose: 'vlm_inspect', mode: 'cloud',
        model: 'qwen-vl-max', prompt_version: 'vlm-invoice-v1',
        image_uri: 'evidence/m_1/m_1_a.jpg', content_class: 'invoice',
        subject_type: 'person', subject_id: 'p1',
        exif_stripped: true, created_epoch: 1_789_000_000, ttl_s: 86_400,
      },
      _sort_hint: { model: 'qwen-vl-max', model_score: 0.9 },
    },
    ...over,
  }
}

const DRAFT_RESULT_OK: VlmDraftResult = {
  ok: true, mode: 'cloud', model: 'qwen-vl-max', log_id: null,
  proposals: [{
    proposal_id: 'pp-1', title: '发票抬头', detail: '抬头：宏业建设有限公司',
    severity: 'info', model_score: 0.9, stale: false,
    model: 'qwen-vl-max', prompt_version: 'vlm-invoice-v1',
  }],
  dropped: [],
}

let root: VueWrapper | null = null
let view: VueWrapper | null = null

interface MountOpts {
  evidence?: EvidenceMaterial[]
  draftsArr?: VlmDraftRecord[]
  draftResp?: () => RawResponse
  verifyResp?: () => RawResponse
  withCase?: boolean
}

function mountView(opts: MountOpts = {}) {
  const evidence = opts.evidence ?? [mat()]
  const draftsArr = opts.draftsArr ?? [draftRec()]
  const t = new FakeTransport([
    { match: (r) => r.method === 'GET' && r.path === CASE_EVIDENCE,
      respond: () => okEnvelope({ items: evidence }) },
    { match: (r) => r.method === 'GET' && r.path === DRAFTS,
      respond: () => okEnvelope({ drafts: draftsArr }) },
    { match: (r) => r.method === 'POST' && r.path === DRAFT,
      respond: () => (opts.draftResp ? opts.draftResp() : okEnvelope(DRAFT_RESULT_OK)) },
    { match: (r) => r.method === 'POST' && r.path === VERIFY,
      respond: () => (opts.verifyResp ? opts.verifyResp() : okEnvelope({})) },
  ])
  setTransport(t)

  setActivePinia(createPinia())
  if (opts.withCase !== false) useCaseStore().selectCase('c1')

  root = mount(NMessageProvider, {
    slots: { default: () => h(VlmView) },
  })
  view = root.findComponent(VlmView)
  return { t, evidence, draftsArr }
}

function buttonByText(w: VueWrapper, text: string) {
  return w.findAll('button').find((b) => b.text().includes(text))
}

beforeEach(() => {
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn(() => 'blob:test'),
    revokeObjectURL: vi.fn(),
  })
})

afterEach(() => {
  root?.unmount()
  root = null
  view = null
  document.body.innerHTML = ''
})

describe('图像研判：案件门', () => {
  it('未选案件：渲染空态且不发起任何请求', async () => {
    const { t } = mountView({ withCase: false })
    await flushPromises()
    expect(view!.text()).toContain('请先选择案件')
    expect(t.calls.some((c) => c.path.includes('/vlm/'))).toBe(false)
  })
})

describe('发起分析：书证图像选项', () => {
  it('图像下拉只列 JPEG/PNG（pdf 书证被过滤）', async () => {
    mountView({
      evidence: [
        mat({ material_id: 'm_img', filename: 'm_img_shot.png', orig_name: '截图.png' }),
        mat({ material_id: 'm_pdf', filename: 'm_pdoc.pdf', orig_name: '合同.pdf' }),
      ],
    })
    await flushPromises()
    const imageSelect = view!.findAllComponents(NSelect)[0]
    expect(imageSelect.props('options')).toHaveLength(1)
    expect((imageSelect.props('options') as any[])[0].value).toBe('evidence/m_img/m_img_shot.png')
  })

  it('成功发起：POST 正确载荷；成功提示「待核草案」；队列刷新', async () => {
    const { t } = mountView()
    await flushPromises()
    await view!.findAllComponents(NSelect)[0].vm
      .$emit('update:value', 'evidence/m_1/m_1_a.jpg')
    await flushPromises()

    await buttonByText(view!, '发起分析')!.trigger('click')
    await flushPromises()

    const post = t.calls.find((c) => c.method === 'POST' && c.path === DRAFT)!
    expect(post.body).toMatchObject({
      image_uri: 'evidence/m_1/m_1_a.jpg',
      content_class: 'invoice',
    })
    // 成功后触发队列刷新（GET drafts ≥ 2 次；toast 文案挂 body 不可断言）
    expect(t.calls.filter((c) => c.path === DRAFTS).length).toBeGreaterThanOrEqual(2)
  })

  it('未选图像：前端拦截，不发 POST', async () => {
    const { t } = mountView()
    await flushPromises()
    await buttonByText(view!, '发起分析')!.trigger('click')
    await flushPromises()
    // 拦截证明：零 POST（toast 提示挂 body 不可断言）
    expect(t.calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('降级：ok:false degraded → 降级 banner 留痕，无模型产出', async () => {
    mountView({
      draftResp: () => okEnvelope({
        ok: false, degraded: true, mode: 'cloud', model: null,
        reason: '视觉模型超时（30s）', proposals: [], dropped: [],
      }),
    })
    await flushPromises()
    await view!.findAllComponents(NSelect)[0].vm
      .$emit('update:value', 'evidence/m_1/m_1_a.jpg')
    await flushPromises()
    await buttonByText(view!, '发起分析')!.trigger('click')
    await flushPromises()

    expect(view!.find('.banner').exists()).toBe(true)
    expect(view!.find('.banner').text()).toContain('能力降级')
    expect(view!.find('.banner').text()).toContain('视觉模型超时')
    expect(view!.find('.banner').text()).toContain('未发起模型调用')
  })
})

describe('草案队列：状态与可核验性', () => {
  it('待核草案：标题/明细/模型/把握度/EXIF 留痕，含核验输入区', async () => {
    mountView()
    await flushPromises()
    const text = view!.text()
    expect(text).toContain('AI 草案待核')
    expect(text).toContain('发票抬头')
    expect(text).toContain('抬头：宏业建设有限公司')
    expect(text).toContain('qwen-vl-max · vlm-invoice-v1')
    expect(text).toContain('把握度 0.90')
    expect(text).toContain('EXIF 出网前已剥离')
    expect(view!.find('.verify-box').exists()).toBe(true)
  })

  it('stale 草案：过期标记 + 禁升格文案，无核验输入区', async () => {
    mountView({ draftsArr: [draftRec({ stale: true })] })
    await flushPromises()
    expect(view!.text()).toContain('已过期 stale')
    expect(view!.find('.stale-note').text()).toContain('不得人验升格')
    expect(view!.find('.verify-box').exists()).toBe(false)
    expect(buttonByText(view!, '核验通过')).toBeUndefined()
  })

  it('已通过（approved）：状态标签，无核验输入区', async () => {
    mountView({ draftsArr: [draftRec({ status: 'approved' })] })
    await flushPromises()
    expect(view!.find('.draft').classes()).toContain('done')
    expect(view!.text()).toContain('approved')
    expect(view!.find('.verify-box').exists()).toBe(false)
  })
})

describe('人验通过：入图入报告', () => {
  it('填核验结论 → POST verify → 队列刷新为 approved', async () => {
    const { t, draftsArr } = mountView({
      verifyResp: () => {
        draftsArr[0].status = 'approved'
        return okEnvelope({
          proposal_id: 'pp-1', status: 'approved',
          image_evidence: { image_evidence_id: 'imgev_1' },
        })
      },
    })
    await flushPromises()

    // textarea 顺序：[0]=发起分析要求，[1]=核验结论
    const areas = view!.findAll('textarea')
    await areas[1].setValue('与原件核对一致，抬头确为宏业建设')
    await flushPromises()

    await buttonByText(view!, '核验通过')!.trigger('click')
    await flushPromises()

    const post = t.calls.find((c) => c.method === 'POST' && c.path === VERIFY)!
    expect(post.body).toMatchObject({
      verify_conclusion: '与原件核对一致，抬头确为宏业建设',
      subject_type: 'person',
      subject_id: 'p1',
    })
    // 刷新后该草案为 approved（verifyResp 改写队列应答；toast 挂 body 不可断言）
    expect(view!.text()).toContain('approved')
    expect(view!.find('.verify-box').exists()).toBe(false)
  })

  it('核验结论为空：拦截不发 POST', async () => {
    const { t } = mountView()
    await flushPromises()
    await buttonByText(view!, '核验通过')!.trigger('click')
    await flushPromises()
    // 拦截证明：零 verify POST（toast 提示挂 body 不可断言）
    expect(t.calls.some((c) => c.method === 'POST' && c.path === VERIFY)).toBe(false)
  })

  it('主体类型/ID 缺失（草案发起时未带）：结论填了也拦截不发 POST', async () => {
    const rec = draftRec()
    rec.payload.input.subject_type = ''
    rec.payload.input.subject_id = ''
    const { t } = mountView({ draftsArr: [rec] })
    await flushPromises()
    const areas = view!.findAll('textarea')
    await areas[1].setValue('与原件核对一致，抬头确为宏业建设')
    await flushPromises()
    await buttonByText(view!, '核验通过')!.trigger('click')
    await flushPromises()
    // 后端 verify 强制合法主体（person/org/bid_project + 非空 ID），
    // 前端必须在缺主体时拦下，避免 400「收到 ''/''」
    expect(t.calls.some((c) => c.method === 'POST' && c.path === VERIFY)).toBe(false)
  })
})
