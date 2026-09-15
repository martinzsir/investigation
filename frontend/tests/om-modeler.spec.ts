import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { h } from 'vue'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import { degradeStaleBindings, type DegradedBinding } from '../src/domain/ontologyModel'
import { CARDINALITIES, CARDINALITY_LABEL, type ObjectType } from '../src/api/endpoints/model'
import type { MeInfo } from '../src/api/endpoints/auth'
import { useAuthStore } from '../src/stores/auth'
import SaveConfirmModal from '../src/components/om/SaveConfirmModal.vue'
import LinkEditModal from '../src/components/om/LinkEditModal.vue'
import OntologyModelerView from '../src/views/OntologyModelerView.vue'

// 可视化本体建模器（S2）测试：
//  - E2-1 数据元失联降级（PRD §10 / 线框 D3 / UC-02-4：静默保存成功 = FAIL）；
//  - 保存确认弹窗（理由必填 + R3 事实文案）；
//  - 关系弹窗（R1 名称锁定 + 唯一性/端点校验 + 基数）；
//  - 建模器集成：载入降级 → 危险确认 → PUT 负载（UC-S2-3 R2 字段不丢）。
// NModal 内容 teleport 到 body：断言/交互走 document.body（manual-node-modal 同口径）。

// G6 假实现（RelationGraphPanel 动态 import 在 happy-dom 下不可真渲染）
vi.mock('@antv/g6', () => ({
  Graph: class {
    constructor(_cfg: unknown) {}
    async render() {}
    destroy() {}
    on() {}
    setData() {}
  },
}))

const here = dirname(fileURLToPath(import.meta.url))
const view = (p: string) => resolve(here, '..', 'src', 'views', p)
const comp = (p: string) => resolve(here, '..', 'src', 'components', p)
const src = (p: string) => readFileSync(p, 'utf8')
/** 剥注释后的源码（结构断言只针对字符串字面量，不误伤红线声明注释） */
const srcNoComment = (p: string) => src(p).replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')

beforeEach(() => {
  sessionStorage.clear()
})

afterEach(() => {
  document.body.innerHTML = ''
  setTransport(null)
})

function findBtn(w: VueWrapper, label: string) {
  const b = w.findAll('button').find((x) => x.text().includes(label))
  if (!b) throw new Error(`button not found: ${label}`)
  return b
}

/** body 内按文本找按钮（NModal teleport 内容） */
function bodyBtn(label: string): HTMLButtonElement {
  const b = Array.from(document.body.querySelectorAll('button')).find(
    (x) => (x.textContent ?? '').includes(label),
  )
  if (!b) throw new Error(`body button not found: ${label}`)
  return b
}

function bodyHas(text: string): boolean {
  return (document.body.textContent ?? '').includes(text)
}

/** 原生 setValue + input 事件（NInput 可感知） */
function setValue(el: Element | null, value: string): void {
  const target = el as HTMLInputElement | HTMLTextAreaElement | null
  if (!target) throw new Error('input element not found')
  target.value = value
  target.dispatchEvent(new Event('input', { bubbles: true }))
}

// ---------------------------------------------------------------- 域逻辑
describe('E2-1 降级域逻辑（degradeStaleBindings）', () => {
  const DE = { DE_IDCARD: { name: '证件号码', type: 'string' } }
  const mk = (properties: Record<string, unknown>, name = 'person'): ObjectType =>
    ({ name, kind: 'entity', pk: 'x', properties, owner_note: '未知字段' }) as unknown as ObjectType

  it('失效 {"data_element"} → 降级为纯 string；有效绑定与纯类型不动；返回清单', () => {
    const os = [mk({ acct: { data_element: 'DE_GONE' }, id_card: { data_element: 'DE_IDCARD' }, memo: 'string' })]
    const out: DegradedBinding[] = degradeStaleBindings(os, DE)
    expect(out).toEqual([{ obj: 'person', prop: 'acct', de: 'DE_GONE' }])
    expect(os[0].properties.acct).toBe('string')
    expect(os[0].properties.id_card).toEqual({ data_element: 'DE_IDCARD' })
    expect(os[0].properties.memo).toBe('string')
  })

  it('显式 type 保留；未知键原样保留（R2）；缺 type 补 string', () => {
    const os = [mk({
      a: { type: 'date', data_element: 'DE_GONE' },
      b: { data_element: 'DE_GONE', composite: true, unknown_key: 'v' },
    })]
    degradeStaleBindings(os, DE)
    expect(os[0].properties.a).toEqual({ type: 'date' })
    expect(os[0].properties.b).toEqual({ type: 'string', composite: true, unknown_key: 'v' })
  })

  it('全域层 ∪ 案件层并集判定；原型链键（toString）不算有效数据元', () => {
    const os = [mk({ x: { data_element: 'DE_CASE' }, y: { data_element: 'toString' } })]
    const out = degradeStaleBindings(os, { DE_IDCARD: {}, DE_CASE: { type: 'integer' } })
    expect(out).toEqual([{ obj: 'person', prop: 'y', de: 'toString' }])
    expect(os[0].properties.x).toEqual({ data_element: 'DE_CASE' })
    expect(os[0].properties.y).toBe('string')
  })

  it('多对象（含未选中）一并降级；无 properties 对象安全跳过', () => {
    const os = [
      mk({ p: { data_element: 'DE_GONE' } }, 'a'),
      mk({ q: 'string' }, 'b'),
      { name: 'c', kind: 'event' } as unknown as ObjectType,
    ]
    const out = degradeStaleBindings(os, DE)
    expect(out).toEqual([{ obj: 'a', prop: 'p', de: 'DE_GONE' }])
    expect(os[0].properties.p).toBe('string')
    expect(os[1].properties.q).toBe('string')
    expect(os[2].properties).toBeUndefined()
  })
})

