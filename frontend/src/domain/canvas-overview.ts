// 画布「全局层聚合视图」纯函数（UX P2；不依赖 G6，可单测）。
//
// 背景：五列分层下一条完整链路横跨近 1200px，节点一多（几十张卡片 + 几十条边）
// 就是一屏糊团——既看不清全局结构，也没有「从哪一层下手」的入口。
// 聚合视图把 doc 压成「5 个列胶囊 + 主干边」：
//   - 列胶囊：该层有多少节点、覆盖哪几类、列内自环关系多少；
//   - 主干边：两列之间的关系条数，线宽 ∝ 条数（哪两层耦合最重一眼可见）。
// 它是「看全局 → 选一层 → 退出聚合去挖」的入口，不是另一份数据：
//   - 只读派生，不回写 doc、不发请求；
//   - 与 canvas-view 的投影/折叠互不侵入（聚合直接吃 doc 全量，绕过投影，
//     因为「全局」本来就该是最终展开后的全貌，不受简洁视图折叠影响）。

import type { CanvasDoc, NodeKind } from './canvas'

/** 列胶囊节点 id 前缀（不进 doc、不参与投影/交互/自动保存） */
export const OVERVIEW_NODE_PREFIX = 'ov:'
/** 主干边 id 前缀 */
export const OVERVIEW_LINK_PREFIX = 'ovl:'

export interface OverviewColumnDef {
  key: string
  label: string
  /** 列 x（与 canvas-layout RANK_X / 泳道列头同口径） */
  x: number
  kinds: readonly NodeKind[]
}

/** 五列定义（顺序即左→右层级顺序，主干边按此归一化方向） */
export const OVERVIEW_COLUMNS: readonly OverviewColumnDef[] = [
  { key: 'rule', label: '规则', x: 0, kinds: ['rule'] },
  { key: 'fact', label: '事实 · 待核实', x: 260, kinds: ['fact', 'verify_item'] },
  {
    key: 'object',
    label: '实体 · 研判',
    x: 480,
    kinds: ['object', 'evidence', 'hypothesis', 'note', 'function_result'],
  },
  { key: 'row', label: '数据行', x: 720, kinds: ['source_row'] },
  { key: 'file', label: '数据源', x: 960, kinds: ['source_file'] },
]

const KEY_BY_KIND = new Map<NodeKind, string>()
const ORDER_BY_KEY = new Map<string, number>()
for (const [idx, def] of OVERVIEW_COLUMNS.entries()) {
  ORDER_BY_KEY.set(def.key, idx)
  for (const kind of def.kinds) KEY_BY_KIND.set(kind, def.key)
}

/** 节点类型 → 列 key（未知类型返回 null：不进任何列） */
export function columnKeyOf(kind: NodeKind): string | null {
  return KEY_BY_KIND.get(kind) ?? null
}

const COLUMN_LAYER_KEYS: Record<string, NodeKind> = {
  object: 'object',
  row: 'source_row',
  file: 'source_file',
}

/**
 * 列 key → canvas-view 的明细层开关（点胶囊下钻时打开对应层）。
 * 骨干层（rule/fact）恒可见，返回 undefined。
 */
export function overviewColumnLayer(key: string): NodeKind | undefined {
  return COLUMN_LAYER_KEYS[key]
}

export function isOverviewNodeId(id: string): boolean {
  return id.startsWith(OVERVIEW_NODE_PREFIX)
}

/** 列胶囊 id → 列 key（非胶囊返回 null） */
export function overviewColumnKey(id: string): string | null {
  if (!isOverviewNodeId(id)) return null
  return id.slice(OVERVIEW_NODE_PREFIX.length)
}

export interface OverviewColumn {
  key: string
  label: string
  x: number
  /** 该列节点总数 */
  count: number
  /** 列内类型分布（降序：多的在前；副标题与 chip 取色用） */
  kinds: Array<{ kind: NodeKind; count: number }>
  /** 列内自环关系数（同列节点之间的关系，不参与主干边） */
  inner: number
}

export interface OverviewLink {
  id: string
  /** 左列 key（主干边按列序归一化：不再出现同对列的反向重边） */
  source: string
  target: string
  /** 两列之间的关系条数 */
  count: number
  /** 0..1（相对最粗主干；线宽映射用） */
  weight: number
}

export interface OverviewModel {
  columns: OverviewColumn[]
  links: OverviewLink[]
  totalNodes: number
  totalEdges: number
}

/** 主干边线宽区间（画布像素） */
export const OVERVIEW_LINK_MIN_WIDTH = 2
export const OVERVIEW_LINK_MAX_WIDTH = 9

/**
 * 主干边线宽 ∝ 边数（开方：中等权重的列对也要看得出来，不被最粗那条吃掉）。
 */
