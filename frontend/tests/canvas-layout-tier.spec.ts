import { describe, expect, it } from 'vitest'
import type { CanvasDoc } from '../src/domain/canvas'
import {
  TIER_3_OPACITY,
  TIER_LABELS,
  layoutByTier,
  tierCounts,
  tierOf,
  tierStyleHint,
} from '../src/domain/canvas-layout-tier'

function node(
  id: string,
  kind: CanvasDoc['nodes'][number]['kind'],
  extra: Partial<CanvasDoc['nodes'][number]> = {},
): CanvasDoc['nodes'][number] {
  return {
    id,
    kind,
    ref: id,
    label: id,
    system: kind !== 'hypothesis' && kind !== 'note',
    adopted: false,
    stale: false,
    pinned: false,
    x: 0,
    y: 0,
    props: {},
    ...extra,
  }
}

describe('TIER_LABELS', () => {
  it('三层有中文标签且互不相同', () => {
    expect(new Set(Object.values(TIER_LABELS)).size).toBe(3)
    expect(TIER_LABELS[1]).toBeTruthy()
    expect(TIER_LABELS[2]).toBeTruthy()
    expect(TIER_LABELS[3]).toBeTruthy()
  })
})

describe('tierOf', () => {
  it('系统规则 / 系统事实 / 书证 → Tier 1', () => {
    expect(tierOf(node('r', 'rule'))).toBe(1)
    expect(tierOf(node('f', 'fact'))).toBe(1)
    expect(tierOf(node('e', 'evidence'))).toBe(1)
  })
  it('采纳节点 → Tier 1', () => {
    expect(
      tierOf(node('h', 'hypothesis', { adopted: true })),
    ).toBe(1)
    expect(
      tierOf(node('vi', 'verify_item', { adopted: true })),
    ).toBe(1)
  })
  it('未采纳假设 / 备注 → Tier 3', () => {
    expect(tierOf(node('h', 'hypothesis'))).toBe(3)
    expect(tierOf(node('n', 'note'))).toBe(3)
  })
  it('stale 节点一律 Tier 3', () => {
    expect(tierOf(node('r', 'rule', { stale: true }))).toBe(3)
    expect(tierOf(node('f', 'fact', { stale: true }))).toBe(3)
  })
  it('实体 / 数据行 / 数据源 / 查询结果 / 建议项 → Tier 2', () => {
    expect(tierOf(node('o', 'object'))).toBe(2)
    expect(tierOf(node('sr', 'source_row'))).toBe(2)
    expect(tierOf(node('sf', 'source_file'))).toBe(2)
    expect(tierOf(node('fr', 'function_result'))).toBe(2)
    expect(tierOf(node('vi', 'verify_item'))).toBe(2)
  })
})

describe('tierCounts', () => {
  it('按 tier 计数', () => {
    const d: CanvasDoc = {
      nodes: [
        node('r1', 'rule'),
        node('r2', 'rule'),
        node('f1', 'fact'),
        node('o1', 'object'),
        node('h1', 'hypothesis'), // tier 3
        node('n1', 'note'), // tier 3
      ],
      edges: [],
    }
    expect(tierCounts(d)).toEqual({ 1: 3, 2: 1, 3: 2 })
  })
})

describe('tierStyleHint', () => {
  it('Tier 3 半透，其余全不透明', () => {
    expect(tierStyleHint(node('h', 'hypothesis')).opacity).toBe(TIER_3_OPACITY)
    expect(tierStyleHint(node('h', 'hypothesis')).dimmed).toBe(true)
    expect(tierStyleHint(node('f', 'fact')).opacity).toBe(1)
    expect(tierStyleHint(node('f', 'fact')).dimmed).toBe(false)
  })
})

