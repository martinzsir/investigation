import { describe, expect, it } from 'vitest'
import type { CanvasDoc, CanvasNode, NodeKind } from '../src/domain/canvas'
import {
  FOCUS_HOPS_MAX,
  LANE_PREFIX,
  SYNTHETIC_EDGE_PREFIX,
  buildDetailModel,
  buildRowOrdinals,
  collapseChain,
  focusChain,
  isLaneId,
  isToggleable,
  nodeSubtitle,
  projectView,
  rowBadges,
} from '../src/domain/canvas-view'

// UX P0 渲染投影纯函数：事实分组 BFS、多生产者并集、compact/full 投影、
// 层强制开关、行序号/副标题（RC-104 不带字段明文）、focus chain。

function node(
  id: string,
  kind: NodeKind,
  extra: Partial<CanvasNode> = {},
): CanvasNode {
  return {
    id,
    kind,
    ref: id,
    label: id,
    system: extra.system ?? true,
    adopted: false,
    stale: false,
    pinned: false,
    x: 0,
    y: 0,
    props: extra.props ?? {},
    ...extra,
  }
}

function edge(
  source: string,
  target: string,
  rel: string,
  system = true,
): CanvasDoc['edges'][number] {
  return { id: `e:${source}--${rel}--${target}`, source, target, rel, system }
}

/**
 * 结构：
 *   rule ─命中→ fact1 ─来源行→ row1 ─所属文件→ file1
 *                     ├─涉及→ obj1
 *                     └─涉及→ objShared
 *   rule ─命中→ fact2 ─涉及→ objShared
 *                     └─来源行→ row2（同数据源）
 *   fact2 ─人工推断→ hyp1（system=false 骨干）
 */
function doc(): CanvasDoc {
  return {
    nodes: [
      node('rule:R1', 'rule'),
      node('fact:f1', 'fact'),
      node('fact:f2', 'fact'),
      node('obj:1', 'object', { props: { type: 'person', type_title: '自然人' } }),
      node('obj:s', 'object', { props: { type: 'company', type_title: '公司' } }),
      node('row:1', 'source_row', {
        props: { source: '银行流水', row_uri: 'secret://row1', registered: true },
      }),
      node('row:2', 'source_row', {
        props: {
          source: '银行流水',
          registered: false,
          missing: true,
          granularity: '表级汇总',
        },
      }),
      node('file:1', 'source_file', { props: { registered: true } }),
      node('hyp:1', 'hypothesis', { system: false, props: { content: '人工假设' } }),
    ],
    edges: [
      edge('rule:R1', 'fact:f1', '命中'),
      edge('rule:R1', 'fact:f2', '命中'),
      edge('fact:f1', 'row:1', '来源行'),
      edge('fact:f1', 'obj:1', '涉及'),
      edge('fact:f1', 'obj:s', '涉及'),
      edge('fact:f2', 'obj:s', '涉及'),
      edge('fact:f2', 'row:2', '来源行'),
      edge('row:1', 'file:1', '所属文件'),
      edge('fact:f2', 'hyp:1', '推断为', false),
    ],
  }
}

/** 线性链（验证传递节点折叠）：rule → fact → row → file */
function chainDoc(): CanvasDoc {
  return {
    nodes: [
      node('rule:R1', 'rule'),
      node('fact:f1', 'fact'),
      node('row:1', 'source_row'),
      node('file:1', 'source_file'),
    ],
    edges: [
      edge('rule:R1', 'fact:f1', '命中'),
      edge('fact:f1', 'row:1', '来源行'),
      edge('row:1', 'file:1', '所属文件'),
    ],
  }
}

