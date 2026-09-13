import { describe, expect, it } from 'vitest'
import type { CanvasDoc } from '../src/domain/canvas'
import {
  OVERVIEW_LINK_MAX_WIDTH,
  OVERVIEW_LINK_MIN_WIDTH,
  OVERVIEW_NODE_PREFIX,
  buildOverview,
  columnKeyOf,
  isOverviewNodeId,
  overviewColumnKey,
  overviewColumnLayer,
  overviewCountText,
  overviewLinkWidth,
} from '../src/domain/canvas-overview'

function doc(nodes: Array<{ id: string; kind: CanvasDoc['nodes'][number]['kind'] }>, edges: Array<{ id: string; source: string; target: string; system?: boolean }>): CanvasDoc {
  return {
    nodes: nodes.map((n, i) => ({
      id: n.id,
      kind: n.kind,
      label: n.id,
      ref: n.id,
      system: true,
      adopted: false,
      stale: false,
      pinned: false,
      x: [0, 260, 480, 720, 960][i % 5],
      y: 0,
      props: {},
    })),
    edges: edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      rel: 'r',
      system: e.system !== false,
    })),
  }
}

describe('columnKeyOf / isOverviewNodeId / overviewColumnKey', () => {
  it('把 NodeKind 映射到五列 key，未知类型返回 null', () => {
    expect(columnKeyOf('rule')).toBe('rule')
    expect(columnKeyOf('fact')).toBe('fact')
    expect(columnKeyOf('verify_item')).toBe('fact')
    expect(columnKeyOf('object')).toBe('object')
    expect(columnKeyOf('evidence')).toBe('object')
    expect(columnKeyOf('hypothesis')).toBe('object')
    expect(columnKeyOf('note')).toBe('object')
    expect(columnKeyOf('function_result')).toBe('object')
    expect(columnKeyOf('source_row')).toBe('row')
    expect(columnKeyOf('source_file')).toBe('file')
  })
  it('isOverviewNodeId / overviewColumnKey', () => {
    expect(isOverviewNodeId(`${OVERVIEW_NODE_PREFIX}rule`)).toBe(true)
    expect(isOverviewNodeId('rule:R1')).toBe(false)
    expect(overviewColumnKey(`${OVERVIEW_NODE_PREFIX}object`)).toBe('object')
    expect(overviewColumnKey('rule:R1')).toBeNull()
  })
  it('overviewColumnLayer：骨干层 undefined；明细层返回对应 kind', () => {
    expect(overviewColumnLayer('rule')).toBeUndefined()
    expect(overviewColumnLayer('fact')).toBeUndefined()
    expect(overviewColumnLayer('object')).toBe('object')
    expect(overviewColumnLayer('row')).toBe('source_row')
    expect(overviewColumnLayer('file')).toBe('source_file')
  })
})

describe('overviewCountText', () => {
  it('99 以内原样返回，超过压成 99+', () => {
    expect(overviewCountText(0)).toBe('0')
    expect(overviewCountText(7)).toBe('7')
    expect(overviewCountText(99)).toBe('99')
    expect(overviewCountText(100)).toBe('99+')
    expect(overviewCountText(1234)).toBe('99+')
  })
  it('非法值兜底', () => {
    expect(overviewCountText(Number.NaN)).toBe('0')
    expect(overviewCountText(-1)).toBe('0')
  })
})

describe('overviewLinkWidth', () => {
  it('单调 + 边界', () => {
    expect(overviewLinkWidth(0)).toBe(OVERVIEW_LINK_MIN_WIDTH)
    expect(overviewLinkWidth(1)).toBe(OVERVIEW_LINK_MAX_WIDTH)
    expect(overviewLinkWidth(0.5)).toBeGreaterThan(OVERVIEW_LINK_MIN_WIDTH)
    expect(overviewLinkWidth(0.5)).toBeLessThan(OVERVIEW_LINK_MAX_WIDTH)
    // 非数值兜底到最细
    expect(overviewLinkWidth(Number.NaN)).toBe(OVERVIEW_LINK_MIN_WIDTH)
    expect(overviewLinkWidth(-0.1)).toBe(OVERVIEW_LINK_MIN_WIDTH)
    expect(overviewLinkWidth(1.5)).toBe(OVERVIEW_LINK_MAX_WIDTH)
  })
})

