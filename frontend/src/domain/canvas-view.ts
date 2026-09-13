// 研判画布「渲染层视图投影」纯函数（UX P0；取代 canvas-collapse.ts）。
//
// 纪律：
//  - doc 仍是唯一权威（自动保存/快照/回滚语义不变），本模块只读派生，
//    决定「此刻渲染哪些节点」，绝不回写、不发请求；
//  - 明细三类 object/source_row/source_file 默认随事实分组折叠；
//    多事实共享的明细取生产者并集（所有归属事实都折叠才隐藏）；
//  - 骨干节点（rule/fact/verify_item/evidence/hypothesis/note/function_result）
//    与人工节点恒可见；
//  - 副标题只允许使用 doc 既有 props 与组内确定序号（RC-104：禁止字段明文）。

import type { CanvasDoc, CanvasEdge, CanvasNode, NodeKind } from './canvas'

/** 明细节点三类（简洁视图默认折叠） */
export const DETAIL_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  'object',
  'source_row',
  'source_file',
])

export const DETAIL_LAYERS = ['object', 'source_row', 'source_file'] as const
export type DetailLayerKind = (typeof DETAIL_LAYERS)[number]

export type ViewMode = 'compact' | 'full'

export interface FactDetailGroup {
  factId: string
  objects: string[]
  rows: string[]
  files: string[]
  /** 三类明细总数（徽标/空组判定） */
  total: number
}

export interface DetailModel {
  groups: FactDetailGroup[]
  groupByFact: Map<string, FactDetailGroup>
  /** 明细节点 → 生产者事实 id 并集（从事实不可达的明细没有生产者） */
  producers: Map<string, Set<string>>
  /** 系统边有向邻接（含 links.title 类系统边，如「中标参与」） */
  adjacency: Map<string, Array<{ to: string; edge: CanvasEdge }>>
}

function isDetailKind(kind: NodeKind): boolean {
  return DETAIL_KINDS.has(kind)
}

function pushUnique(arr: string[], id: string): void {
  if (!arr.includes(id)) arr.push(id)
}

/**
 * 沿系统边从每个 fact 做有界 BFS（只进入明细三类节点），
 * 得到 {objects,rows,files} 分组并构造生产者并集。
 */
export function buildDetailModel(doc: CanvasDoc): DetailModel {
  const nodeById = new Map(doc.nodes.map((n) => [n.id, n]))
  const adjacency = new Map<string, Array<{ to: string; edge: CanvasEdge }>>()
  for (const edge of doc.edges) {
    if (edge.system !== true) continue
    const list = adjacency.get(edge.source) ?? []
    list.push({ to: edge.target, edge })
    adjacency.set(edge.source, list)
  }

  const groups: FactDetailGroup[] = []
  const groupByFact = new Map<string, FactDetailGroup>()
  const producers = new Map<string, Set<string>>()

  const tagProducer = (id: string, factId: string): void => {
    const set = producers.get(id) ?? new Set<string>()
    set.add(factId)
    producers.set(id, set)
  }

  for (const fact of doc.nodes) {
    if (fact.kind !== 'fact') continue
    const group: FactDetailGroup = {
      factId: fact.id,
      objects: [],
      rows: [],
      files: [],
      total: 0,
    }
    const queue: string[] = [fact.id]
    const seen = new Set<string>([fact.id])
    while (queue.length > 0) {
      const cur = queue.shift() as string
      for (const { to } of adjacency.get(cur) ?? []) {
        if (seen.has(to)) continue
        const target = nodeById.get(to)
        if (!target || !isDetailKind(target.kind)) continue
        seen.add(to)
        if (target.kind === 'object') {
          pushUnique(group.objects, to)
        } else if (target.kind === 'source_row') {
          pushUnique(group.rows, to)
        } else {
          pushUnique(group.files, to)
        }
        tagProducer(to, fact.id)
        queue.push(to)
      }
    }
    group.total =
      group.objects.length + group.rows.length + group.files.length
    groups.push(group)
    groupByFact.set(fact.id, group)
  }

  return { groups, groupByFact, producers, adjacency }
}

/**
 * 从一个非 fact 展开根（object/row）沿系统边向下游收集明细节点
 * （静态 doc 已有的部分；尚未懒加载的节点等 expand 回流后自然并入）。
 */
function reachableDetails(
  model: DetailModel,
  nodeById: Map<string, CanvasNode>,
  rootId: string,
): Set<string> {
  const out = new Set<string>()
  const queue = [rootId]
  const seen = new Set<string>([rootId])
  while (queue.length > 0) {
    const cur = queue.shift() as string
    for (const { to } of model.adjacency.get(cur) ?? []) {
      if (seen.has(to)) continue
      const target = nodeById.get(to)
      if (!target || !isDetailKind(target.kind)) continue
      seen.add(to)
      out.add(to)
      queue.push(to)
    }
  }
  return out
}