describe('buildDetailModel 事实分组 BFS', () => {
  it('沿系统边收集 objects/rows/files，传递经过 row 的 file 也入组', () => {
    const m = buildDetailModel(doc())
    const g1 = m.groupByFact.get('fact:f1')!
    expect(g1.objects.sort()).toEqual(['obj:1', 'obj:s'])
    expect(g1.rows).toEqual(['row:1'])
    // row:1 → file:1 传递可达
    expect(g1.files).toEqual(['file:1'])
    expect(g1.total).toBe(4)

    const g2 = m.groupByFact.get('fact:f2')!
    expect(g2.objects).toEqual(['obj:s'])
    expect(g2.rows).toEqual(['row:2'])
    // 人工边不进邻接（hyp:1 不是明细也不可达）
    expect(g2.files).toEqual([])
  })

  it('共享明细取生产者并集', () => {
    const m = buildDetailModel(doc())
    expect([...(m.producers.get('obj:s') ?? [])].sort()).toEqual([
      'fact:f1',
      'fact:f2',
    ])
    expect([...(m.producers.get('hyp:1') ?? [])]).toEqual([])
  })
})

describe('projectView compact/full', () => {
  it('compact 默认：骨干 + 人工节点恒可见，明细隐藏', () => {
    const d = doc()
    const p = projectView(d, buildDetailModel(d), {
      mode: 'compact',
      expandedRoots: new Set(),
    })
    for (const id of ['rule:R1', 'fact:f1', 'fact:f2', 'hyp:1']) {
      expect(p.visibleNodeIds.has(id)).toBe(true)
    }
    for (const id of ['obj:1', 'obj:s', 'row:1', 'row:2', 'file:1']) {
      expect(p.visibleNodeIds.has(id)).toBe(false)
    }
    // 不产生悬挂边
    for (const e of d.edges) {
      expect(p.visibleEdgeIds.has(e.id)).toBe(
        p.visibleNodeIds.has(e.source) && p.visibleNodeIds.has(e.target),
      )
    }
  })

  it('展开任一生产者事实即可见共享明细；所有生产者折叠才隐藏', () => {
    const d = doc()
    const m = buildDetailModel(d)
    const onlyF1 = projectView(d, m, {
      mode: 'compact',
      expandedRoots: new Set(['fact:f1']),
    })
    expect(onlyF1.visibleNodeIds.has('obj:s')).toBe(true)
    expect(onlyF1.visibleNodeIds.has('obj:1')).toBe(true)
    expect(onlyF1.visibleNodeIds.has('row:1')).toBe(true)
    expect(onlyF1.visibleNodeIds.has('file:1')).toBe(true)
    // f2 的专属明细仍隐藏
    expect(onlyF1.visibleNodeIds.has('row:2')).toBe(false)

    const both = projectView(d, m, {
      mode: 'compact',
      expandedRoots: new Set(['fact:f1', 'fact:f2']),
    })
    expect(both.visibleNodeIds.has('row:2')).toBe(true)

    const onlyF2 = projectView(d, m, {
      mode: 'compact',
      expandedRoots: new Set(['fact:f2']),
    })
    // obj:s 被 f2 展开 → 可见；obj:1 仅属于 f1 → 隐藏
    expect(onlyF2.visibleNodeIds.has('obj:s')).toBe(true)
    expect(onlyF2.visibleNodeIds.has('obj:1')).toBe(false)
  })

  it('非事实根展开：沿系统边可达的明细可见', () => {
    const d = doc()
    const p = projectView(d, buildDetailModel(d), {
      mode: 'compact',
      expandedRoots: new Set(['row:1']),
    })
    // object 不是 row 的下游；file:1 是
    expect(p.visibleNodeIds.has('file:1')).toBe(true)
    expect(p.visibleNodeIds.has('obj:1')).toBe(false)
  })

  it('三层开关强制显示，与展开集无关', () => {
    const d = doc()
    const p = projectView(d, buildDetailModel(d), {
      mode: 'compact',
      expandedRoots: new Set(),
      layers: { object: false, source_row: true, source_file: false },
    })
    expect(p.visibleNodeIds.has('row:1')).toBe(true)
    expect(p.visibleNodeIds.has('row:2')).toBe(true)
    expect(p.visibleNodeIds.has('obj:1')).toBe(false)
    expect(p.visibleNodeIds.has('file:1')).toBe(false)
  })

  it('full 模式全量可见', () => {
    const d = doc()
    const p = projectView(d, buildDetailModel(d), {
      mode: 'full',
      expandedRoots: new Set(),
    })
    expect(p.visibleNodeIds.size).toBe(d.nodes.length)
    expect(p.visibleEdgeIds.size).toBe(d.edges.length)
  })
})

