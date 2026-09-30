import { describe, expect, it } from 'vitest'
import {
  CASE_EDGE_RELS,
  CASE_KIND_ORDER,
  CASE_MANUAL_NODE_KINDS,
  CASE_ONLY_KINDS,
  KIND_LABELS,
  KIND_ORDER,
  MANUAL_NODE_KINDS,
  MANUAL_RELS,
  NODE_KINDS,
  RESEARCH_RELS,
  SYSTEM_RELS,
  caseCanvasId,
  caseAllowedRels,
  caseCanConnect,
  canConnect,
  isManualNode,
} from '../src/domain/canvas'
import { RANK_X } from '../src/domain/canvas-layout'
import { canvasTokens } from '../src/design/tokens'
import { NAV_GROUPS } from '../src/nav/sections'

// P1 案件级研判画布契约（PRD V1.0.0 功能 3 / REQ-P1-01~04）。
// 核心红线「域隔离零回归」：NodeKind 扩至 14 类，但线索域口径
// （KIND_ORDER / MANUAL_NODE_KINDS / canConnect 矩阵）必须原样不变——
// 研判节点不出现在线索画布 UI，研判边是案件画布的人工关系，
// 不得混入线索画布连线矩阵。

describe('NodeKind 域升级（线索域零回归）', () => {
  it('NODE_KINDS 14 类 = 既有 10 + 案件级 4，无重名', () => {
    expect(NODE_KINDS.length).toBe(14)
    expect(new Set(NODE_KINDS).size).toBe(14)
    for (const k of CASE_ONLY_KINDS) expect(NODE_KINDS).toContain(k)
  })

  it('KIND_LABELS 全量覆盖且研判四类中文释义就位', () => {
    for (const k of NODE_KINDS) expect(KIND_LABELS[k]).toBeTruthy()
    expect(KIND_LABELS.subject).toBe('主体')
    expect(KIND_LABELS.place).toBe('地点')
    expect(KIND_LABELS.event).toBe('事件')
    expect(KIND_LABELS.analysis_result).toBe('研判结论')
  })

  it('线索域口径不变：KIND_ORDER 仍 10 类且不含研判 kind', () => {
    expect(KIND_ORDER.length).toBe(10)
    for (const k of CASE_ONLY_KINDS) expect(KIND_ORDER).not.toContain(k)
  })

  it('线索人工节点仍只有 hypothesis/note；研判 kind 不算线索人工节点', () => {
    expect(MANUAL_NODE_KINDS.length).toBe(2)
    expect(isManualNode({ kind: 'subject', system: false })).toBe(false)
    expect(isManualNode({ kind: 'note', system: false })).toBe(true)
  })

  it('SYSTEM_RELS 7 / MANUAL_RELS 4 不变；研判边 6 类独立成表且互斥', () => {
    expect(SYSTEM_RELS.length).toBe(7)
    expect(MANUAL_RELS.length).toBe(4)
    expect(RESEARCH_RELS).toEqual(['位于', '发生于', '支撑', '反驳', '同现', '联系'])
    for (const r of RESEARCH_RELS) {
      expect(MANUAL_RELS).not.toContain(r)
      expect(SYSTEM_RELS).not.toContain(r)
    }
  })

  it('线索画布连线矩阵拒绝研判边（bad_rel，研判关系不得混入线索域）', () => {
    expect(canConnect('subject', 'place', '位于').code).toBe('bad_rel')
    expect(canConnect('subject', 'subject', '同现').code).toBe('bad_rel')
    expect(canConnect('analysis_result', 'hypothesis', '支撑').code).toBe('bad_rel')
  })
})