export interface ViewOptions {
  mode: ViewMode
  /** 已展开的根节点 id（fact / object / source_row） */
  expandedRoots: ReadonlySet<string>
  /** 简洁视图下三层独立强制显示开关 */
  layers?: Partial<Record<DetailLayerKind, boolean>>
}

export interface ViewProjection {
  visibleNodeIds: Set<string>
  visibleEdgeIds: Set<string>
}

/**
 * 视图投影：完整视图=全量；简洁视图=骨干 + 满足以下任一条件的明细：
 *  ① 其生产者事实中至少一个已展开；
 *  ② 被某个已展开的非事实根（object/row）静态可达；
 *  ③ 对应层开关强制打开。
 * 边随两端可见性过滤（不产生悬挂边）。
 */
export function projectView(
  doc: CanvasDoc,
  model: DetailModel,
  opts: ViewOptions,
): ViewProjection {
  const visibleNodeIds = new Set<string>()
  if (opts.mode === 'full') {
    for (const n of doc.nodes) visibleNodeIds.add(n.id)
  } else {
    const nodeById = new Map(doc.nodes.map((n) => [n.id, n]))
    const layerOn = opts.layers ?? {}
    for (const n of doc.nodes) {
      if (!isDetailKind(n.kind)) {
        visibleNodeIds.add(n.id)
        continue
      }
      if (layerOn[n.kind as DetailLayerKind] === true) {
        visibleNodeIds.add(n.id)
        continue
      }
      const facts = model.producers.get(n.id)
      if (facts) {
        for (const factId of facts) {
          if (opts.expandedRoots.has(factId)) {
            visibleNodeIds.add(n.id)
            break
          }
        }
      }
      if (visibleNodeIds.has(n.id)) continue
      for (const rootId of opts.expandedRoots) {
        const root = nodeById.get(rootId)
        if (!root || root.kind === 'fact') continue
        if (reachableDetails(model, nodeById, rootId).has(n.id)) {
          visibleNodeIds.add(n.id)
          break
        }
      }
    }
  }

  const visibleEdgeIds = new Set<string>()
  for (const e of doc.edges) {
    if (visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target)) {
      visibleEdgeIds.add(e.id)
    }
  }
  return { visibleNodeIds, visibleEdgeIds }
}

// ----------------------------------------------------------------------
// 行序号（同数据源内按节点 id 确定序；副标题「数据源 · 行 N」）
// ----------------------------------------------------------------------
export interface RowOrdinal {
  source: string
  ordinal: number
}

export function buildRowOrdinals(doc: CanvasDoc): Map<string, RowOrdinal> {
  const bySource = new Map<string, string[]>()
  for (const n of doc.nodes) {
    if (n.kind !== 'source_row') continue
    const source = typeof n.props?.source === 'string' ? n.props.source : '未登记数据源'
    const list = bySource.get(source) ?? []
    list.push(n.id)
    bySource.set(source, list)
  }
  const out = new Map<string, RowOrdinal>()
  for (const [source, ids] of bySource) {
    ids.sort()
    ids.forEach((id, idx) => out.set(id, { source, ordinal: idx + 1 }))
  }
  return out
}

export type RowBadge = 'missing' | 'unregistered' | 'table-summary'

/** 数据行状态徽标（只用 doc props，不含字段明文） */
export function rowBadges(n: CanvasNode): RowBadge[] {
  const out: RowBadge[] = []
  if (n.props?.missing === true) out.push('missing')
  if (n.props?.registered === false) out.push('unregistered')
  if (n.props?.granularity === '表级汇总') out.push('table-summary')
  return out
}

// ----------------------------------------------------------------------
// 副标题（RC-104：只用 doc 既有业务 props 与组内确定序号）
// ----------------------------------------------------------------------
export interface SubtitleContext {
  model: DetailModel
  ordinals: ReadonlyMap<string, RowOrdinal>
  /** 规则节点 id → 维度名（ruleAudit.dimension，懒取，缺省不拼） */
  dimensions?: ReadonlyMap<string, string>
}

function clip(s: unknown, max = 24): string {
  const v = typeof s === 'string' ? s.trim() : ''
  return v.length > max ? `${v.slice(0, max)}…` : v
}