// ---------------------------------------------------------------- 保存确认弹窗
describe('保存确认弹窗（理由必填 + R3 事实文案）', () => {
  const mountModal = (props: Record<string, unknown>) =>
    mount(SaveConfirmModal, { props })

  it('结构性改动 → 理由必填：空理由禁用确认，填理由后放行', async () => {
    const w = mountModal({ show: true, structural: ['新增对象类型 x'], relational: [], semantic: [], reason: '' })
    await flushPromises()
    expect(bodyHas('必须填写理由'))
    expect(bodyBtn('确认保存').disabled).toBe(true)
    await w.setProps({ reason: '新增对手方实体承载过桥账户分析' })
    await flushPromises()
    expect(bodyBtn('确认保存').disabled).toBe(false)
    bodyBtn('确认保存').click()
    await w.vm.$nextTick()
    expect(w.emitted('confirm')).toHaveLength(1)
  })

  it('R3：文案只陈述事实（需重跑 BUILD），绝不承诺「自动触发」', async () => {
    mountModal({ show: true, structural: ['a'], relational: ['b'], semantic: [], reason: 'r' })
    await flushPromises()
    expect(bodyHas('保存本身不触发任何重跑'))
    expect(bodyHas('自动触发')).toBe(false)
  })

  it('纯语义改动 → 非危险：理由不强制，可直接确认', async () => {
    const w = mountModal({ show: true, structural: [], relational: [], semantic: ['title 变更'], reason: '' })
    await flushPromises()
    expect(bodyHas('必填')).toBe(false)
    expect(bodyBtn('确认保存').disabled).toBe(false)
    bodyBtn('确认保存').click()
    await w.vm.$nextTick()
    expect(w.emitted('confirm')).toHaveLength(1)
  })
})

// ---------------------------------------------------------------- 关系弹窗
describe('关系弹窗（R1 名称锁定 + 唯一性/端点校验 + 基数）', () => {
  const objectOptions = [{ label: 'person', value: 'person' }, { label: 'company', value: 'company' }]
  const mountModal = (props: Record<string, unknown>) =>
    mount(LinkEditModal, { props })
  const nameInput = (): HTMLInputElement => {
    const i = Array.from(document.body.querySelectorAll('input')).find(
      (x) => (x.getAttribute('placeholder') ?? '').includes('works_for'),
    )
    if (!i) throw new Error('name input not found')
    return i
  }

  it('编辑态：name 输入禁用（R1 同款）+ 不可改说明', async () => {
    mountModal({
      show: true, mode: 'edit',
      initial: { name: 'works_for', title: '', from_obj: 'person', to_obj: 'company', cardinality: '' },
      linkNames: [], objectOptions,
    })
    await flushPromises()
    expect(nameInput().disabled).toBe(true)
    expect(bodyHas('新建后不可改'))
  })

  it('新建态：关系名重复拦截；合法后 confirm 载荷含端点与基数', async () => {
    const w = mountModal({
      show: true, mode: 'create',
      initial: { name: '', title: '', from_obj: 'person', to_obj: 'company', cardinality: 'one_to_many' },
      linkNames: ['works_for'], objectOptions,
    })
    await flushPromises()
    setValue(nameInput(), 'works_for')
    await w.vm.$nextTick()
    bodyBtn('确定').click()
    await w.vm.$nextTick()
    expect(bodyHas('已存在'))
    expect(w.emitted('confirm')).toBeUndefined()

    setValue(nameInput(), 'works_with')
    await w.vm.$nextTick()
    bodyBtn('确定').click()
    await w.vm.$nextTick()
    expect(w.emitted('confirm')![0][0]).toEqual({
      name: 'works_with', title: '', from_obj: 'person', to_obj: 'company', cardinality: 'one_to_many',
    })
  })

  it('新建态：端点缺一 → 拦截（E3-1）', async () => {
    const w = mountModal({
      show: true, mode: 'create',
      initial: { name: '', title: '', from_obj: '', to_obj: '', cardinality: '' },
      linkNames: [], objectOptions,
    })
    await flushPromises()
    setValue(nameInput(), 'rel_x')
    await w.vm.$nextTick()
    bodyBtn('确定').click()
    await w.vm.$nextTick()
    expect(bodyHas('端点对象不能为空'))
    expect(w.emitted('confirm')).toBeUndefined()
  })

  it('基数枚举 = 3 档 + （空）兼容档（D5）', () => {
    expect(CARDINALITIES).toEqual(['one_to_one', 'one_to_many', 'many_to_many'])
    expect(CARDINALITY_LABEL.one_to_many).toBe('一对多')
    expect(src(comp('om/LinkEditModal.vue'))).toContain('（空）= 未声明，兼容历史数据')
  })
})

