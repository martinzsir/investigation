import { describe, expect, it } from 'vitest'
import {
  layoutNodes,
  RANK_X,
  RANK_Y_GAP,
} from '../src/domain/canvas-layout'
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