export function overviewLinkWidth(weight: number): number {
  const w = Number.isFinite(weight) ? Math.min(1, Math.max(0, weight)) : 0
  const raw =
    OVERVIEW_LINK_MIN_WIDTH +
    (OVERVIEW_LINK_MAX_WIDTH - OVERVIEW_LINK_MIN_WIDTH) * Math.sqrt(w)
  return Math.round(raw * 10) / 10
}

/** 证据边线宽区间（画布像素）：比普通系统边(1.4)粗，但不超过概览主干边 */
export const EVIDENCE_LINK_MIN_WIDTH = 1.6
export const EVIDENCE_LINK_MAX_WIDTH = 3.2

/**
 * 命中边线宽 ∝ 溯源行数（对数缩放）。
 *
 * 为什么是对数：证据行数从 1 到几千都有可能，线性映射会让 10 行的边细到
 * 看不见、而 1000 行的边粗到糊成一团。对数后每差一个数量级才明显变粗一档，
 * 既区分得出"证据扎实"与"孤证"，又不会视觉失控。
 *
 * 1 行 → 1.6（略粗于普通边，表示"有证据"）
 * 10 行 → 2.4
 * 100 行 → 3.2（封顶）
 */
export function evidenceLinkWidth(rows: number): number {
  const n = Number.isFinite(rows) ? Math.max(0, rows) : 0
  if (n <= 0) return EVIDENCE_LINK_MIN_WIDTH
  // log10(1)=0 → min；log10(100)=2 → max；中间线性插值
  const t = Math.min(1, Math.log10(n + 1) / 2)
  const raw =
    EVIDENCE_LINK_MIN_WIDTH +
    (EVIDENCE_LINK_MAX_WIDTH - EVIDENCE_LINK_MIN_WIDTH) * t
  return Math.round(raw * 10) / 10
}

/** 胶囊 chip 上的计数文本（圆底宽 26，超两位退化成 99+） */
export function overviewCountText(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return '0'
  return count > 99 ? '99+' : String(Math.floor(count))
}

/**
 * 聚合：列胶囊 + 主干边。
 * 空列不产出（一个「数据行 0」的胶囊是噪声，且会把 bbox 撑宽）。
 */
export function buildOverview(doc: CanvasDoc): OverviewModel {
  const countByKind = new Map<NodeKind, number>()
  for (const n of doc.nodes) {
    countByKind.set(n.kind, (countByKind.get(n.kind) ?? 0) + 1)
  }

  const columns: OverviewColumn[] = []
  for (const def of OVERVIEW_COLUMNS) {
    let count = 0
    const kinds: Array<{ kind: NodeKind; count: number }> = []
    for (const kind of def.kinds) {
      const c = countByKind.get(kind) ?? 0
      if (c <= 0) continue
      kinds.push({ kind, count: c })
      count += c
    }
    if (count === 0) continue
    kinds.sort(
      (a, b) => b.count - a.count || (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0),
    )
    columns.push({ key: def.key, label: def.label, x: def.x, count, kinds, inner: 0 })
  }

  const byKey = new Map(columns.map((c) => [c.key, c]))
  const kindById = new Map(doc.nodes.map((n) => [n.id, n.kind]))
  const linkMap = new Map<string, { source: string; target: string; count: number }>()
  let totalEdges = 0

  for (const e of doc.edges) {
    const ks = kindById.get(e.source)
    const kt = kindById.get(e.target)
    if (!ks || !kt) continue
    const a = columnKeyOf(ks)
    const b = columnKeyOf(kt)
    if (!a || !b) continue
    totalEdges++
    if (a === b) {
      const col = byKey.get(a)
      if (col) col.inner += 1
      continue
    }
    // 归一化到左→右：反向重边不再各画一条（两条 cubic 会完全重叠）
    const left = (ORDER_BY_KEY.get(a) ?? 0) <= (ORDER_BY_KEY.get(b) ?? 0) ? a : b
    const right = left === a ? b : a
    const id = `${OVERVIEW_LINK_PREFIX}${left}--${right}`
    const cur = linkMap.get(id)
    if (cur) cur.count += 1
    else linkMap.set(id, { source: left, target: right, count: 1 })
  }

  let maxCount = 1
  for (const v of linkMap.values()) maxCount = Math.max(maxCount, v.count)
  const links: OverviewLink[] = [...linkMap.entries()]
    .map(([id, v]) => ({
      id,
      source: v.source,
      target: v.target,
      count: v.count,
      weight: v.count / maxCount,
    }))
    .sort((a, b) => b.count - a.count || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))

  return {
    columns,
    links,
    totalNodes: doc.nodes.length,
    totalEdges,
  }
}