describe('buildRowOrdinals / rowBadges / nodeSubtitle', () => {
  it('同数据源按节点 id 确定序，序号从 1 开始', () => {
    const o = buildRowOrdinals(doc())
    // id 排序：row:1 < row:2
    expect(o.get('row:1')).toEqual({ source: '银行流水', ordinal: 1 })
    expect(o.get('row:2')).toEqual({ source: '银行流水', ordinal: 2 })
  })

  it('副标题只用 props/序号，绝不带出 row_uri 等字段明文', () => {
    const d = doc()
    const ctx = {
      model: buildDetailModel(d),
      ordinals: buildRowOrdinals(d),
      dimensions: new Map([['rule:R1', '资金']]),
    }
    const row1 = d.nodes.find((x) => x.id === 'row:1')!
    const sub = nodeSubtitle(row1, ctx)
    expect(sub).toBe('银行流水 · 行 1')
    expect(sub).not.toContain('secret')
    expect(nodeSubtitle(d.nodes.find((x) => x.id === 'rule:R1')!, ctx))
      .toBe('资金')
    expect(nodeSubtitle(d.nodes.find((x) => x.id === 'obj:1')!, ctx))
      .toBe('自然人')
    expect(nodeSubtitle(d.nodes.find((x) => x.id === 'file:1')!, ctx))
      .toBe('已登记数据源')
    const f1 = nodeSubtitle(d.nodes.find((x) => x.id === 'fact:f1')!, ctx)
    expect(f1).toContain('1 条来源行')
    expect(f1).toContain('2 个实体')
  })

  it('rowBadges 反映缺失/未登记/表级汇总', () => {
    const d = doc()
    expect(rowBadges(d.nodes.find((x) => x.id === 'row:2')!)).toEqual([
      'missing',
      'unregistered',
      'table-summary',
    ])
    expect(rowBadges(d.nodes.find((x) => x.id === 'row:1')!)).toEqual([])
  })
})

