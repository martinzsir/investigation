import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 知识图谱（server/app/routers/graph.py + graph_view.py 契约）。
// GET /cases/{cid}/graph?node_limit=&edge_limit=
//   节点按度数 top-N 采样（非全量）；未 BUILD/无实体表 → available:false 空结构（不 500）。

export interface GraphNode {
  /** id 域 "<object>:<pk>"，如 person:123 */
  id: string
  label: string
  type: string
  type_title: string
  /** 命中间名（因/内/反/死/生），用于节点着色 */
  jian: string[]
}

export interface GraphEdge {
  source: string
  target: string
  label: string
  type: string
}

export interface GraphDto {
  available: boolean
  truncated: { nodes: boolean; edges: boolean; dropped_edges: number }
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphQuery {
  nodeLimit?: number // 1–1000，默认 300
  edgeLimit?: number // 1–3000，默认 500
}

export const graphApi = {
  async get(caseId: string, query: GraphQuery = {}): Promise<GraphDto> {
    const params = new URLSearchParams()
    if (query.nodeLimit) params.set('node_limit', String(query.nodeLimit))
    if (query.edgeLimit) params.set('edge_limit', String(query.edgeLimit))
    const qs = params.toString()
    const res = await api.get<GraphDto>(
      `/cases/${encodeURIComponent(caseId)}/graph${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
