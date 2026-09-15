// RC-201 分层布局纯函数（不依赖 G6，可单测；与服务端种子分列同口径）。
//
// 规则：
//  - 左→右按 kind 分列（与 server/app/canvas_seed.py _apply_positions 一致）：
//    规则 0 / 事实·待核实 260 / 实体·书证·假设·备注·查询结果 480 /
//    数据行 720 / 数据源 960；
//  - 钉住节点坐标原样保留，且其纵向槽位被保留，重排节点不会与它重叠；
//  - 未钉住节点列内按 barycenter 交叉最小化位序落槽（Sugiyama 第②步），
//    初始位序沿用 (y, 度数, id) 确定性规则，经中位数双向迭代优化；

import type { CanvasEdge, CanvasNode, NodeKind } from './canvas'
import { minimizeCrossings } from './canvas-layout-cross'

export const RANK_X: Record<NodeKind, number> = {
  rule: 0,
  fact: 260,
  verify_item: 260,
  object: 480,
  evidence: 480,
  hypothesis: 480,
  note: 480,
  function_result: 480,
  source_row: 720,
  source_file: 960,
}

export const RANK_Y_GAP = 104
/** 钉住节点纵向保留半距（新槽位中心距钉住节点小于此值则顺延） */
const PIN_CLEARANCE = RANK_Y_GAP / 2

function nextFreeY(slot: number, pinnedYs: readonly number[]): number {
  let y = slot * RANK_Y_GAP
  const collides = () =>
    pinnedYs.some((py) => Math.abs(py - y) < PIN_CLEARANCE)
  while (collides()) {
    y += RANK_Y_GAP
  }
  return y
}

/**
 * 输出重排后的新节点数组（不改入参）。
 *
 * @param nodes     当前全部节点
 * @param edges     全部边（参与交叉最小化的拓扑输入）
 * @param pinnedIds 钉住节点 id 集合——坐标不变且占位
 */
export function layoutNodes(
  nodes: readonly CanvasNode[],
  edges: readonly CanvasEdge[],
  pinnedIds: ReadonlySet<string>,
): CanvasNode[] {
  // barycenter 交叉最小化：对全图跑一次，拿到各列最优位序
  const rankMap = minimizeCrossings(nodes, edges, pinnedIds)
  const pinnedYsByRank = new Map<number, number[]>()
  for (const n of nodes) {
    if (!pinnedIds.has(n.id)) continue
    const x = RANK_X[n.kind]
    const ys = pinnedYsByRank.get(x) ?? []
    ys.push(n.y)
    pinnedYsByRank.set(x, ys)
  }

  const byId = new Map<string, CanvasNode>()
  // 槽位按「列」分配而非按 kind：object/evidence/hypothesis/note/
  // function_result 共用 480 列，分列各算各的会导致同列重叠。
  const unpinnedByX = new Map<number, CanvasNode[]>()
  for (const n of nodes) {
    if (pinnedIds.has(n.id)) continue
    const x = RANK_X[n.kind]
    const group = unpinnedByX.get(x) ?? []
    group.push(n)
    unpinnedByX.set(x, group)
  }
  const byRankOrder: NodeKind[] = Object.keys(RANK_X) as NodeKind[]
  const columns = [...new Set(byRankOrder.map((k) => RANK_X[k]))]
  for (const rankX of columns) {
    const pinnedYs = pinnedYsByRank.get(rankX) ?? []
    ;(unpinnedByX.get(rankX) ?? [])
      .slice()
      .sort(
        (a, b) =>
          (rankMap.get(a.id) ?? 0) - (rankMap.get(b.id) ?? 0) ||
          (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
      )
      .forEach((n, idx) => {
        byId.set(n.id, { ...n, x: rankX, y: nextFreeY(idx, pinnedYs) })
      })
  }
  // 钉住节点坐标原样
  for (const n of nodes) {
    if (pinnedIds.has(n.id)) byId.set(n.id, { ...n })
  }
  // 输出保持入参顺序（G6 按 id 索引，顺序变化也安全，但保留顺序更可预期）
  return nodes.map((n) => byId.get(n.id) ?? { ...n })
}
