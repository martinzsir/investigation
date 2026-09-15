import { describe, expect, it } from 'vitest'
import {
  layoutNodes,
  RANK_X,
  RANK_Y_GAP,
} from '../src/domain/canvas-layout'
import {
  minimizeCrossings,
  countCrossings,
} from '../src/domain/canvas-layout-cross'
import type { CanvasEdge, CanvasNode } from '../src/domain/canvas'

// RC-201 分层布局：列 x 与种子/后端同口径；钉住节点坐标原样保留并占位；
// 未钉住节点按槽位重排，不与钉住节点重叠。

function node(
  id: string,
  kind: CanvasNode['kind'],
  x: number,
  y: number,
  pinned = false,
): CanvasNode {
  return { id, kind, ref: id, label: id, system: true, adopted: false,
    stale: false, pinned, x, y, props: {} }
}

describe('RC-201 layoutNodes', () => {
  it('places unpinned nodes on rank columns with 104 vertical gap', () => {
    const nodes = [
      node('r1', 'rule', 5, 777),
      node('f1', 'fact', 99, -33),
      node('f2', 'fact', 8, 400),
      node('row1', 'source_row', 700, 9),
    ]
    const out = layoutNodes(nodes, [], new Set())
    const byId = new Map(out.map((n) => [n.id, n]))
    expect(byId.get('r1')!.x).toBe(RANK_X.rule)
    expect(byId.get('r1')!.y).toBe(0)
    expect(byId.get('f1')!.x).toBe(RANK_X.fact)
    expect(byId.get('f2')!.x).toBe(RANK_X.fact)
    expect(byId.get('row1')!.x).toBe(RANK_X.source_row)
    // 同列按原 y 排序落槽，间距 104
    expect(byId.get('f1')!.y).toBe(0)
    expect(byId.get('f2')!.y).toBe(RANK_Y_GAP)
    expect(byId.get('row1')!.y).toBe(0)
  })

  it('keeps pinned nodes exactly where they are', () => {
    const pinned = node('h1', 'hypothesis', 1234, 567, true)
    const nodes = [pinned, node('h2', 'hypothesis', 480, 0)]
    const out = layoutNodes(nodes, [], new Set(['h1']))
    const byId = new Map(out.map((n) => [n.id, n]))
    expect(byId.get('h1')!.x).toBe(1234)
    expect(byId.get('h1')!.y).toBe(567)
    expect(byId.get('h1')!.pinned).toBe(true)
    // 未钉住节点仍归列
    expect(byId.get('h2')!.x).toBe(RANK_X.hypothesis)
  })

  it('skips slots that would collide (within half-gap) with a pinned node', () => {
    // 钉住在列 x=260 的 y=100：槽位 0(y=0) 距离 100 ≥52 可用；
    // 槽位 1(y=104) 距离 4 <52，顺延到 y=208
    const pinned = node('f1', 'fact', 260, 100, true)
    const unpinnedA = node('f2', 'fact', 260, 0)
    const unpinnedB = node('f3', 'fact', 260, 260)
    const out = layoutNodes(
      [pinned, unpinnedA, unpinnedB], [], new Set(['f1']))
    const byId = new Map(out.map((n) => [n.id, n]))
    expect(byId.get('f2')!.y).toBe(0)
    expect(byId.get('f3')!.y).toBe(208)
  })

  it('does not mutate inputs and preserves output order', () => {
    const nodes = [node('f1', 'fact', 0, 0), node('r1', 'rule', 0, 0)]
    const snapshot = JSON.stringify(nodes)
    const out = layoutNodes(nodes, [], new Set())
    expect(JSON.stringify(nodes)).toBe(snapshot)
    expect(out.map((n) => n.id)).toEqual(['f1', 'r1'])
  })

  it('nodes with more incident edges rank earlier within a column', () => {
    // 同 y 起点时按连边度数降序：f2 有两条边，应占槽 0
    const f1 = node('f1', 'fact', 260, 0)
    const f2 = node('f2', 'fact', 260, 0)
    const h = node('h1', 'hypothesis', 480, 0)
    const edges: CanvasEdge[] = [
      { id: 'e1', source: 'f2', target: 'h', rel: '推断为', system: false },
      { id: 'e2', source: 'f2', target: 'f1', rel: 'x', system: false },
    ]
    const out = layoutNodes([f1, f2, h], edges, new Set())
    const byId = new Map(out.map((n) => [n.id, n]))
    expect(byId.get('f2')!.y).toBe(0)
    expect(byId.get('f1')!.y).toBe(RANK_Y_GAP)
  })

  it('manual kinds share the 480 column with object/evidence', () => {
    const nodes = [
      node('h', 'hypothesis', 0, 0),
      node('n', 'note', 0, 0),
      node('o', 'object', 0, 0),
    ]
    const out = layoutNodes(nodes, [], new Set())
    expect(out.map((n) => n.x)).toEqual([
      RANK_X.hypothesis,
      RANK_X.note,
      RANK_X.object,
    ])
    // 同列三节点落 0/104/208
    expect(out.map((n) => n.y).sort((a, b) => a - b)).toEqual([0, 104, 208])
  })
})

// --- barycenter 交叉最小化（L-TC-01 ~ L-TC-04）---

/** 简单 LCG 伪随机数（确定性，复刻 POC 的随机命中模式） */
function lcgNext(state: { s: number }): number {
  state.s = (state.s * 1103515245 + 12345) & 0x7fffffff
  return state.s
}