describe('案件级域契约', () => {
  it('CASE_MANUAL_NODE_KINDS 5 类：analysis_result 仅回写产生，不可人工添加', () => {
    expect(CASE_MANUAL_NODE_KINDS.length).toBe(5)
    expect(CASE_MANUAL_NODE_KINDS).not.toContain('analysis_result')
    expect(CASE_MANUAL_NODE_KINDS).toContain('subject')
    expect(CASE_MANUAL_NODE_KINDS).toContain('place')
    expect(CASE_MANUAL_NODE_KINDS).toContain('event')
  })

  it('CASE_KIND_ORDER：研判 4 类在前 + hypothesis/note 兜底，均为合法 NodeKind', () => {
    expect(CASE_KIND_ORDER.length).toBe(6)
    for (const k of CASE_KIND_ORDER) expect(NODE_KINDS).toContain(k)
  })

  it('域键合成：canvas_id = case#<case_id>（复用 clue_canvas 存储）', () => {
    expect(caseCanvasId('C2026001')).toBe('case#C2026001')
    expect(caseCanvasId('')).toBe('case#')
  })
})

describe('样式令牌与布局列（穷举映射全量对拍）', () => {
  it('canvasTokens.kind / RANK_X 覆盖全部 14 类且无 undefined', () => {
    for (const k of NODE_KINDS) {
      expect(canvasTokens.kind[k]?.chip).toBeTruthy()
      expect(canvasTokens.kind[k]?.ink).toBeTruthy()
      expect(Number.isFinite(RANK_X[k])).toBe(true)
    }
  })

  it('研判四类 chip 色互异（一眼分型）', () => {
    const chips = CASE_ONLY_KINDS.map((k) => canvasTokens.kind[k].chip)
    expect(new Set(chips).size).toBe(4)
  })

  it('RANK_X 列语义：主体 < 地点/事件/结论 < 假设（研判边左→右流向）', () => {
    expect(RANK_X.subject).toBe(0)
    expect(RANK_X.place).toBe(RANK_X.event)
    expect(RANK_X.event).toBe(RANK_X.analysis_result)
    expect(RANK_X.analysis_result).toBeLessThan(RANK_X.hypothesis)
  })
})

describe('入口（侧栏）', () => {
  it('侧栏研判中心含「研判画布」入口（mvp 6）', () => {
    const group = NAV_GROUPS.find((g) => g.key === 'research')
    expect(group).toBeTruthy()
    const item = group?.items.find((i) => i.key === 'canvas')
    expect(item?.label).toBe('研判画布')
    expect(item?.mvp).toBe(6)
  })
})

// ======================================================================
// P2 案件级连线矩阵（caseCanConnect 与后端 canvas_edit.can_connect
// 案件分支逐一对应；本组即前端先拒层的真相表对拍）。
// ======================================================================

