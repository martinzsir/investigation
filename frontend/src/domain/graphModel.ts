// 知识图谱（FE-P-010）纯函数：采样横幅文案、度数排序、降级关系表格派生。
// G6 动态 import 失败或 available:false 时降级关系表格（禁空白）。
import type { GraphDto, GraphEdge, GraphNode } from '../api/endpoints/graph'

/** 区间常量（与后端 Query 约束一致） */
export const NODE_LIMIT = { def: 300, min: 1, max: 1000 }
export const EDGE_LIMIT = { def: 500, min: 1, max: 3000 }

export function clampNodeLimit(n: number): number {
  if (!Number.isFinite(n)) return NODE_LIMIT.def
  return Math.min(NODE_LIMIT.max, Math.max(NODE_LIMIT.min, Math.floor(n)))
}

export function clampEdgeLimit(n: number): number {
  if (!Number.isFinite(n)) return EDGE_LIMIT.def
  return Math.min(EDGE_LIMIT.max, Math.max(EDGE_LIMIT.min, Math.floor(n)))
}

/** 有图可画：available 且至少一个节点 */
export function hasGraph(g: GraphDto | null | undefined): boolean {
  return !!g && g.available === true && g.nodes.length > 0
}

/** 截断横幅文案（按度数采样/丢边）；无截断返 '' */
export function truncatedBanner(g: GraphDto): string {
  const t = g.truncated
  const parts: string[] = []
  if (t.nodes) parts.push(`节点已按度数采样取前 ${g.nodes.length} 个`)
  if (t.edges) parts.push(`关系已截断，丢弃 ${t.dropped_edges} 条边`)
  return parts.join('；')
}

/** 节点 id → 节点映射（降级表格补 label 用） */
export function nodeMap(nodes: GraphNode[]): Map<string, GraphNode> {
  return new Map(nodes.map((n) => [n.id, n]))
}

export interface RelationRow {
  source: string
  sourceLabel: string
  relation: string
  target: string
  targetLabel: string
  type: string
}

/** 降级关系表格：边 → 关系行；端点缺失补 id-only */
export function edgesToRows(edges: GraphEdge[], nodes: GraphNode[]): RelationRow[] {
  const m = nodeMap(nodes)
  const labelOf = (id: string) => m.get(id)?.label ?? id
  return edges.map((e) => ({
    source: e.source,
    sourceLabel: labelOf(e.source),
    relation: e.label || e.type,
    target: e.target,
    targetLabel: labelOf(e.target),
    type: e.type,
  }))
}

/** 节点按度数（边出现次数）降序，用于侧栏列表 */
export function nodesByDegree(g: GraphDto): { node: GraphNode; degree: number }[] {
  const deg = new Map<string, number>()
  for (const e of g.edges) {
    deg.set(e.source, (deg.get(e.source) ?? 0) + 1)
    deg.set(e.target, (deg.get(e.target) ?? 0) + 1)
  }
  return g.nodes
    .map((node) => ({ node, degree: deg.get(node.id) ?? 0 }))
    .sort((a, b) => b.degree - a.degree || a.node.label.localeCompare(b.node.label))
}

/** 节点点击下钻路由：按 type 跳线索列表过滤（无实体详情页） */
export function nodeDrillHref(node: GraphNode): string {
  return `/c/clues?jian=${encodeURIComponent(node.type_title || node.type)}&q=${encodeURIComponent(node.label)}`
}
