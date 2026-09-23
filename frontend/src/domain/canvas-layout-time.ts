/**
 * 时间轴布局（P2-③）：把画布从「关系图」切成「研判过程时间线」。
 *
 * 口径说明（重要）
 * --------------
 * 本视图排的是 **研判过程时间**（什么时候产生的这条核查/书证/节点），
 * 不是 **业务事件发生时间**（那笔转账发生在哪天）。
 *
 * 为什么是过程时间：业务时间字段因领域而异（中标公示日/交易日期/就诊时间…），
 * 本体层目前**没有声明**哪个属性是时间字段，硬编码字段名不通用。
 * 而节点自带的 created_at / updated_at / props.uploaded_at 是底座统一
 * 写入的，跨领域一致、零本体改造即可用。
 *
 * 若将来需要业务事件时间轴：应在 objects.json 的属性上加语义标记
 * （如 semantic: "event_time"），由装载器下发、画布读取——那是另一个改造。
 */
import { RANK_Y_GAP } from './canvas-layout'
import type { CanvasDoc, CanvasEdge, CanvasNode } from './canvas'

/** 时间轴横向间距（像素/档；同档节点纵向堆叠） */
export const TIME_X_GAP = 210
/** 时间轴起点（首档左侧留白） */
export const TIME_X_START = 40

/** 轨道带高度（event 口径下聚集簇/碰撞窗节点渲染成的横带） */
export const BAND_HEIGHT = 28
/** 同泳道相邻带允许贴边；不同泳道竖直间距 */
export const BAND_LANE_GAP = 4
/** 顶部区间轨道的上下内边距 */
const BAND_TRACK_PAD_TOP = 8
const BAND_TRACK_PAD_BOTTOM = 10
/**
 * 轨道带最多泳道数。实测 demoW：358 天切 8 档（~45 天/档），单档最多 6 个簇。
 * 泳道过多会把点节点挤出版面，超量的带复用右缘最早空出的泳道（少量重叠可接受，
 * 点击/抽屉与边连接不受影响）。
 */
const BAND_MAX_LANES = 4
/** 轨道带最小宽度（单日簇也要可点可读） */
const BAND_MIN_WIDTH = 96
/** 轨道带相对所占档宽的两侧内缩（相邻档的带不贴死） */
const BAND_GAP = 22

/** laneCount → 顶部区间轨道总高度（点节点从该高度以下开始堆叠） */
export function bandTrackHeight(laneCount: number): number {
  if (laneCount <= 0) return 0
  return (
    BAND_TRACK_PAD_TOP +
    laneCount * BAND_HEIGHT +
    Math.max(0, laneCount - 1) * BAND_LANE_GAP +
    BAND_TRACK_PAD_BOTTOM
  )
}

/** 时间轴口径：process=研判过程时间 / event=业务发生时间 */
export type TimeMode = 'process' | 'event'

/** 区间节点种类（与后端 canvas_seed 的 props.interval_kind 对齐） */
export type IntervalBandKind = 'burst' | 'collision_window'

/**
 * 取节点的业务时间区间（无 start/end 或种类不对返回 null）。
 * 区间节点（function_result + interval_kind）只在 event 口径下成带；
 * process 口径把它们当普通节点（无过程时间戳 → 归「无时间」档）。
 */
export function intervalRange(
  n: CanvasNode,
): { start: number; end: number; kind: IntervalBandKind } | null {
  const props = (n.props ?? {}) as Record<string, unknown>
  const kind = props.interval_kind
  if (kind !== 'burst' && kind !== 'collision_window') return null
  const s = Date.parse(String(props.start ?? ''))
  const endRaw = props.end ?? props.start
  const e = Date.parse(String(endRaw ?? ''))
  if (!Number.isFinite(s) || !Number.isFinite(e)) return null
  return { start: s, end: Math.max(e, s), kind }
}

/**
 * 取节点时间戳（毫秒）。返回 null = 该节点无该口径下的时间信息。
 *
 * event 口径：读 props.event_time（后端从行数据按本体 semantic:event_time
 *   声明提取的业务发生时间）。仅 source_row 节点有此字段——业务时间只对
 *   数据行有意义，规则/事实节点本身没有"发生在哪天"。
 * process 口径：书证上传时间 → 创建时间 → 更新时间。书证取 uploaded_at
 *   是因为它才是"这份材料什么时候进入本案"的真实时刻，created_at 可能是补录时间。
 */