describe('layoutByTier', () => {
  function buildDoc(nodes: CanvasDoc['nodes'][number][]): CanvasDoc {
    return { nodes, edges: [] }
  }

  it('空 doc：返回空', () => {
    const out = layoutByTier(buildDoc([]))
    expect(out.nodes).toEqual([])
    expect(out.edges).toEqual([])
  })

  it('同列节点按 tier 分带落槽：Tier 1 在顶 / Tier 3 在底', () => {
    // 各取一个不同 tier 的节点类型：rule → tier 1、object → tier 2、hypothesis → tier 3
    const out = layoutByTier(
      buildDoc([
        node('r1', 'rule'),
        node('o1', 'object'),
        node('h1', 'hypothesis'),
        node('n1', 'note'),
      ]),
    )
    const ys = {
      t1: out.nodes.filter((n) => tierOf(n) === 1).map((n) => n.y),
      t2: out.nodes.filter((n) => tierOf(n) === 2).map((n) => n.y),
      t3: out.nodes.filter((n) => tierOf(n) === 3).map((n) => n.y),
    }
    // 同带内的 y 不超过 TIER_Y_GAP × (节点数 - 1)
    expect(Math.max(...ys.t1) - Math.min(...ys.t1)).toBeLessThan(200)
    expect(Math.max(...ys.t2) - Math.min(...ys.t2)).toBeLessThan(200)
    expect(Math.max(...ys.t3) - Math.min(...ys.t3)).toBeLessThan(200)
    // 带间：t1 全在 t2 之上
    expect(Math.max(...ys.t1)).toBeLessThan(Math.min(...ys.t2))
    // t2 全在 t3 之上
    expect(Math.max(...ys.t2)).toBeLessThan(Math.min(...ys.t3))
    // x 仍按 RANK_X 分列（rule=0, object=480, hypothesis=480, note=480）
    expect(out.nodes.find((n) => n.id === 'r1')!.x).toBe(0)
    expect(out.nodes.find((n) => n.id === 'o1')!.x).toBe(480)
  })

  it('钉住节点保留原 y；非钉住节点按 tier 重排', () => {
    const pinned = node('r1', 'rule', { pinned: true, y: 999 })
    const floating = node('r2', 'rule')
    const out = layoutByTier(buildDoc([pinned, floating]))
    expect(out.nodes.find((n) => n.id === 'r1')!.y).toBe(999)
    expect(out.nodes.find((n) => n.id === 'r2')!.y).not.toBe(999)
    // x 仍按 RANK_X 强制（rule → 0）
    expect(out.nodes.every((n) => n.x === 0)).toBe(true)
  })

  it('edges 不被修改（仅节点坐标变化）', () => {
    const d: CanvasDoc = {
      nodes: [
        node('r1', 'rule'),
        node('o1', 'object'),
        node('h1', 'hypothesis'),
      ],
      edges: [
        {
          id: 'e1',
          source: 'r1',
          target: 'o1',
          rel: '命中',
          system: true,
        },
        {
          id: 'e2',
          source: 'o1',
          target: 'h1',
          rel: '支撑',
          system: false,
        },
      ],
    }
    const out = layoutByTier(d)
    expect(out.edges).toEqual(d.edges)
  })

  it('输入 doc 不被修改（纯函数）', () => {
    const original: CanvasDoc = buildDoc([
      node('r1', 'rule'),
      node('o1', 'object'),
      node('h1', 'hypothesis'),
    ])
    const snapshot = JSON.stringify(original)
    layoutByTier(original)
    expect(JSON.stringify(original)).toBe(snapshot)
  })

  it('跨 tier 节点的 y 间距够大（带间留白不小于 TIER_BAND_GAP）', () => {
    // 不同 tier：rule(t1) / object(t2) / hypothesis(t3)
    const out = layoutByTier(
      buildDoc([
        node('r1', 'rule'),
        node('o1', 'object'),
        node('h1', 'hypothesis'),
      ]),
    )
    const t1 = out.nodes.find((n) => n.id === 'r1')!
    const t2 = out.nodes.find((n) => n.id === 'o1')!
    const t3 = out.nodes.find((n) => n.id === 'h1')!
    expect(t2.y - t1.y).toBeGreaterThanOrEqual(180)
    expect(t3.y - t2.y).toBeGreaterThanOrEqual(180)
  })
})