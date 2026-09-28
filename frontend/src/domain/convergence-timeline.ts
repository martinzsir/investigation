/**
 * 三维交汇时间轴构图层（纯函数，不依赖 vue-timeline-chart / DOM）。
 *
 * 为什么单独抽一层
 * ----------------
 * 时间轴库（vue-timeline-chart）的 items/groups 结构与业务语义耦合很紧，
 * 若直接在组件里转，口径改一次要动模板。抽成纯函数可脱离 DOM 单测，
 * 且将来换渲染器（手写 SVG）可直接复用。
 *
 * 时间轴看什么
 * ------------
 * **不是**"事件流水"，而是"哪些日期段三维同时密集"。故每条锚点在它命中
 * 的每个维度泳道上各出一个 item，而**同一人同一天**若三个维度都命中，
 * 就构成一个**簇（cluster）——这才是交汇在时间轴上的表达**，须高亮。
 *
 * 两条红线（与地图、G6 图、列表面板同口径）
 * ------------------------------------------
 * 1. item 必须带该维度的 precision：日期级（date）只能说明"前后一天"，
 *    不得画成与时刻级一样的实心精确点。
 * 2. 不做定性。簇只表示"这一天三维都有证据"，不说"可疑"。
 */
import {
  DIM_LABEL,
  DIM_ORDER,
  type ConvergenceItem,
} from '../api/endpoints/convergence'
// 维度配色复用证据图口径——单一真相源，避免两处图例颜色不一致
import { DIM_COLOR } from './convergence-graph'

/** 泳道上的一个点 = 某锚点在某一维度上的命中 */
export interface ConvTimelineItem {
  key: string
  date: string
  /** 日期零点的毫秒时间戳（锚点只到日期，无时刻） */
  ts: number
  personLabel: string
  address: string
  /** 该维度命中条数——**不是强度**，须与 precision 同看 */
  count: number
  precision: string
  weight: number
  ambiguous: boolean
  dimHit: number
}

export interface ConvTimelineLane {
  dim: string
  label: string
  color: string
  items: ConvTimelineItem[]
}

/** 同一主体同一日的交汇簇：三维齐全 = 真交汇 */
export interface ConvTimelineCluster {
  date: string
  ts: number
  personKey: string
  personLabel: string
  dims: string[]
  convKeys: string[]
  addresses: string[]
  /** 三维全部命中 */
  full: boolean
  /** 木桶：该簇内最低精度档 */
  minPrecision: string
  ambiguous: boolean
}

export interface ConvTimelineModel {
  lanes: ConvTimelineLane[]
  clusters: ConvTimelineCluster[]
  domainStart: number
  domainEnd: number
  /** 无日期的锚点数（不应静默丢弃） */
  undated: number
  note: string
}

const PRECISION_RANK: Record<string, number> = {
  unknown: 0, date: 1, hour: 2, minute: 3, second: 4,
}

function _weaker(a: string, b: string): string {
  return (PRECISION_RANK[a] ?? 0) <= (PRECISION_RANK[b] ?? 0) ? a : b
}

function _ts(date: string): number | null {
  if (!date) return null
  const t = new Date(`${date}T00:00:00Z`).getTime()
  return Number.isFinite(t) ? t : null
}

function _shortAddr(list: string[] | undefined): string {
  const a = (list || []).map((s) => {
    const seg = String(s || '').split('/')
    return seg[seg.length - 1] || s
  })
  return a.join('、') || '—'
}

export function buildConvergenceTimeline(
  items: ConvergenceItem[],
): ConvTimelineModel {
  const lanes: ConvTimelineLane[] = DIM_ORDER.map((dim) => ({
    dim,
    label: DIM_LABEL[dim] ?? dim,
    color: DIM_COLOR[dim] ?? '#999999',
    items: [],
  }))
  const laneByDim = new Map(lanes.map((l) => [l.dim, l]))

  // 簇：按（主体 + 日期）归集
  const clusterMap = new Map<string, ConvTimelineCluster>()
  let undated = 0
  let minTs = Infinity
  let maxTs = -Infinity

  for (const it of items) {
    const personLabel = (it.person_names || []).join('/') || it.person_key || '—'
    const address = _shortAddr(it.std_addresses)
    const ts = _ts(it.date)
    if (ts === null) {
      undated += 1
      continue
    }
    minTs = Math.min(minTs, ts)
    maxTs = Math.max(maxTs, ts)

    for (const dim of DIM_ORDER) {
      const d = it.dimensions?.[dim]
      if (!d?.hit) continue
      laneByDim.get(dim)?.items.push({
        key: it.key,
        date: it.date,
        ts,
        personLabel,
        address,
        count: d.count || 0,
        precision: d.precision || 'unknown',
        weight: d.weight || 0,
        ambiguous: !!it.person_ambiguous,
        dimHit: it.dim_hit_count || 0,
      })
    }

    const ckey = `${it.person_key || personLabel}|${it.date}`
    let c = clusterMap.get(ckey)
    if (!c) {
      c = {
        date: it.date,
        ts,
        personKey: it.person_key || '',
        personLabel,
        dims: [],
        convKeys: [],
        addresses: [],
        full: false,
        minPrecision: 'second',
        ambiguous: false,
      }
      clusterMap.set(ckey, c)
    }
    if (!c.convKeys.includes(it.key)) c.convKeys.push(it.key)
    if (!c.addresses.includes(address)) c.addresses.push(address)
    c.ambiguous = c.ambiguous || !!it.person_ambiguous
    for (const dim of DIM_ORDER) {
      const d = it.dimensions?.[dim]
      if (!d?.hit) continue
      if (!c.dims.includes(dim)) c.dims.push(dim)
      c.minPrecision = _weaker(c.minPrecision, d.precision || 'unknown')
    }
  }

  for (const l of lanes) l.items.sort((a, b) => a.ts - b.ts)
  const clusters = [...clusterMap.values()]
  for (const c of clusters) c.full = c.dims.length === DIM_ORDER.length
  // 三维齐全优先，再按时间
  clusters.sort((a, b) => (b.full === a.full ? a.ts - b.ts
    : b.full ? 1 : -1))

  return {
    lanes,
    clusters,
    domainStart: Number.isFinite(minTs) ? minTs : 0,
    domainEnd: Number.isFinite(maxTs) ? maxTs : 0,
    undated,
    note:
      '每条锚点在命中的维度泳道上各出一点；虚点 = 日期级（仅知前后一天，同地异时），' +
      '实点 = 时刻级（可判时间窗重叠）。金色高亮列 = 同一主体同一日三维齐全（真交汇）。' +
      '簇只陈述"这天三维都有证据"，不作定性。',
  }
}