// ---------------------------------------------------------------- 建模器集成
describe('建模器集成：E2-1 载入降级 → 危险确认 → PUT 负载（UC-S2-3 / R3）', () => {
  const ME: MeInfo = { operator: '测试员', role: 'human', clearance: 3, tenant_id: 't1', is_admin: true }

  // R2 字段齐备：对象级 jian/jian_source + 未知键 owner_unit；属性含失联/有效绑定
  // fin 绑定行业层数据元 DE_FIN（S0-1 落地后三层并集判定，不得误降级）
  const objectsFixture: ObjectType[] = [
    {
      name: 'transaction', kind: 'event', pk: 'id', name_property: 'acct',
      jian: '内间', jian_source: '初审判决书P12',
      owner_unit: '第三大队',
      properties: {
        id: 'string',
        acct: { data_element: 'DE_GONE' },
        amount: { type: 'decimal', data_element: 'DE_GONE2' },
        id_card: { data_element: 'DE_IDCARD' },
        fin: { data_element: 'DE_FIN' },
      },
    },
  ]

  let wrapper: VueWrapper | null = null

  async function mountView(): Promise<{ puts: Array<Record<string, unknown>> }> {
    const puts: Array<Record<string, unknown>> = []
    const transport = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path === '/cases/c1/objects',
        respond: () => okEnvelope({ objects: JSON.parse(JSON.stringify(objectsFixture)), pack: 'reqd_case' }),
      },
      {
        match: (r) => r.method === 'GET' && r.path === '/cases/c1/links',
        respond: () => okEnvelope({ links: [], pack: 'reqd_case' }),
      },
      {
        match: (r) => r.method === 'GET' && r.path === '/cases/c1/data-elements-shared',
        respond: () => okEnvelope({ elements: { DE_IDCARD: { name: '证件号码', type: 'string' } } }),
      },
      {
        match: (r) => r.method === 'GET' && r.path === '/cases/c1/data-elements-industry',
        respond: () => okEnvelope({ industry: '金融', elements: { DE_FIN: { name: '银行账号', type: 'string' } } }),
      },
      {
        match: (r) => r.method === 'GET' && r.path === '/cases/c1/data-elements',
        respond: () => okEnvelope({ elements: {} }),
      },
      {
        match: (r) => r.method === 'PUT' && r.path === '/cases/c1/objects',
        respond: (r) => {
          puts.push(r.body as Record<string, unknown>)
          return okEnvelope({ saved: 1 })
        },
      },
    ])
    setTransport(transport)

    const pinia: Pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore(pinia).me = ME  // clearance 3 → 可写

    const router: Router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { render: () => null } },
        { path: '/tasks', component: { render: () => null } },
        { path: '/c/designer', component: { render: () => null } },
        { path: '/c/omodel', component: { render: () => null } },
      ],
    })
    router.push('/')
    await router.isReady()

    sessionStorage.setItem('sunzi.case', 'c1')  // case store 在 setup 时恢复
    wrapper = mount(
      { setup: () => () => h(NMessageProvider, () => h(NDialogProvider, () => h(OntologyModelerView))) },
      { global: { plugins: [pinia, router] } },
    )
    await flushPromises()
    return { puts }
  }

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
  })

  it('载入即降级：E2-1 提示条（对象.属性 + 原码）+ 脏标记；有效绑定不动', async () => {
    await mountView()
    const t = wrapper!.text()
    expect(t).toContain('2 处属性绑定的数据元不存在，已降级为普通类型')
    expect(t).toContain('transaction.acct（原 DE_GONE）')
    expect(t).toContain('transaction.amount（原 DE_GONE2）')
    // 行业层 DE_FIN（S0-1 三层并集）不得被误判失联
    expect(t).not.toContain('（原 DE_FIN）')
    expect(t).toContain('未保存')
    expect(t).toContain('DE_IDCARD')
  })

  it('保存走危险确认（理由必填）→ PUT 负载降级生效且 R2 字段不丢（UC-S2-3）', async () => {
    const { puts } = await mountView()
    await findBtn(wrapper!, '保存').trigger('click')
    await flushPromises()

    // 结构性改动 → 危险确认弹窗，理由必填（NModal 内容在 body）
    expect(bodyHas('结构性改动'))
    expect(bodyBtn('确认保存').disabled).toBe(true)
    setValue(document.body.querySelector('textarea'), '归档前清理失联数据元绑定')
    await flushPromises()
    expect(bodyBtn('确认保存').disabled).toBe(false)
    bodyBtn('确认保存').click()
    await flushPromises()

    expect(puts).toHaveLength(1)  // links 未动 → 不发 PUT links
    const body = puts[0] as unknown as { objects: Array<Record<string, unknown>>; reason?: string }
    expect(body.reason).toBe('归档前清理失联数据元绑定')
    const props = body.objects[0].properties as Record<string, unknown>
    expect(props.acct).toBe('string')                              // E2-1 降级
    expect(props.amount).toEqual({ type: 'decimal' })              // 显式 type 保留，失联绑定摘除
    expect(props.id_card).toEqual({ data_element: 'DE_IDCARD' })   // 有效绑定不动
    expect(props.fin).toEqual({ data_element: 'DE_FIN' })          // 行业层绑定保留
    const obj = body.objects[0]
    expect(obj.jian).toBe('内间')                                   // UC-S2-3：对象级未覆盖字段
    expect(obj.jian_source).toBe('初审判决书P12')
    expect(obj.owner_unit).toBe('第三大队')                          // UC-S2-3：未知键透传

    // R3：保存后反馈只陈述事实，绝不出现「已自动触发」
    expect(bodyHas('保存本身不触发任何重跑'))
    expect(bodyHas('自动触发')).toBe(false)
  })
})

