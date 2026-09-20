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
import type { CanvasDoc, CanvasNode } from './canvas'

/** 时间轴横向间距（像素/档；同档节点纵向堆叠） */
export const TIME_X_GAP = 210
/** 时间轴起点（首档左侧留白） */
export const TIME_X_START = 40

/** 时间轴口径：process=研判过程时间 / event=业务发生时间 */
export type TimeMode = 'process' | 'event'

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
      : (props.uploaded_at ?? props.uploadedAt ?? n.created_at ?? n.updated_at)
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
    if (t === null) {
      untimed += 1
      continue
    }
    timed += 1
    if (from === null || t < from) from = t
    if (to === null || t > to) to = t
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

/**
 * 按时间重排节点坐标。
 *
 * - 有时间戳的节点：按时间分档落到对应 x，档内按时间先后纵向堆叠；
 * - 无时间戳的节点：统一放最右"无时间"档（**不隐藏**——隐藏会让用户
 *   误以为数据少了，与画布规模保护同理：宁可显式归置，不静默丢弃）；
 * - 钉住的节点坐标不动（与 layoutByTier 同口径）。
 */
export function layoutByTime(
  doc: CanvasDoc,
  mode: TimeMode = 'process',
): CanvasDoc {
  const axis = buildTimeAxis(doc, mode)
  if (axis.timed === 0) return doc // 全无时间 → 保持原布局，不做无意义重排

  const bucketMs =
    axis.from !== null && axis.to !== null && axis.to > axis.from
      ? (axis.to - axis.from) / Math.max(1, _bucketCount(axis.from, axis.to))
      : 0

  const timed: Array<{ node: CanvasNode; t: number }> = []
  const untimed: CanvasNode[] = []
  for (const n of doc.nodes) {
    const t = nodeTimestamp(n, mode)
    if (t === null) untimed.push(n)
    else timed.push({ node: n, t })
  }
  timed.sort((a, b) => a.t - b.t || a.node.id.localeCompare(b.node.id))

  // 档内纵向计数
  const yCounter = new Map<number, number>()
  const xByIndex = (idx: number) => TIME_X_START + idx * TIME_X_GAP

  const out = doc.nodes.map((n) => ({ ...n }))
  const byId = new Map(out.map((n) => [n.id, n]))

  for (const { node, t } of timed) {
    const target = byId.get(node.id)
    if (!target || target.pinned === true) continue
    const idx =
      bucketMs > 0 && axis.from !== null
        ? Math.min(
            _bucketCount(axis.from, axis.to) - 1,
            Math.floor((t - axis.from) / bucketMs),
          )
        : 0
    const y = yCounter.get(idx) ?? 0
    yCounter.set(idx, y + 1)
    target.x = xByIndex(idx)
    target.y = y * RANK_Y_GAP
  }

  // 无时间档放最右
  const untimedIdx = axis.timed > 0 ? _bucketCount(axis.from, axis.to) : 0
  let uy = 0
  for (const n of untimed) {
    const target = byId.get(n.id)
    if (!target || target.pinned === true) continue
    target.x = xByIndex(untimedIdx)
    target.y = uy * RANK_Y_GAP
    uy += 1
  }

  return { ...doc, nodes: out }
}

/** 时间轴状态栏摘要文本（无有效时间轴时返回 null） */
export function timeAxisSummary(axis: TimeAxisModel): string | null {
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
  return axis.untimed > 0 ? `${base}（${axis.untimed} 个节点无时间）` : base
}
