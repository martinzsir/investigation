/**
 * 交汇证据图构图层测试。
 *
 * 守两条红线（与 ConvergenceCard.vue 同口径）：
 *   R-1 精度档必须在图上可分辨——date 档虚线、minute 档实线；
 *       两者若长得一样，"12 条 date 档"会被误读成强于"4 条 minute 档"。
 *   R-2 重名不猜——ambiguous 锚点须带标记，不混同普通锚点。
 */
import { describe, expect, it } from 'vitest'
import { buildConvergenceGraph, GRAPH_LEGEND } from '../src/domain/convergence-graph'
import type { ConvergenceDetailResult, ConvergenceItem } from '../src/api/endpoints/convergence'

function dim(hit: boolean, over: Partial<any> = {}) {
  return {
    hit,
    count: hit ? 4 : 0,
    precision: 'minute',
    weight: 1.6,
    reason: hit ? null : '无该维度观察',
    kinds: hit ? ['accompany'] : [],
    ...over,
  }
}

function item(over: Partial<ConvergenceItem> = {}): ConvergenceItem {
  return {
    key: 'k1',
    person_key: 'person_ecb52c3719fc',
    person_names: ['张卫国'],
    person_ambiguous: false,
    ambiguous_candidates: [],
    date: '2020-03-24',
    location_ids: ['loc_1'],
    std_addresses: ['浙江省/杭州市/拱墅区/莫干山路111号'],
    co_present: ['李志强'],
    dimensions: { space: dim(true), time: dim(false), relation: dim(true) },
    dim_hit_count: 2,
    hit_dimensions: ['space', 'relation'],
    score: 3.2,
    max_score: 9,
    claims: [],
    falsification: '',
    ...over,
  } as ConvergenceItem
}

describe('交汇证据图构图', () => {
  it('中心为锚点（人·时·地），三维各出节点并挂到中心', () => {
    const g = buildConvergenceGraph(item())
    const anchor = g.nodes.find((n) => n.kind === 'anchor')
    expect(anchor).toBeTruthy()
    expect(anchor!.label).toContain('张卫国')
    expect(anchor!.sub).toContain('2020-03-24')
    expect(anchor!.sub).toContain('莫干山路111号')

    for (const d of ['space', 'time', 'relation']) {
      const node = g.nodes.find((n) => n.id === `dim:${d}`)
      expect(node, `${d} 节点必须存在`).toBeTruthy()
      const edge = g.edges.find((e) => e.source === 'anchor' && e.target === `dim:${d}`)
      expect(edge, `${d} 必须有连向中心的边`).toBeTruthy()
    }
  })

  it('R-1：date 档画虚线，minute 档画实线——精度在图上可分辨', () => {
    const g = buildConvergenceGraph(
      item({
        dimensions: {
          space: dim(true, { precision: 'date', count: 12, weight: 0.6 }),
          time: dim(true, { precision: 'minute', count: 3, weight: 1.6 }),
          relation: dim(false),
        },
      } as any),
    )
    const dateEdge = g.edges.find((e) => e.target === 'dim:space')!
    const minuteEdge = g.edges.find((e) => e.target === 'dim:time')!

    expect(dateEdge.dashed).toBe(true)
    expect(minuteEdge.dashed).toBe(false)
    // 条数多但权重低：线宽不得反而更粗
    expect(dateEdge.width).toBeLessThan(minuteEdge.width)
    expect(dateEdge.color).not.toBe(minuteEdge.color)
  })

  it('R-2：重名待裁决的锚点带标记，不混同普通锚点', () => {
    const g = buildConvergenceGraph(
      item({ person_ambiguous: true, ambiguous_candidates: ['person_a', 'person_b'] }),
    )
    const anchor = g.nodes.find((n) => n.kind === 'anchor')!
    expect(anchor.ambiguous).toBe(true)
    expect(anchor.label).toContain('?')
  })

  it('未命中维度仍出节点并写明原因，不由其他维度盖过去', () => {
    const g = buildConvergenceGraph(item())
    const t = g.nodes.find((n) => n.id === 'dim:time')!
    expect(t.label).toContain('未命中')
    expect(t.missReason).toBeTruthy()
    expect(t.missReason).not.toBe('')
  })

  it('支撑观察挂到所属维度下，并可顺 obs_id 回档案', () => {
    const detail = {
      available: true,
      convergence: item(),
      support: {
        space: [
          {
            observation_id: 'obs_1',
            skill_id: 'geo_accompany',
            lens_name: '时空伴随',
            title: '反复同框',
            subject: '张卫国',
            basis: 'b',
            falsification: 'f',
            degraded: false,
            degraded_reason: '',
            facts: [],
            facts_total: 0,
          },
        ],
        time: [],
        relation: [],
      },
    } as unknown as ConvergenceDetailResult

    const g = buildConvergenceGraph(item(), detail)
    const sup = g.nodes.find((n) => n.id === 'sup:obs_1')!
    expect(sup.dim).toBe('space')
    expect(sup.observationId).toBe('obs_1')
    expect(g.edges.some((e) => e.source === 'dim:space' && e.target === 'sup:obs_1')).toBe(true)
  })

  it('同现主体单独一类且统一虚线，不构成接触结论', () => {
    const g = buildConvergenceGraph(item())
    const p = g.nodes.find((n) => n.kind === 'person')!
    expect(p.label).toBe('李志强')
    const e = g.edges.find((x) => x.target === `p:李志强`)!
    expect(e.dashed).toBe(true)
    expect(e.label).toBe('同现')
  })

  it('图例覆盖 date 档与同现，口径不散落在模板里', () => {
    const text = GRAPH_LEGEND.map((l) => l.text).join('|')
    expect(text).toContain('日期级')
    expect(text).toContain('同现主体')
  })

  it('节点颜色口径收敛在构图层：三维异色、未命中灰、重名锚点警示色', () => {
    const g = buildConvergenceGraph(item())
    const space = g.nodes.find((n) => n.id === 'dim:space')!
    const time = g.nodes.find((n) => n.id === 'dim:time')! // 该维未命中
    const relation = g.nodes.find((n) => n.id === 'dim:relation')!
    expect(space.color).not.toBe(time.color) // 命中维与未命中维可辨
    expect(relation.color).not.toBe(space.color) // 三维彼此可辨

    const amb = buildConvergenceGraph(item({ person_ambiguous: true }))
    const a0 = g.nodes.find((n) => n.kind === 'anchor')!
    const a1 = amb.nodes.find((n) => n.kind === 'anchor')!
    // R-2：重名锚点不可与普通锚点同色，否则图上无法区分
    expect(a1.color).not.toBe(a0.color)
    expect(a1.ambiguous).toBe(true)

    // 每个节点都必须带 color：否则渲染层兜底色，样式口径就漏出去了
    for (const n of g.nodes) expect(n.color, `${n.id} 必须有 color`).toBeTruthy()
  })
})