export function nodeTimestamp(
  n: CanvasNode,
  mode: TimeMode = 'process',
): number | null {
  const props = (n.props ?? {}) as Record<string, unknown>
  const raw =
    mode === 'event'
      ? props.event_time
      : (props.uploaded_at ?? props.uploadedAt ?? props.created_at ?? n.created_at ?? n.updated_at)
  if (typeof raw !== 'string' && typeof raw !== 'number') return null
  const t = typeof raw === 'number' ? raw : Date.parse(String(raw))
  return Number.isFinite(t) ? t : null
}

export interface TimeAxisModel {
  /** 有时间戳的节点数 */
  timed: number
  /** 无时间戳的节点数（会归入"无时间"档） */
  untimed: number
  /** 最早/最晚时间戳（无时间节点时均为 null） */
  from: number | null
  to: number | null
  /** 分档数（含"无时间"档） */
  buckets: number
}

/**
 * 统计时间轴分布（状态栏摘要用）。
 * event 口径下区间节点的结束日也参与 from/to——否则尾部只在末端日结束
 * 的轨道带会伸出时间轴范围。
 */
export function buildTimeAxis(
  doc: CanvasDoc,
  mode: TimeMode = 'process',
): TimeAxisModel {
  let timed = 0
  let untimed = 0
  let from: number | null = null
  let to: number | null = null
  for (const n of doc.nodes) {
    const t = nodeTimestamp(n, mode)
    const range = mode === 'event' ? intervalRange(n) : null
    if (t === null && !range) {
      untimed += 1
      continue
    }
    timed += 1
    const lo = range ? Math.min(t ?? range.start, range.start) : (t as number)
    const hi = range ? Math.max(t ?? range.end, range.end) : (t as number)
    if (from === null || lo < from) from = lo
    if (to === null || hi > to) to = hi
  }
  return {
    timed,
    untimed,
    from,
    to,
    buckets: timed > 0 ? _bucketCount(from, to) + (untimed > 0 ? 1 : 0) : 0,
  }
}

/**
 * 分档数：把时间跨度切成若干档，避免每个节点独占一列（那就散成一条长线）。
 * 上限 8 档——超过 8 档人眼已经无法横向比较。
 */
function _bucketCount(from: number | null, to: number | null): number {
  if (from === null || to === null || to <= from) return 1
  const span = to - from
  const DAY = 86_400_000
  if (span <= DAY) return Math.min(8, 2)
  if (span <= 7 * DAY) return Math.min(8, Math.ceil(span / DAY))
  if (span <= 90 * DAY) return Math.min(8, Math.ceil(span / (7 * DAY)))
  return 8
}

/** 时间戳 → 档下标（与 layoutByTime / layoutTimeBands 同口径） */
function _bucketIndex(
  t: number,
  from: number,
  bucketMs: number,
  count: number,
): number {
  if (bucketMs <= 0) return 0
  return Math.min(count - 1, Math.max(0, Math.floor((t - from) / bucketMs)))
}

/** 档下标 → 该档中心 x */
function _bucketCenterX(idx: number): number {
  return TIME_X_START + idx * TIME_X_GAP
}

export interface TimeBandGeometry {
  kind: IntervalBandKind
  /** 轨道带中心 x（= 起止两档中心的中点） */
  cx: number
  /** 轨道带顶边 y（按所在泳道定位） */
  y: number
  width: number
  height: number
  /** 分配到的泳道（0 起） */
  lane: number
}

/**
 * 全文档区间节点 → 轨道带几何（doc 级：泳道分配必须看全部分带）。
 *
 * 宽 = 起档到止档覆盖的档宽之和（两侧内缩），单日区间取最小宽度；
 * 泳道按开始位置排序贪心分配：与某泳道最后一条带不重叠就进该泳道，
 * 否则开新泳道（上限 BAND_MAX_LANES，超量复用右缘最早的泳道）。
 * 布局（x/y）与渲染（宽/高）共用本函数，保证「排的位置」与
 * 「画的宽度」严格同源。轴无效返回空 Map。
 */
