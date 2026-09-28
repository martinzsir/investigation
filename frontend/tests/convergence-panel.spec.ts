import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import { setTransport, getTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope, type FakeRoute } from './helpers'
import ConvergenceView from '../src/views/ConvergenceView.vue'
import { useCaseStore } from '../src/stores/case'
import type { ConvergenceItem } from '../src/api/endpoints/convergence'

// 三维交汇读面（人-时-地锚点）三条红线的前端守护：
//   红线1 分数不是结论 —— 三维必须**并列**渲染 count/precision/weight，
//        只给一个总分就等于把精度加权重新掩盖（12 条 date 档 ≠ 4 条 minute 档）；
//   红线2 重名不猜 —— person_ambiguous 的锚点单列，不混进普通排序；
//   红线3 裁决未生效要看得见 —— 拿不到语义层连接时必须显式告警，不静默按名归一。

function dim(hit: boolean, count: number, precision: string, weight: number) {
  return {
    hit, count, precision, precision_dist: { [precision]: count },
    weight, reason: hit ? null : '该维度无命中', kinds: hit ? ['accompany'] : [],
  }
}

function item(over: Partial<ConvergenceItem> = {}): ConvergenceItem {
  return {
    key: 'person_07ed989d84fd|2020-03-24',
    person_key: 'person_07ed989d84fd',
    person_names: ['李志强'],
    person_ambiguous: false,
    ambiguous_candidates: [],
    date: '2020-03-24',
    location_ids: ['loc_d124ca20b7db'],
    std_addresses: ['浙江省/杭州市/拱墅区/浙江省杭州市拱墅区莫干山路'],
    co_present: ['张卫国'],
    dimensions: {
      space: dim(true, 4, 'minute', 1.6) as never,
      time: dim(true, 12, 'date', 0.6) as never,
      relation: dim(false, 0, 'unknown', 0) as never,
    },
    dim_hit_count: 2,
    hit_dimensions: ['space', 'time'],
    score: 2.2,
    max_score: 9,
    claims: ['李志强 于 2020-03-24 在 莫干山路 存在二维锚点'],
    falsification: '同框地点若为职务性地点则属工作常态',
    ...over,
  }
}

const AMBIG = item({
  key: '?张卫国|2020-03-24',
  person_key: '?张卫国',
  person_names: ['张卫国'],
  person_ambiguous: true,
  ambiguous_candidates: ['person_ecb52c3719fc'],
  score: 3.6,
  dim_hit_count: 3,
})

function listPayload(over: Record<string, unknown> = {}) {
  return {
    available: true,
    convergences: [item(), AMBIG],
    total: 2, returned: 2, page: 1, page_size: 50, min_dims: 2,
    stats: {
      by_dimension: { space: 2, time: 2, relation: 0 },
      by_hit_count: { '1': 0, '2': 1, '3': 1 },
      ambiguous: 1, max_score: 3.6,
    },
    weight_model: {
      dim_weight: { space: 1, time: 1, relation: 1 },
      precision_weight: { second: 1, minute: 1, hour: 0.5, date: 0.2, unknown: 0.1 },
      repeat_step: 0.2, repeat_cap: 3,
      formula: 'score = Σ_dims (dim_weight × precision_weight × repeat_factor)',
    },
    note: '交汇强度表示核查优先级，不代表风险高低',
    diagnostics: {
      homonym_resolution: { status: 'ok', detail: '已连接语义层，同名异人裁决生效' },
      observations_used: 21,
    },
    ...over,
  }
}

function detailPayload() {
  return {
    available: true,
    convergence: AMBIG,
    support: {
      space: [{
        observation_id: 'obs_s1', skill_id: 'geo_anomaly', lens_name: '轨迹异常检测',
        title: '张卫国 偏离常驻路线 3 次', subject: '张卫国',
        basis: '3 个工作日脱离日常路线进入莫干山路', falsification: '临时公务亦会造成偏离',
        degraded: false, degraded_reason: '', facts: [], facts_total: 0,
      }],
      time: [],
      relation: [{
        observation_id: 'obs_r1', skill_id: 'relation_common_neighbors',
        lens_name: '共同邻居', title: '张卫国-李志强 共同关联',
        subject: '张卫国', basis: '两主体共享联系人', falsification: '',
        degraded: true, degraded_reason: '坐标缺失', facts: [], facts_total: 0,
      }],
    },
    observations_index: 21,
    weight_model: null,
    note: '',
    diagnostics: {},
  }
}

let wrapper: VueWrapper | null = null

