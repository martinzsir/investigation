import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 知识图谱（server/app/routers/graph.py + graph_view.py 契约）。
// GET /cases/{cid}/graph?node_limit=&edge_limit=&edge_kinds=&clue_id=
//   节点按度数 top-N 采样（非全量）；未 BUILD/无实体表 → available:false 空结构（不 500）。
//   edge_kinds：同一取数的 link 白名单投影（资金链路图=transfers）。
//   被线索 evidence_refs 引用的节点/边带 hit/clue_ids 高亮。

export interface GraphNode {
  /** id 域 "<object>:<pk>"，如 person:123 */
  id: string
  label: string
  type: string
  type_title: string
  /** 命中间名（因/内/反/死/生），用于节点着色 */
  jian: string[]
  /** 被线索 evidence_refs 直接引用 */
  hit: boolean
  /** 命中该节点的线索 ID（可溯源） */
  clue_ids: string[]
}

export interface GraphEdgeProps {
  amount?: string
  date?: string
}

export interface GraphEdge {
  source: string
  target: string
  label: string
  type: string
  /** 被线索 evidence_refs 引用 */
  hit: boolean
  clue_ids: string[]
  /** build_sql 实际输出中的附加属性（amount/date 白名单） */
  props: GraphEdgeProps
}

export interface EdgeKind {
  name: string
  title: string
  /** 案件语义层是否存在该边表 */
  present: boolean
}

export interface GraphHighlights {
  clue_count: number
  nodes: number
  edges: number
}

export interface GraphDto {
  available: boolean
  truncated: { nodes: boolean; edges: boolean; dropped_edges: number }
  edge_kinds: EdgeKind[]
  highlights: GraphHighlights
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphQuery {
  nodeLimit?: number // 1–1000，默认 300
  edgeLimit?: number // 1–3000，默认 500
  edgeKinds?: string[] // link 名白名单；缺省全部
  clueId?: string // 仅高亮该线索；缺省全部可见线索
}

export const graphApi = {
  async get(caseId: string, query: GraphQuery = {}): Promise<GraphDto> {
    const params = new URLSearchParams()
    if (query.nodeLimit) params.set('node_limit', String(query.nodeLimit))
    if (query.edgeLimit) params.set('edge_limit', String(query.edgeLimit))
    if (query.edgeKinds?.length) params.set('edge_kinds', query.edgeKinds.join(','))
    if (query.clueId) params.set('clue_id', query.clueId)
    const qs = params.toString()
    const res = await api.get<GraphDto>(
      `/cases/${encodeURIComponent(caseId)}/graph${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