export function layoutTimeBands(
  doc: CanvasDoc,
  axis: TimeAxisModel | null,
): Map<string, TimeBandGeometry> {
  const out = new Map<string, TimeBandGeometry>()
  if (!axis || axis.from === null || axis.to === null) return out
  const count = _bucketCount(axis.from, axis.to)
  const bucketMs =
    axis.to > axis.from ? (axis.to - axis.from) / Math.max(1, count) : 0

  interface BandItem {
    id: string
    kind: IntervalBandKind
    cx: number
    width: number
    /** 带左右缘 x（泳道重叠判定用） */
    lo: number
    hi: number
  }
  const items: BandItem[] = []
  for (const n of doc.nodes) {
    const range = intervalRange(n)
    if (!range) continue
    const i0 = _bucketIndex(range.start, axis.from, bucketMs, count)
    const i1 = _bucketIndex(range.end, axis.from, bucketMs, count)
    const span = Math.max(0, i1 - i0)
    const cx = (_bucketCenterX(i0) + _bucketCenterX(i1)) / 2
    const width = Math.max(BAND_MIN_WIDTH, (span + 1) * TIME_X_GAP - BAND_GAP)
    items.push({
      id: n.id, kind: range.kind, cx, width,
      lo: cx - width / 2, hi: cx + width / 2,
    })
  }
  if (items.length === 0) return out
  items.sort((a, b) => a.lo - b.lo || a.hi - b.hi || a.id.localeCompare(b.id))

  /** 各泳道最后一条带的右缘 */
  const laneEnd: number[] = []
  for (const it of items) {
    let lane = laneEnd.findIndex((end) => end <= it.lo)
    if (lane < 0) {
      lane =
        laneEnd.length < BAND_MAX_LANES
          ? laneEnd.length
          : laneEnd.indexOf(Math.min(...laneEnd))
    }
    laneEnd[lane] = it.hi
    out.set(it.id, {
      kind: it.kind,
      cx: it.cx,
      y: BAND_TRACK_PAD_TOP + lane * (BAND_HEIGHT + BAND_LANE_GAP),
      width: it.width,
      height: BAND_HEIGHT,
      lane,
    })
  }
  return out
}

/** 轨道带实际占用的泳道数（无带为 0） */
export function bandLaneCount(bands: Map<string, TimeBandGeometry>): number {
  let max = -1
  for (const g of bands.values()) max = Math.max(max, g.lane)
  return max + 1
}

/**
 * 按时间重排节点坐标。
 *
 * - 有时间戳的节点：按时间分档落到对应 x，档内按时间先后纵向堆叠；
 * - 无时间戳的节点：统一放最右"无时间"档（**不隐藏**——隐藏会让用户
 *   误以为数据少了，与画布规模保护同理：宁可显式归置，不静默丢弃）；
 * - event 口径下区间节点（聚集簇/碰撞窗）独占顶部「区间轨道」，渲染层
 *   据此把它们画成横跨起档→止档的轨道带（几何见 layoutTimeBands）；
 *   同档多带按泳道上下错开，点节点整体下移轨道实际高度避让；
 * - 钉住的节点坐标不动（与 layoutByTier 同口径）。
 */
