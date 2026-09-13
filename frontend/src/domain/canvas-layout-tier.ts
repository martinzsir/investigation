// 画布「证据强度三层」纯函数（UX P3；不依赖 G6，可单测）。
//
// 背景：现有 RANK_X 排列是「流程视角」——按数据血缘从规则一路排到数据源。
// 研判视角回答的是另一个问题：「这个案子我信几分」？
// 三层带：
//   - Tier 1（已锁死）：系统事实 / 采纳的规则 / 采纳的假设 / 采纳的书证
//   - Tier 2（待核实）：实体 / 查询结果 / 建议项 / 普通明细
//   - Tier 3（推测 / 失效）：未采纳假设 / 备注 / stale 节点
//
// 设计纪律：
//  - 只读派生、不回写 doc；不修改节点 props；
//  - 横向仍按 RANK_X 分列（与流程视角同口径，便于在两种视角间「换轴不换位」）；
//  - 钉住节点保留其原 y，绕过重排（与 layoutNodes 同约定）；
//  - 跨 tier 边自然跨越带间隙，可视作「证据链跨越证据强度」——天然引导用户
//    追问「这条链下端是不是 Tier 3 推测、能不能换成 Tier 1 证据」。

import type { CanvasDoc, CanvasNode } from './canvas'
import { RANK_X, RANK_Y_GAP } from './canvas-layout'

export type Tier = 1 | 2 | 3

/** 三层标题（状态栏 + 摘要用） */
export const TIER_LABELS: Record<Tier, string> = {
  1: '已锁死',
  2: '待核实',
  3: '推测 / 失效',
}

/** Tier 3 节点在画布上的不透明度（让低置信节点自然后退） */
export const TIER_3_OPACITY = 0.55

/** 带内纵向间距（略小于 RANK_Y_GAP 让三带更紧凑） */
export const TIER_Y_GAP = Math.round(RANK_Y_GAP * 0.92)
/** 三带之间的留白（用于区隔带 + 容纳 tier 标签） */
export const TIER_BAND_GAP = 220

/**
 * 节点 → 所属证据强度层。
 *
 * Tier 3 优先（最不可信——放在最前面判断，把 stale / 推测快速分流）；
 * Tier 1 其次（adopted 与系统事实）；
 * Tier 2 兜底（明细类、待核实、查询结果等中置信节点）。
 */
export function tierOf(n: CanvasNode): Tier {
  // Tier 3：失效或纯推测
  if (n.stale === true) return 3
  // 已采纳覆盖 kind 猜测：被锁死的人工假设/建议项应属 Tier 1
  if (n.adopted === true) return 1
  // 未采纳的人工假设 / 备注：默认 Tier 3
  if (n.kind === 'hypothesis' || n.kind === 'note') return 3
  // 系统锚点（规则 / 事实 / 书证）：默认 Tier 1
  if (n.kind === 'rule' || n.kind === 'fact' || n.kind === 'evidence') return 1
  // 其余（实体 / 数据行 / 数据源 / 查询结果 / 建议项）：Tier 2
  return 2
}

export interface TierBandLayout {
  tier: Tier
  /** 该带底部 y 中心（用于放置带标题与边距） */
  baseY: number
  /** 该带的总高度（含留白） */
  height: number
  /** 该带节点数 */
  count: number
}

/**
 * 按 tier 重排节点 y：同列节点按 (tier, 槽位) 落槽，x 不动。
 * 钉住节点保留原 y，不参与重排（与 layoutNodes 同约定）。
 */