export function nodeSubtitle(n: CanvasNode, ctx: SubtitleContext): string {
  switch (n.kind) {
    case 'rule':
      return ctx.dimensions?.get(n.id) ?? ''
    case 'fact': {
      const g = ctx.model.groupByFact.get(n.id)
      if (!g) return ''
      const parts: string[] = []
      if (g.rows.length > 0) parts.push(`${g.rows.length} 条来源行`)
      if (g.objects.length > 0) parts.push(`${g.objects.length} 个实体`)
      return parts.join(' · ')
    }
    case 'object':
      return clip(n.props?.type_title, 20)
    case 'source_row': {
      const o = ctx.ordinals.get(n.id)
      if (!o) return ''
      return `${o.source} · 行 ${o.ordinal}`
    }
    case 'source_file':
      return n.props?.registered === false ? '未登记数据源' : '已登记数据源'
    case 'verify_item':
      return clip(n.props?.status, 12)
    case 'evidence':
      return clip(n.props?.material_type, 20)
    case 'hypothesis':
      return clip(n.props?.content, 26)
    case 'note':
      return clip(n.props?.content, 26)
    case 'function_result':
      return clip(n.props?.function_title, 24)
    default:
      return ''
  }
}

// ----------------------------------------------------------------------
// focus chain（BFS n 跳上下游；系统边+人工边都参与）
//
// 跳数（hops）存在的理由：五列分层下「规则→事实→实体→数据行→数据源」一条链
// 横向跨度近 1200px，固定一跳既看不全链路、又因为全部边照画而糊成一团。
// 层级跳数让「看多远」成为可控游标，卡片/边按跳数分级渲染。
// ----------------------------------------------------------------------
export interface FocusChain {
  nodeIds: Set<string>
  edgeIds: Set<string>
  /** 节点 id → 跳数（0 = 焦点本身） */
  depthByNode: Map<string, number>
  /** 边 id → 跳数（归属首次触达的那一跳，保证唯一） */
  depthByEdge: Map<string, number>
}

/** 跳数上限：超过 3 跳整图基本都进链，焦点失去意义 */
export const FOCUS_HOPS_MAX = 3

export function focusChain(
  doc: CanvasDoc,
  nodeId: string | null,
  hops: number = 1,
): FocusChain | null {
  if (!nodeId) return null
  const ids = new Set(doc.nodes.map((n) => n.id))
  if (!ids.has(nodeId)) return null
  // NaN/Infinity 一律回落一跳（Math.floor(NaN) 仍为 NaN，会让循环一次都不进）
  const raw = Number.isFinite(hops) ? Math.floor(hops) : 1
  const limit = Math.max(1, Math.min(FOCUS_HOPS_MAX, raw))

  // 邻接表（无向：溯源既看上游也看下游）
  const incident = new Map<string, Array<{ edgeId: string; other: string }>>()
  const push = (id: string, edgeId: string, other: string) => {
    const list = incident.get(id)
    if (list) list.push({ edgeId, other })
    else incident.set(id, [{ edgeId, other }])
  }
  for (const e of doc.edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue
    push(e.source, e.id, e.target)
    push(e.target, e.id, e.source)
  }

  const depthByNode = new Map<string, number>([[nodeId, 0]])
  const depthByEdge = new Map<string, number>()
  const edgeIds = new Set<string>()
  let frontier = [nodeId]
  for (let d = 1; d <= limit; d++) {
    const next: string[] = []
    for (const id of frontier) {
      for (const { edgeId, other } of incident.get(id) ?? []) {
        if (depthByEdge.has(edgeId)) continue
        depthByEdge.set(edgeId, d)
        edgeIds.add(edgeId)
        if (!depthByNode.has(other)) {
          depthByNode.set(other, d)
          next.push(other)
        }
      }
    }
    if (next.length === 0) break
    frontier = next
  }
  return { nodeIds: new Set(depthByNode.keys()), edgeIds, depthByNode, depthByEdge }
}

// ----------------------------------------------------------------------
// 传递节点折叠（P1）：把链路上「只是过路」的中间节点压成一条合成边
//
// 五列分层下「规则→事实→实体→数据行→数据源」一条链要横跨 1200px；
// 中间大量节点度数为 2 —— 既不是焦点也不是分叉，只是把链拉长。
// 折叠后 5 段链变 1 段，边上标注「经 N 个中间节点」，点边可展开该段。
// ----------------------------------------------------------------------
export const SYNTHETIC_EDGE_PREFIX = 'syn:'

export interface SyntheticEdge {
  id: string
  source: string
  target: string
  /** 被折叠掉的中间节点（按链路顺序，source → target） */
  via: string[]
  /** 两端节点较远的那一跳 */
  depth: number
}

export interface CollapsedChain {
  /** 保留在画布上的节点（焦点/分叉/端点） */
  visibleNodeIds: Set<string>
  /** 被折叠掉的中间节点 */
  collapsedNodeIds: Set<string>
  syntheticEdges: SyntheticEdge[]
}