export function layoutByTime(
  doc: CanvasDoc,
  mode: TimeMode = 'process',
): CanvasDoc {
  const axis = buildTimeAxis(doc, mode)
  if (axis.timed === 0) return doc // 全无时间 → 保持原布局，不做无意义重排

  const count = _bucketCount(axis.from, axis.to)
  const bucketMs =
    axis.from !== null && axis.to !== null && axis.to > axis.from
      ? (axis.to - axis.from) / Math.max(1, count)
      : 0

  // event 口径：区间节点成带（doc 级泳道分配）；process 口径不产生带
  const bandMap = mode === 'event' ? layoutTimeBands(doc, axis) : null
  const bandIds = new Set(bandMap?.keys() ?? [])

  const timed: Array<{ node: CanvasNode; t: number }> = []
  const untimed: CanvasNode[] = []
  for (const n of doc.nodes) {
    // 区间仅在 event 口径成带；process 口径下它们没有过程时间戳，归无时间档
    if (bandIds.has(n.id)) continue
    const t = nodeTimestamp(n, mode)
    if (t === null) untimed.push(n)
    else timed.push({ node: n, t })
  }
  timed.sort((a, b) => a.t - b.t || a.node.id.localeCompare(b.node.id))

  // 档内纵向计数
  const yCounter = new Map<number, number>()
  const xByIndex = (idx: number) => TIME_X_START + idx * TIME_X_GAP
  // 点节点从区间轨道实际高度下方开始排
  const topPad = bandMap
    ? bandTrackHeight(bandLaneCount(bandMap))
    : 0

  const out = doc.nodes.map((n) => ({ ...n }))
  const byId = new Map(out.map((n) => [n.id, n]))

  // 区间轨道带：x=跨度中心、y=泳道中心（G6 节点 (x,y) 是包围盒中心，
  // 而几何里的 y 是顶边——必须下移半个带高，否则首泳道贴顶被裁）
  if (bandMap) {
    for (const [id, g] of bandMap) {
      const target = byId.get(id)
      if (!target || target.pinned === true) continue
      target.x = g.cx
      target.y = g.y + BAND_HEIGHT / 2
    }
  }

  for (const { node, t } of timed) {
    const target = byId.get(node.id)
    if (!target || target.pinned === true) continue
    const idx =
      axis.from !== null
        ? _bucketIndex(t, axis.from, bucketMs, count)
        : 0
    const y = yCounter.get(idx) ?? 0
    yCounter.set(idx, y + 1)
    target.x = xByIndex(idx)
    target.y = topPad + y * RANK_Y_GAP
  }

  // ---- 无时间节点归置：两种口径理由不同，处置也不同 ----
  const untimedIdx = axis.timed > 0 ? count : 0
  if (mode === 'event' && untimed.length > 0) {
    // event 口径：无 event_time 是**本质如此**——rule/fact/hypothesis 这些
    // 研判产物没有"发生在哪天"，不是数据缺失。全塞进一个「无时间」档会在
    // 最右堆成一列长柱（实测可上百节点）。按 kind 分列摊到时间轴右侧：
    // 同类一列、纵向堆叠，既摊薄高度，也保留了「这些不是业务事件」的语义。
    //
    // 为什么不让它们回流程坐标：流程列 x（0/260/480/720）与时间轴分档
    // x（40/250/460…）大量重合，两坐标系会叠在一起互相遮挡。
    const byKind = new Map<string, CanvasNode[]>()
    for (const n of untimed) {
      const k = String(n.kind || 'other')
      const bucket = byKind.get(k)
      if (bucket) bucket.push(n)
      else byKind.set(k, [n])
    }
    let gi = 0
    for (const k of [...byKind.keys()].sort()) {
      const col = untimedIdx + gi
      let uy = 0
      for (const n of byKind.get(k) as CanvasNode[]) {
        const target = byId.get(n.id)
        if (!target || target.pinned === true) continue
        target.x = xByIndex(col)
        target.y = topPad + uy * RANK_Y_GAP
        uy += 1
      }
      gi += 1
    }
  } else {
    // process 口径：无过程时间戳才是真的缺失，保留单列「无时间」档显式归置
    let uy = 0
    for (const n of untimed) {
      const target = byId.get(n.id)
      if (!target || target.pinned === true) continue
      target.x = xByIndex(untimedIdx)
      target.y = topPad + uy * RANK_Y_GAP
      uy += 1
    }
  }

  return { ...doc, nodes: out }
}

/** 时间轴状态栏摘要文本（无有效时间轴时返回 null） */
export function timeAxisSummary(axis: TimeAxisModel,
                                mode: TimeMode = 'process'): string | null {
  if (axis.timed === 0) return null
  const fmt = (t: number | null) =>
    t === null
      ? '—'
      : new Date(t).toLocaleDateString('zh-CN', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
        })
  const base = `${fmt(axis.from)} → ${fmt(axis.to)}`
  if (axis.untimed === 0) return base
  // 口径要说实话：event 口径下它们是"不该在业务时间轴上"，不是"缺时间"
  return mode === 'event'
    ? `${base}（${axis.untimed} 个节点非业务事件，按类型列于右侧）`
    : `${base}（${axis.untimed} 个节点无时间）`
}