export function layoutByTier(doc: CanvasDoc): CanvasDoc {
  // 按 (tier, x) 分桶统计：每个桶的 slot 数决定该带该列占用高度
  const buckets = new Map<string, CanvasNode[]>()
  const pinnedByBucket = new Map<string, CanvasNode[]>()
  for (const n of doc.nodes) {
    const key = `${tierOf(n)}|${RANK_X[n.kind]}`
    if (n.pinned === true) {
      const list = pinnedByBucket.get(key) ?? []
      list.push(n)
      pinnedByBucket.set(key, list)
    } else {
      const list = buckets.get(key) ?? []
      list.push(n)
      buckets.set(key, list)
    }
  }

  // 计算每带高度 = 该带所有桶中最大槽数
  const heightByTier: Record<Tier, number> = { 1: 0, 2: 0, 3: 0 }
  const tierCols: Record<Tier, Record<number, number>> = { 1: {}, 2: {}, 3: {} }
  const allBuckets: Array<[string, CanvasNode[]]> = [
    ...buckets.entries(),
    ...pinnedByBucket.entries(),
  ]
  for (const tier of [1, 2, 3] as const) {
    for (const [key, list] of allBuckets) {
      const [t, xs] = key.split('|')
      if (Number(t) !== tier) continue
      const x = Number(xs)
      tierCols[tier][x] = (tierCols[tier][x] ?? 0) + list.length
    }
    const maxSlot = Math.max(0, ...Object.values(tierCols[tier]))
    heightByTier[tier] = Math.max(1, maxSlot) * TIER_Y_GAP
  }

  // 堆叠三带：tier 1 顶 / tier 2 中 / tier 3 底
  // 起点往上挪一格给 tier 1 留顶部余量
  const bands: TierBandLayout[] = []
  let cursor = -TIER_Y_GAP - TIER_BAND_GAP / 2
  for (const tier of [1, 2, 3] as const) {
    const height = heightByTier[tier]
    const baseY = cursor + height / 2
    bands.push({
      tier,
      baseY,
      height: height + TIER_BAND_GAP,
      count: 0,
    })
    cursor = baseY + height / 2 + TIER_BAND_GAP
  }
  for (const tier of [1, 2, 3] as const) {
    bands[tier - 1].count = doc.nodes.filter((n) => tierOf(n) === tier).length
  }

  // 落槽：先按 (tier, x, y, id) 排序后连续落 y 槽位
  const sortKey = (n: CanvasNode): [Tier, number, number, string] => [
    tierOf(n),
    RANK_X[n.kind],
    n.y,
    n.id,
  ]
  const sorted = [...buckets.values()]
    .flat()
    .sort((a, b) => {
      const ka = sortKey(a)
      const kb = sortKey(b)
      return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2] || (ka[3] < kb[3] ? -1 : 1)
    })

  const slotIdx = new Map<string, number>()
  const out: CanvasNode[] = [...doc.nodes]
  const idIndex = new Map(out.map((n, i) => [n.id, i]))
  for (const n of sorted) {
    const tier = tierOf(n)
    const x = RANK_X[n.kind]
    const key = `${tier}|${x}`
    const slot = slotIdx.get(key) ?? 0
    slotIdx.set(key, slot + 1)
    const band = bands[tier - 1]
    const bandTop = band.baseY - band.height / 2 + (TIER_BAND_GAP - TIER_Y_GAP) / 2
    const y = bandTop + slot * TIER_Y_GAP
    const idx = idIndex.get(n.id)
    if (idx !== undefined) out[idx] = { ...n, x, y }
  }
  // 钉住节点：保留原 y，只强制 x 落 RANK_X
  for (const list of pinnedByBucket.values()) {
    for (const n of list) {
      const idx = idIndex.get(n.id)
      if (idx !== undefined) out[idx] = { ...n, x: RANK_X[n.kind] }
    }
  }

  return { ...doc, nodes: out }
}

/** 各 tier 计数（状态栏摘要） */
export function tierCounts(doc: CanvasDoc): Record<Tier, number> {
  const out: Record<Tier, number> = { 1: 0, 2: 0, 3: 0 }
  for (const n of doc.nodes) out[tierOf(n)] += 1
  return out
}

/** 节点所属带的视觉参数：返回该节点在 tier 模式下应取的 y / opacity */
export interface TierStyleHint {
  opacity: number
  dimmed: boolean
}

export function tierStyleHint(n: CanvasNode): TierStyleHint {
  if (tierOf(n) === 3) return { opacity: TIER_3_OPACITY, dimmed: true }
  return { opacity: 1, dimmed: false }
}