export interface CollapseOptions {
  /** 强制保留的节点（用户点开过的段、钉住节点等） */
  keep?: ReadonlySet<string>
  /** 单次折叠允许吃掉的最大中间节点数（防止一整段链被吞掉后失去上下文） */
  maxVia?: number
}

const DEFAULT_MAX_VIA = 4

export function collapseChain(
  doc: CanvasDoc,
  chain: FocusChain,
  opts: CollapseOptions = {},
): CollapsedChain {
  const maxVia = opts.maxVia ?? DEFAULT_MAX_VIA
  const keep = opts.keep ?? new Set<string>()
  const nodeById = new Map(doc.nodes.map((n) => [n.id, n]))

  // 链内邻接（无向；两端都必须在链上）
  const adj = new Map<string, Set<string>>()
  const addEdge = (a: string, b: string) => {
    const s = adj.get(a)
    if (s) s.add(b)
    else adj.set(a, new Set([b]))
  }
  for (const e of doc.edges) {
    if (!chain.edgeIds.has(e.id)) continue
    if (!chain.nodeIds.has(e.source) || !chain.nodeIds.has(e.target)) continue
    addEdge(e.source, e.target)
    addEdge(e.target, e.source)
  }

  const depthOf = (id: string): number => chain.depthByNode.get(id) ?? 1
  /** 可折叠：非焦点、链内度=2、系统节点、非钉住、未被强制保留 */
  const collapsible = (id: string): boolean => {
    if (depthOf(id) === 0) return false
    if (keep.has(id)) return false
    const n = nodeById.get(id)
    if (!n) return false
    if (n.system !== true) return false
    if (n.pinned === true) return false
    return (adj.get(id)?.size ?? 0) === 2
  }

  const collapsedNodeIds = new Set<string>()
  const syntheticEdges: SyntheticEdge[] = []
  const visited = new Set<string>()

  const walk = (start: string, first: string): { path: string[]; end: string | null } => {
    const path: string[] = []
    let cur: string = start
    let next: string | null = first
    while (next && collapsible(next) && !visited.has(next)) {
      path.push(next)
      const rest: string[] = [...(adj.get(next) ?? [])].filter((n) => n !== cur)
      if (rest.length !== 1) {
        cur = next
        next = null
        break
      }
      const nn: string = rest[0]
      cur = next
      next = nn
    }
    return { path, end: next }
  }

  for (const id of chain.nodeIds) {
    if (!collapsible(id) || visited.has(id)) continue
    const neighbors = [...(adj.get(id) ?? [])]
    if (neighbors.length !== 2) continue
    const [a, b] = neighbors
    const segA = walk(id, a)
    const segB = walk(id, b)
    const via = [...segA.path.slice().reverse(), id, ...segB.path]
    if (via.length > maxVia) continue
    const endA = segA.end ?? segA.path[segA.path.length - 1] ?? null
    const endB = segB.end ?? segB.path[segB.path.length - 1] ?? null
    if (!endA || !endB || endA === endB) continue
    for (const v of via) {
      visited.add(v)
      collapsedNodeIds.add(v)
    }
    syntheticEdges.push({
      id: `${SYNTHETIC_EDGE_PREFIX}${endA}--${endB}`,
      source: endA,
      target: endB,
      via: [...via],
      depth: Math.max(depthOf(endA), depthOf(endB)),
    })
  }

  const visibleNodeIds = new Set<string>()
  for (const id of chain.nodeIds) {
    if (!collapsedNodeIds.has(id)) visibleNodeIds.add(id)
  }
  return { visibleNodeIds, collapsedNodeIds, syntheticEdges }
}

// ----------------------------------------------------------------------
// 可展开判定（徽标 +/− 是否出现）
// ----------------------------------------------------------------------
export const TOGGLEABLE_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  'fact',
  'object',
  'source_row',
])

/**
 * 节点是否显示 +/− 徽标（存在可展开的明细下一层；空展开由后端 leaf 兜底）：
 *  - fact：组内已有明细，或实体层尚未懒加载（fact 恒给入口）；
 *  - object：邻居/来源行可能尚未加载；
 *  - source_row：所属文件可能尚未加载。
 */
export function isToggleable(n: CanvasNode): boolean {
  return TOGGLEABLE_KINDS.has(n.kind)
}

/** 泳道伪节点 id 前缀（不参与 findNode/交互/投影） */
export const LANE_PREFIX = 'lane:'

export function isLaneId(id: string): boolean {
  return id.startsWith(LANE_PREFIX)
}