async function mountView(routes: FakeRoute[]): Promise<VueWrapper> {
  setTransport(new FakeTransport(routes))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/c/observations/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()

  wrapper = mount(NMessageProvider, {
    slots: { default: () => h(ConvergenceView) },
    global: { plugins: [router] },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

function calls(pred: (m: string, p: string) => boolean) {
  const t = getTransport() as unknown as { calls?: Array<{ method: string; path: string }> }
  return (t.calls ?? []).filter((c) => pred(c.method, c.path))
}

beforeEach(() => {
  setActivePinia(createPinia())
  useCaseStore().currentCaseId = 'c1'
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('三维交汇读面', () => {
  it('红线1：三维并列渲染条数+精度档+权重，不靠一个总分表态', async () => {
    const w = await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope(listPayload()),
      },
    ])

    const matrix = w.find('[data-testid="cc-matrix"]')
    expect(matrix.exists()).toBe(true)

    // 空间：4 条 minute 档 权重 1.6
    expect(w.find('[data-testid="cc-cell-space"]').text()).toContain('4 条')
    expect(w.find('[data-testid="cc-cell-space"]').text()).toContain('分钟级')
    expect(w.find('[data-testid="cc-cell-space"]').text()).toContain('权 1.6')

    // 时间：12 条但 date 档 —— 条数多一倍、权重只有 0.6，必须看得出差别
    expect(w.find('[data-testid="cc-cell-time"]').text()).toContain('12 条')
    expect(w.find('[data-testid="cc-cell-time"]').text()).toContain('日期级')
    expect(w.find('[data-testid="cc-cell-time"]').text()).toContain('权 0.6')

    // 关系：未命中要显式写"—"，不许由其他维度盖过去
    expect(w.find('[data-testid="cc-cell-relation"]').text()).toContain('—')
  })

  it('红线1：默认只发 min_dims=2（单维不是交汇）', async () => {
    await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope(listPayload()),
      },
    ])
    const c = calls((m, p) => m === 'GET' && p.includes('/convergence'))
    expect(c.length).toBeGreaterThan(0)
    expect(c[0].path).toContain('min_dims=2')
  })

  it('红线2：重名待裁决的锚点单列，不混进普通排序', async () => {
    const w = await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope(listPayload()),
      },
    ])

    const ambList = w.find('[data-testid="cv-list-ambiguous"]')
    expect(ambList.exists()).toBe(true)
    expect(ambList.text()).toContain('张卫国')
    // 分组标题要说明"不可当作单一自然人"
    expect(w.find('[data-testid="cv-group-ambiguous"]').text()).toContain('重名待裁决')

    // 确定的主体进普通区，不在重名区
    const normal = w.find('[data-testid="cv-list"]')
    expect(normal.text()).toContain('李志强')
    expect(ambList.text()).not.toContain('李志强')
  })

  it('红线3：裁决未生效时顶部显式告警，不静默按名归一', async () => {
    const w = await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope(listPayload({
          diagnostics: {
            homonym_resolution: {
              status: 'unavailable',
              detail: '无法打开案件库：IO',
              effect: '同名异人裁决未执行，主体按名归一；重名将被静默合并',
            },
          },
        })),
      },
    ])

    const alert = w.find('[data-testid="cv-homonym-alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('unavailable')
    expect(alert.text()).toContain('静默合并')
  })

  it('裁决正常时不打扰（无告警）', async () => {
    const w = await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope(listPayload()),
      },
    ])
    expect(w.find('[data-testid="cv-homonym-alert"]').exists()).toBe(false)
  })

  it('展开锚点拉取支撑观察；未命中维度写明"无支撑观察"', async () => {
    const w = await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope(listPayload()),
      },
      {
        // 详情 key 含 `|`，须 encodeURIComponent 后拼路径
        match: (r) => r.method === 'GET' && r.path.includes('/convergence/'),
        respond: () => okEnvelope(detailPayload()),
      },
    ])

    // 展开重名锚点（第一张卡片在重名区）
    await w.find('[data-testid="cv-list-ambiguous"] [data-testid="cc-head"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const detail = w.find('[data-testid="cc-detail"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('轨迹异常检测')
    expect(detail.text()).toContain('共同邻居')
    // 时间维无支撑：必须写明，不许由空间/关系的热度盖过去
    expect(detail.text()).toContain('时间维 · 支撑观察 0')
    expect(detail.text()).toContain('该维度无支撑观察')
    // 证伪条件随详情露出
    expect(detail.text()).toContain('证伪条件')
  })

  it('无观察档案时显示不可用原因，不报错', async () => {
    const w = await mountView([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/cases/c1/convergence'),
        respond: () => okEnvelope({
          available: false, convergences: [], total: 0, returned: 0,
          page: 1, page_size: 50, min_dims: 2, stats: null, weight_model: null,
          note: '案件尚未 BUILD，无观察档案可归集',
          diagnostics: {},
        }),
      },
    ])
    const box = w.find('[data-testid="cv-unavailable"]')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('尚未 BUILD')
  })
})