function seededShuffle(arr: number[], seed: number): number[] {
  const result = [...arr]
  const st = { s: seed }
  for (let i = result.length - 1; i > 0; i--) {
    const j = lcgNext(st) % (i + 1)
    ;[result[i], result[j]] = [result[j], result[i]]
  }
  return result
}

/** 构造典型交叉图：1 rule → N fact → M source_row → 1 source_file */
function buildCrossCase(nFact = 4, nRow = 5): { nodes: CanvasNode[]; edges: CanvasEdge[] } {
  const nodes: CanvasNode[] = [node('R1', 'rule', 0, 0)]
  const edges: CanvasEdge[] = []
  for (let i = 0; i < nFact; i++) {
    nodes.push(node(`F${i}`, 'fact', 260, i * 100))
    edges.push({ id: `eR${i}`, source: 'R1', target: `F${i}`, rel: '命中', system: true })
  }
  for (let j = 0; j < nRow; j++) {
    nodes.push(node(`S${j}`, 'source_row', 720, j * 100))
    edges.push({ id: `eS${j}`, source: `S${j}`, target: 'SF1', rel: '所属文件', system: true })
  }
  nodes.push(node('SF1', 'source_file', 960, 0))
  // fact → source_row 命中边（确定性伪随机 shuffle，模拟真实命中关系）
  let rows = Array.from({ length: nRow }, (_, i) => i)
  for (let i = 0; i < nFact; i++) {
    rows = seededShuffle(rows, 7)
    for (const j of rows.slice(0, 2)) {
      edges.push({ id: `eF${i}S${j}`, source: `F${i}`, target: `S${j}`, rel: '来源行', system: true })
    }
  }
  return { nodes, edges }
}

describe('barycenter 交叉最小化', () => {
  it('L-TC-01: 交叉数显著下降（≥60%）', () => {
    const { nodes, edges } = buildCrossCase(8, 10)
    // 现状排序（无 barycenter）：按 (y, -deg, id) 落槽
    const naivePos = new Map<string, [number, number]>()
    for (const n of nodes) {
      naivePos.set(n.id, [RANK_X[n.kind], n.y])
    }
    const naiveCross = countCrossings(nodes, edges, naivePos)

    // barycenter 优化后
    const rankMap = minimizeCrossings(nodes, edges, new Set())
    const bcPos = new Map<string, [number, number]>()
    // 按 rank 重建 y 坐标
    const byCol = new Map<number, string[]>()
    for (const n of nodes) {
      const x = RANK_X[n.kind]
      if (!byCol.has(x)) byCol.set(x, [])
      byCol.get(x)!.push(n.id)
    }
    for (const [x, ids] of byCol) {
      ids.sort((a, b) => (rankMap.get(a) ?? 0) - (rankMap.get(b) ?? 0))
      ids.forEach((id, i) => bcPos.set(id, [x, i * RANK_Y_GAP]))
    }
    const bcCross = countCrossings(nodes, edges, bcPos)

    expect(naiveCross).toBeGreaterThan(0)
    const reduction = naiveCross > 0 ? (naiveCross - bcCross) / naiveCross : 0
    expect(reduction).toBeGreaterThanOrEqual(0.6)
  })

  it('L-TC-02: 确定性——同输入两次结果完全一致', () => {
    const { nodes, edges } = buildCrossCase(6, 8)
    const a = minimizeCrossings(nodes, edges, new Set())
    const b = minimizeCrossings(nodes, edges, new Set())
    expect(a).toEqual(b)
  })

  it('L-TC-03: 钉住节点坐标不变且不被覆盖', () => {
    const { nodes, edges } = buildCrossCase(4, 5)
    // 将 F0 钉住在 (260, 500)
    const pinnedNodes = nodes.map((n) =>
      n.id === 'F0' ? { ...n, pinned: true, x: 260, y: 500 } : n,
    )
    const pinnedSet = new Set(['F0'])
    const out = layoutNodes(pinnedNodes, edges, pinnedSet)
    const f0 = out.find((n) => n.id === 'F0')!
    expect(f0.x).toBe(260)
    expect(f0.y).toBe(500)
    // 其他节点不与钉住节点重叠
    const sameCol = out.filter((n) => n.x === 260 && n.id !== 'F0')
    for (const n of sameCol) {
      expect(Math.abs(n.y - 500)).toBeGreaterThanOrEqual(RANK_Y_GAP / 2)
    }
  })

  it('L-TC-04: 收敛——iterations=4 与 =16 交叉数一致', () => {
    const { nodes, edges } = buildCrossCase(8, 10)
    // barycenter 可能存在多个等价最优排列（交叉数相同），故验证交叉数收敛
    const buildPos = (rank: Map<string, number>) => {
      const byCol = new Map<number, string[]>()
      for (const n of nodes) {
        const x = RANK_X[n.kind]
        if (!byCol.has(x)) byCol.set(x, [])
        byCol.get(x)!.push(n.id)
      }
      const pos = new Map<string, [number, number]>()
      for (const [x, ids] of byCol) {
        ids.sort((a, b) => (rank.get(a) ?? 0) - (rank.get(b) ?? 0))
        ids.forEach((id, i) => pos.set(id, [x, i * RANK_Y_GAP]))
      }
      return pos
    }
    const r4 = minimizeCrossings(nodes, edges, new Set(), 4)
    const r16 = minimizeCrossings(nodes, edges, new Set(), 16)
    const c4 = countCrossings(nodes, edges, buildPos(r4))
    const c16 = countCrossings(nodes, edges, buildPos(r16))
    expect(c4).toBe(c16)
  })
})