// ---------------------------------------------------------------- 结构断言（红线落点）
describe('结构断言（R1 / R2 / R3 / D6 落点）', () => {
  it('R1：对象名无任何编辑绑定，锁定说明可见', () => {
    const vue = src(view('OntologyModelerView.vue'))
    expect(vue).toContain('对象名被 bindings / links 引用，不可修改（R1）')
    expect(vue).not.toMatch(/v-model[^>]*selectedObj\.name/)
  })

  it('R2：未覆盖字段提示 + E2-1 载入降级双入口接线（load 与放弃更改）', () => {
    const vue = src(view('OntologyModelerView.vue'))
    expect(vue).toContain('已原样保留')
    expect(vue.match(/degradeStaleBindings\(/g)).toHaveLength(2)
    expect(vue).toContain('已降级为普通类型')
  })

  it('S0-1 行业层接入：数据元三层并行加载 + 合并优先级 案件>行业>全域', () => {
    const vue = srcNoComment(view('OntologyModelerView.vue'))
    expect(vue).toContain('listIndustry')
    expect(vue).toContain('g-industry')
    expect(vue).toContain(
      '...sharedDE.value, ...industryDE.value, ...caseDE.value',
    )
  })

  it('R3：渲染文案无「自动触发」承诺；D6 确定性布局不使用 force/dagre', () => {
    expect(srcNoComment(view('OntologyModelerView.vue'))).not.toContain('自动触发')
    const graph = srcNoComment(comp('om/RelationGraphPanel.vue'))
    expect(graph).toContain('确定性布局')
    expect(graph).not.toMatch(/force|dagre/i)
  })

  it('保存确认弹窗独立实现（不复用含 RESCAN 假承诺的 ConfigConfirmDialog）', () => {
    const scm = srcNoComment(comp('om/SaveConfirmModal.vue'))
    expect(scm).not.toContain('ConfigConfirmDialog')
    expect(scm).not.toContain('自动触发')
    expect(scm).toContain('保存本身不触发任何重跑')
  })
})