describe('caseCanConnect（案件级连线矩阵，与后端真相表对拍）', () => {
  it('CASE_EDGE_RELS 10 类 = 人工 4 在前 + 研判 6 在后（与后端 allowed_rels 同序）', () => {
    expect(CASE_EDGE_RELS).toEqual([
      '推断为',
      '证实',
      '查否',
      '补充说明',
      '位于',
      '发生于',
      '支撑',
      '反驳',
      '同现',
      '联系',
    ])
  })

  it('通用拒绝：系统关系 bad_rel；note 只能作起点；数据行/数据源禁入', () => {
    expect(caseCanConnect('subject', 'place', '转移').code).toBe('bad_rel')
    expect(caseCanConnect('subject', 'note', '同现').code).toBe('note_target')
    expect(caseCanConnect('subject', 'source_row', '位于').code).toBe('forbidden_target')
    expect(caseCanConnect('subject', 'source_file', '位于').code).toBe('forbidden_target')
  })

  it('同现：subject↔subject 先于 kind 级自连（同 kind 异节点合法）', () => {
    expect(caseCanConnect('subject', 'subject', '同现').ok).toBe(true)
    expect(caseCanConnect('subject', 'place', '同现').code).toBe('matrix')
    expect(caseCanConnect('analysis_result', 'analysis_result', '同现').code).toBe('matrix')
  })

  it('联系：subject↔subject 通话关系专用（与同现同口径，语义不同）', () => {
    expect(caseCanConnect('subject', 'subject', '联系').ok).toBe(true)
    expect(caseCanConnect('subject', 'place', '联系').code).toBe('matrix')
    expect(caseCanConnect('analysis_result', 'analysis_result', '联系').code).toBe('matrix')
    expect(caseCanConnect('subject', 'hypothesis', '联系').code).toBe('matrix')
  })

  it('位于/发生于：subject 与 analysis_result 可指向 place/event', () => {
    expect(caseCanConnect('subject', 'place', '位于').ok).toBe(true)
    expect(caseCanConnect('analysis_result', 'place', '位于').ok).toBe(true)
    expect(caseCanConnect('subject', 'event', '发生于').ok).toBe(true)
    expect(caseCanConnect('analysis_result', 'event', '发生于').ok).toBe(true)
    expect(caseCanConnect('place', 'place', '位于').code).toBe('matrix')
    expect(caseCanConnect('subject', 'place', '发生于').code).toBe('matrix')
    expect(caseCanConnect('event', 'event', '发生于').code).toBe('matrix')
  })

  it('支撑/反驳：仅 analysis_result→hypothesis（研判边左→右流向）', () => {
    expect(caseCanConnect('analysis_result', 'hypothesis', '支撑').ok).toBe(true)
    expect(caseCanConnect('analysis_result', 'hypothesis', '反驳').ok).toBe(true)
    expect(caseCanConnect('subject', 'hypothesis', '支撑').code).toBe('matrix')
    expect(caseCanConnect('analysis_result', 'analysis_result', '支撑').code).toBe('matrix')
    expect(caseCanConnect('hypothesis', 'analysis_result', '反驳').code).toBe('matrix')
  })

  it('人工 4 类与线索域矩阵同口径（推断为/证实/查否/补充说明）', () => {
    expect(caseCanConnect('fact', 'hypothesis', '推断为').ok).toBe(true)
    expect(caseCanConnect('object', 'hypothesis', '推断为').ok).toBe(true)
    expect(caseCanConnect('verify_item', 'hypothesis', '证实').ok).toBe(true)
    expect(caseCanConnect('evidence', 'hypothesis', '查否').ok).toBe(true)
    expect(caseCanConnect('note', 'subject', '补充说明').ok).toBe(true)
    expect(caseCanConnect('subject', 'hypothesis', '推断为').code).toBe('matrix')
    expect(caseCanConnect('subject', 'hypothesis', '补充说明').code).toBe('note_source_only')
    expect(caseCanConnect('fact', 'place', '推断为').code).toBe('matrix')
  })

  it('同 kind 兜底 self；note→note 优先落备注方向拒绝（与后端分支序一致）', () => {
    expect(caseCanConnect('note', 'note', '补充说明').code).toBe('note_target')
    expect(caseCanConnect('fact', 'fact', '推断为').code).toBe('self')
  })

  it('caseAllowedRels：关系气泡白名单与矩阵一致', () => {
    expect(caseAllowedRels('subject', 'subject')).toEqual(['同现', '联系'])
    expect(caseAllowedRels('analysis_result', 'place')).toEqual(['位于'])
    expect(caseAllowedRels('analysis_result', 'hypothesis')).toEqual(['支撑', '反驳'])
    expect(caseAllowedRels('fact', 'hypothesis')).toEqual(['推断为'])
    expect(caseAllowedRels('note', 'hypothesis')).toEqual(['补充说明'])
    expect(caseAllowedRels('place', 'event')).toEqual([])
  })

  it('域隔离反向验证：线索域 canConnect 仍拒绝全部研判边', () => {
    expect(canConnect('fact', 'hypothesis', '位于').code).toBe('bad_rel')
    expect(canConnect('fact', 'hypothesis', '同现').code).toBe('bad_rel')
    expect(canConnect('subject', 'place', '发生于').code).toBe('bad_rel')
  })
})