describe('focusChain / toggle / lane', () => {
  it('一跳上下游（系统边+人工边都参与）', () => {
    const d = doc()
    const c = focusChain(d, 'fact:f2')!
    expect([...c.nodeIds].sort()).toEqual([
      'fact:f2',
      'hyp:1',
      'obj:s',
      'row:2',
      'rule:R1',
    ])
    expect(c.edgeIds.size).toBe(4)
    expect(focusChain(d, 'nope')).toBeNull()
  })

  it('跳数游标：hops 越大链越长，且跳数可回读', () => {
    const d = doc()
    const one = focusChain(d, 'fact:f1')!
    expect(one.depthByNode.get('fact:f1')).toBe(0)
    expect(one.depthByNode.get('rule:R1')).toBe(1)
    expect(one.nodeIds.has('file:1')).toBe(false)

    const two = focusChain(d, 'fact:f1', 2)!
    // 第二跳经 row:1 到 file:1、经 rule:R1/obj:s 到 fact:f2
    expect(two.depthByNode.get('file:1')).toBe(2)
    expect(two.depthByNode.get('fact:f2')).toBe(2)
    expect(two.nodeIds.has('row:2')).toBe(false)

    const three = focusChain(d, 'fact:f1', 3)!
    expect(three.depthByNode.get('row:2')).toBe(3)
    expect(three.depthByNode.get('hyp:1')).toBe(3)
    // 边跳数归属首次触达的一跳
    expect(three.depthByEdge.get('e:fact:f2--来源行--row:2')).toBe(3)
    expect(one.edgeIds.size).toBeLessThan(two.edgeIds.size)
    expect(two.edgeIds.size).toBeLessThan(three.edgeIds.size)
  })

  it('跳数被 clamp 到 1..FOCUS_HOPS_MAX，非法值回落 1', () => {
    const d = doc()
    expect(focusChain(d, 'fact:f1', 0)!.edgeIds.size)
      .toBe(focusChain(d, 'fact:f1', 1)!.edgeIds.size)
    expect(focusChain(d, 'fact:f1', 99)!.edgeIds.size)
      .toBe(focusChain(d, 'fact:f1', FOCUS_HOPS_MAX)!.edgeIds.size)
    expect(focusChain(d, 'fact:f1', Number.NaN)!.edgeIds.size)
      .toBe(focusChain(d, 'fact:f1', 1)!.edgeIds.size)
  })

  it('线性链：度=2 的系统中间节点被压成一条合成边', () => {
    const d = chainDoc()
    const chain = focusChain(d, 'rule:R1', 3)!
    const c = collapseChain(d, chain)
    expect([...c.collapsedNodeIds].sort()).toEqual(['fact:f1', 'row:1'])
    expect(c.syntheticEdges).toHaveLength(1)
    const s = c.syntheticEdges[0]
    expect(s.source).toBe('rule:R1')
    expect(s.target).toBe('file:1')
    // via 按链路顺序，标签可直接渲染成「经 N 个中间节点」
    expect(s.via).toEqual(['fact:f1', 'row:1'])
    expect(s.id.startsWith(SYNTHETIC_EDGE_PREFIX)).toBe(true)
    expect(c.visibleNodeIds.has('rule:R1')).toBe(true)
    expect(c.visibleNodeIds.has('file:1')).toBe(true)
    expect(c.visibleNodeIds.has('fact:f1')).toBe(false)
  })

  it('分叉（度>2）、焦点、人工节点都不折叠', () => {
    const d = chainDoc()
    // fact:f1 加一条到 obj:1 的边 → 度 3，成为分叉
    d.nodes.push(node('obj:1', 'object'))
    d.edges.push(edge('fact:f1', 'obj:1', '涉及'))
    // 焦点本身（depth 0）永不折叠
    let c = collapseChain(d, focusChain(d, 'fact:f1', 3)!)
    expect(c.collapsedNodeIds.has('fact:f1')).toBe(false)
    expect(c.collapsedNodeIds.has('row:1')).toBe(true)

    // 人工节点（system=false）不折叠
    const d2 = chainDoc()
    d2.nodes[2] = node('row:1', 'source_row', { system: false })
    c = collapseChain(d2, focusChain(d2, 'rule:R1', 3)!)
    expect(c.collapsedNodeIds.has('row:1')).toBe(false)
  })

  it('keep 集合（点开过的段）与 maxVia 限制生效', () => {
    const d = chainDoc()
    const chain = focusChain(d, 'rule:R1', 3)!
    // keep 保证「该节点本身不被折叠」，段会绕开保留点重新计算
    const kept = collapseChain(d, chain, { keep: new Set(['row:1']) })
    expect(kept.collapsedNodeIds.has('row:1')).toBe(false)
    expect(kept.collapsedNodeIds.has('fact:f1')).toBe(true)
    expect(kept.syntheticEdges[0].target).toBe('row:1')
    // 段长超过 maxVia 时不折叠（避免一段吃掉太多上下文）
    expect(collapseChain(d, chain, { maxVia: 1 }).syntheticEdges).toHaveLength(0)
  })

  it('isToggleable 仅 fact/object/source_row', () => {
    expect(isToggleable(node('f', 'fact'))).toBe(true)
    expect(isToggleable(node('o', 'object'))).toBe(true)
    expect(isToggleable(node('r', 'source_row'))).toBe(true)
    expect(isToggleable(node('v', 'verify_item'))).toBe(false)
  })

  it('lane 伪节点 id 前缀判定', () => {
    expect(isLaneId(`${LANE_PREFIX}band:0`)).toBe(true)
    expect(isLaneId('fact:f1')).toBe(false)
  })
})