describe('buildOverview', () => {
  it('空 doc：零列零边', () => {
    const m = buildOverview({ nodes: [], edges: [] })
    expect(m).toEqual({ columns: [], links: [], totalNodes: 0, totalEdges: 0 })
  })

  it('仅按列聚合：每个非空 kind 计入对应列；空列不产出', () => {
    const d = doc(
      [
        { id: 'r1', kind: 'rule' },
        { id: 'f1', kind: 'fact' },
        { id: 'o1', kind: 'object' },
        { id: 'o2', kind: 'hypothesis' },
      ],
      [],
    )
    const m = buildOverview(d)
    expect(m.columns.map((c) => c.key)).toEqual(['rule', 'fact', 'object'])
    expect(m.columns.find((c) => c.key === 'object')?.count).toBe(2)
    // object 列同时含 object / hypothesis；用集合断言忽略内部排序
    const objectKinds = m.columns.find((c) => c.key === 'object')?.kinds ?? []
    expect(objectKinds.map((k) => k.kind).sort()).toEqual(['hypothesis', 'object'])
    expect(objectKinds.every((k) => k.count === 1)).toBe(true)
    expect(m.links).toEqual([])
    expect(m.totalNodes).toBe(4)
  })

  it('反向重边归一化为左→右（同对列只产一条）', () => {
    const d = doc(
      [
        { id: 'r1', kind: 'rule' },
        { id: 'o1', kind: 'object' },
      ],
      [
        { id: 'e1', source: 'r1', target: 'o1' },
        { id: 'e2', source: 'o1', target: 'r1' },
        { id: 'e3', source: 'o1', target: 'r1' },
      ],
    )
    const m = buildOverview(d)
    expect(m.links).toHaveLength(1)
    expect(m.links[0].source).toBe('rule')
    expect(m.links[0].target).toBe('object')
    expect(m.links[0].count).toBe(3)
    expect(m.links[0].weight).toBe(1)
  })

  it('weight = count / maxCount', () => {
    const d = doc(
      [
        { id: 'r1', kind: 'rule' },
        { id: 'f1', kind: 'fact' },
        { id: 'o1', kind: 'object' },
        { id: 'sr1', kind: 'source_row' },
      ],
      [
        { id: 'e1', source: 'r1', target: 'f1' },
        { id: 'e2', source: 'r1', target: 'f1' },
        { id: 'e3', source: 'f1', target: 'o1' },
        { id: 'e4', source: 'o1', target: 'sr1' },
        { id: 'e5', source: 'o1', target: 'sr1' },
        { id: 'e6', source: 'o1', target: 'sr1' },
        { id: 'e7', source: 'o1', target: 'sr1' },
      ],
    )
    const m = buildOverview(d)
    const byPair = new Map(m.links.map((l) => [`${l.source}--${l.target}`, l]))
    expect(byPair.get('rule--fact')?.count).toBe(2)
    expect(byPair.get('fact--object')?.count).toBe(1)
    expect(byPair.get('object--row')?.count).toBe(4)
    // maxCount = 4
    expect(byPair.get('object--row')?.weight).toBe(1)
    expect(byPair.get('rule--fact')?.weight).toBe(0.5)
    expect(byPair.get('fact--object')?.weight).toBe(0.25)
  })

  it('列内自环计入 inner，不参与主干边', () => {
    const d = doc(
      [
        { id: 'f1', kind: 'fact' },
        { id: 'f2', kind: 'fact' },
        { id: 'r1', kind: 'rule' },
      ],
      [
        { id: 'e1', source: 'f1', target: 'f2' }, // 同列自环
        { id: 'e2', source: 'r1', target: 'f1' },
      ],
    )
    const m = buildOverview(d)
    expect(m.columns.find((c) => c.key === 'fact')?.inner).toBe(1)
    // 主干边只剩 rule--fact
    expect(m.links).toHaveLength(1)
    expect(m.links[0].count).toBe(1)
  })

  it('edges 指向不存在的节点 / 未知 kind 的节点 = 静默丢弃，不计入 totalEdges', () => {
    const d = doc(
      [
        { id: 'r1', kind: 'rule' },
        { id: 'f1', kind: 'fact' },
      ],
      [
        { id: 'e1', source: 'r1', target: 'ghost' }, // target 不存在
        { id: 'e2', source: 'r1', target: 'f1' },
      ],
    )
    const m = buildOverview(d)
    expect(m.totalEdges).toBe(1)
    expect(m.links[0].count).toBe(1)
  })
})