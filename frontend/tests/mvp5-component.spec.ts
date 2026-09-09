import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { NMessageProvider } from 'naive-ui'
import { flushPromises, mount } from '@vue/test-utils'
import { setTransport } from '../src/api/transport'
import { errEnvelope, FakeTransport, okEnvelope } from './helpers'
import CrossCaseView from '../src/views/CrossCaseView.vue'

// 组件级集成（happy-dom 真实挂载）：跨案页 chip 勾选（label/NCheckbox 双重 toggle
// 回归）、执行按钮闸门（授权数 + SQL + 事由）、全有或全无 403 落地。

function mountPage() {
  const cases = [
    { id: 'c1', tenant_id: 't1', name: '案件一', status: '侦查中', pack_id: 'default', pack_snapshot_at: '2026-09-08T10:00:00', created_at: '', created_by: '王' },
    { id: 'c2', tenant_id: 't1', name: '案件二', status: '侦查中', pack_id: 'default', pack_snapshot_at: '', created_at: '', created_by: '王' },
    { id: 'c3', tenant_id: 't1', name: '案件三', status: '待建案', pack_id: 'default', pack_snapshot_at: '', created_at: '', created_by: '王' },
  ]
  const transport = new FakeTransport([
    { match: (r) => r.method === 'GET' && r.path === '/cases', respond: () => okEnvelope(cases.map((c) => ({ ...c }))) },
    {
      match: (r) => r.method === 'GET' && r.path.startsWith('/cross-case/history'),
      respond: () => okEnvelope({ items: [], total: 0, page: 1, page_size: 20 }),
    },
    {
      match: (r) => r.method === 'POST' && r.path === '/cross-case/query',
      respond: (r) => {
        const b = r.body as { case_ids: string[] }
        if (b.case_ids.some((id) => !cases.some((c) => c.id === id))) {
          return errEnvelope(403, 'FORBIDDEN', '全有或全无鉴权失败：案件 cX 无权访问，本次查询整体拒绝')
        }
        return okEnvelope({ rows: b.case_ids.map((cid) => ({ source_case: cid, id: `${cid}-p1` })), total: b.case_ids.length, case_ids: b.case_ids })
      },
    },
  ])
  setTransport(transport)

  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(CrossCaseView) },
  })
  return { wrapper, transport }
}

function findExecButton(wrapper: ReturnType<typeof mount>): HTMLButtonElement {
  const btn = wrapper.findAll('button').find((b) => b.text().includes('执行跨案查询'))
  if (!btn) throw new Error('执行按钮未渲染')
  return btn.element as HTMLButtonElement
}

beforeEach(async () => {
  setActivePinia(createPinia())
  sessionStorage.clear()
})

describe('CrossCaseView 组件集成（真实挂载）', () => {
  it('chip 单击即选中（无双重 toggle 回退）；未满足条件时按钮禁用', async () => {
    const { wrapper } = mountPage()
    await flushPromises() // onMounted: loadCases + history

    const chips = wrapper.findAll('.case-chip')
    expect(chips).toHaveLength(3)
    await chips[0].trigger('click')
    await chips[1].trigger('click')
    await flushPromises()

    const checked = wrapper.findAll('.case-chip.checked')
    expect(checked).toHaveLength(2)
    expect(wrapper.text()).toContain('2/2 有权')

    // 未填 SQL/事由：禁用
    expect(findExecButton(wrapper).disabled).toBe(true)
  })

  it('SQL + 事由齐备后按钮可执行，点击落结果表（含 source_case 列）', async () => {
    const { wrapper, transport } = mountPage()
    await flushPromises()

    const chips = wrapper.findAll('.case-chip')
    await chips[0].trigger('click')
    await chips[1].trigger('click')

    const areas = wrapper.findAll('textarea')
    expect(areas.length).toBeGreaterThanOrEqual(2)
    const sql = "SELECT 'c1' AS source_case, t.* FROM case_c1.obj_person t UNION ALL SELECT 'c2' AS source_case, t.* FROM case_c2.obj_person t"
    await areas[0].setValue(sql)
    await areas[1].setValue('并案排查重合主体')
    await flushPromises()

    const btn = findExecButton(wrapper)
    expect(btn.disabled).toBe(false)
    await wrapper.find('button').trigger('click') // 兜底（NButton 事件绑定）
    btn.click()
    await flushPromises()

    const queryCall = transport.calls.find((c) => c.method === 'POST' && c.path === '/cross-case/query')
    expect(queryCall).toBeDefined()
    expect((queryCall?.body as { reason: string }).reason).toBe('并案排查重合主体')
    expect(wrapper.text()).toContain('source_case')
    expect(wrapper.text()).toContain('共 2 行')
  })

  it('事由为空：按钮保持禁用（留痕闸门）', async () => {
    const { wrapper } = mountPage()
    await flushPromises()

    const chips = wrapper.findAll('.case-chip')
    await chips[0].trigger('click')
    await chips[1].trigger('click')
    const areas = wrapper.findAll('textarea')
    await areas[0].setValue('SELECT 1')
    await flushPromises()

    expect(findExecButton(wrapper).disabled).toBe(true)
    expect(wrapper.text()).toContain('查询事由必填（审计留痕）')
  })

  it('含无权案件：denied 红条提示且执行按钮禁用（全有或全无 UI 闸门）', async () => {
    const { wrapper } = mountPage()
    await flushPromises()

    // c1 正常勾选 + 注入一个不在租户全集的选择（模拟授权全集变化）
    const chips = wrapper.findAll('.case-chip')
    await chips[0].trigger('click')
    const vm = wrapper.findComponent(CrossCaseView)
    const selectedRef = (vm.vm as unknown as { selected: string[] }).selected
    selectedRef.push('cX')
    await flushPromises()

    expect(wrapper.text()).toContain('无权案件')
    expect(wrapper.text()).toContain('全有或全无')
    expect(findExecButton(wrapper).disabled).toBe(true)
  })
})
