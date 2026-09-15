// Sugiyama 第②步：跨列交叉最小化（barycenter 中位数迭代）。
//
// 纯函数、确定性：同输入必同输出，可进快照测试。
// 与 canvas-layout.ts 同口径：按 RANK_X 分列，初始位序沿用 (y, -deg, id) 规则。
//
// 算法：对每一列，按「相邻列中邻居节点的位序中位数」重排本列，
// 双向扫描若干轮（奇数轮正向、偶数轮反向）。
// 参考：Geng & Wismath 的 median heuristic。

import type { CanvasEdge, CanvasNode } from './canvas'
import { RANK_X } from './canvas-layout'

const DEFAULT_ITERATIONS = 4

/** 取数组中位数（偶数取中间两数均值）。 */
function median(values: number[]): number | null {
  if (values.length === 0) return null
  const s = [...values].sort((a, b) => a - b)
  const mid = s.length >> 1
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2
}

/**
 * 计算跨列长边交叉数（诊断/测试用）。
 * 两端 x 不同的边按端点 y 序做逆序对计数。
 */
export function countCrossings(
  nodes: readonly CanvasNode[],
  edges: readonly CanvasEdge[],
  pos: ReadonlyMap<string, [number, number]>,
): number {
  const kind = new Map(nodes.map((n) => [n.id, n.kind]))
  const longEdges = edges.filter(
    (e) =>
      kind.has(e.source) &&
      kind.has(e.target) &&
      RANK_X[kind.get(e.source)!] !== RANK_X[kind.get(e.target)!],
  )

  // 按列对分组
  const pairs = new Map<string, [number, number][]>()
  for (const e of longEdges) {
    const xa = RANK_X[kind.get(e.source)!]
    const xb = RANK_X[kind.get(e.target)!]
    const key = `${Math.min(xa, xb)},${Math.max(xa, xb)}`
    const list = pairs.get(key) ?? []
    list.push(e)
    pairs.set(key, list)
  }

  let total = 0
  for (const es of pairs.values()) {
    const xa = Math.min(
      RANK_X[kind.get(es[0].source)!],
      RANK_X[kind.get(es[0].target)!],
    )
    const seq: [number, number][] = []
    for (const e of es) {
      const xaE = RANK_X[kind.get(e.source)!]
      const src = xaE === xa ? e.source : e.target
      const dst = xaE === xa ? e.target : e.source
      seq.push([pos.get(src)?.[1] ?? 0, pos.get(dst)?.[1] ?? 0])
    }
    seq.sort((a, b) => a[0] - b[0])
    const ys = seq.map((s) => s[1])
    for (let i = 0; i < ys.length; i++) {
      for (let j = i + 1; j < ys.length; j++) {
        if (ys[i] > ys[j]) total++
      }
    }
  }
  return total
}

/**
 * 最小化跨列长边交叉：按邻居位序中位数双向迭代重排各列。
 *
 * @returns 节点 id → 列内位序（0 基），供 layoutNodes 替换原有排序键
 */
export function minimizeCrossings(
  nodes: readonly CanvasNode[],
  edges: readonly CanvasEdge[],
  pinnedIds?: ReadonlySet<string>,
  iterations: number = DEFAULT_ITERATIONS,
): Map<string, number> {
  const kindOf = new Map(nodes.map((n) => [n.id, n.kind]))

  // 邻接表（无向）
  const adj = new Map<string, Set<string>>()
  for (const e of edges) {
    if (!adj.has(e.source)) adj.set(e.source, new Set())
    if (!adj.has(e.target)) adj.set(e.target, new Set())
    adj.get(e.source)!.add(e.target)
    adj.get(e.target)!.add(e.source)
  }

  // 按 RANK_X 分列
  const columns = new Map<number, string[]>()
  for (const n of nodes) {
    const x = RANK_X[n.kind]
    if (!columns.has(x)) columns.set(x, [])
    columns.get(x)!.push(n.id)
  }
  const xs = [...columns.keys()].sort((a, b) => a - b)

  // 度数
  const deg = new Map<string, number>()
  for (const e of edges) {
    deg.set(e.source, (deg.get(e.source) ?? 0) + 1)
    deg.set(e.target, (deg.get(e.target) ?? 0) + 1)
  }

  // 初始位序沿用确定性规则 (y, -deg, id)，保证与现状同起点
  const yOf = new Map(nodes.map((n) => [n.id, n.y]))
  const rank = new Map<string, number>()
  for (const x of xs) {
    const ordered = [...columns.get(x)!].sort((a, b) => {
      const ya = yOf.get(a) ?? 0
      const yb = yOf.get(b) ?? 0
      return (
        ya - yb ||
        (deg.get(b) ?? 0) - (deg.get(a) ?? 0) ||
        (a < b ? -1 : a > b ? 1 : 0)
      )
    })
    ordered.forEach((id, i) => rank.set(id, i))
    columns.set(x, ordered)
  }

  // 双向迭代
  for (let it = 0; it < iterations; it++) {
    const dir = it % 2 === 0 ? xs : [...xs].reverse()
    for (const x of dir) {
      const ids = columns.get(x)!
      const scored = ids.map((id, i) => {
        const neigh: number[] = []
        for (const m of adj.get(id) ?? []) {
          const mk = kindOf.get(m)
          if (mk === undefined) continue
          if (RANK_X[mk] !== x && rank.has(m)) neigh.push(rank.get(m)!)
        }
        const med = median(neigh)
        return { id, key: med ?? i, i }
      })
      scored.sort(
        (a, b) =>
          a.key - b.key ||
          a.i - b.i ||
          (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
      )
      const next = scored.map((s) => s.id)
      next.forEach((id, i) => rank.set(id, i))
      columns.set(x, next)
    }
  }

  return rank
}
