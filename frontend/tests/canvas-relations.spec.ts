import { describe, expect, it } from 'vitest'
import {
  allowedRels,
  canConnect,
  findEdge,
  isManualNode,
  LIMITS,
  MANUAL_NODE_KINDS,
  MANUAL_RELS,
  validateEdgeNote,
  validateManualNode,
  validateSnapshotLabel,
  type CanvasDoc,
  type NodeKind,
} from '../src/domain/canvas'

// RC-203 人工关系合法性矩阵：10 类节点 × 4 类关系穷举，对拍独立真相表
// （与 server/app/canvas_edit.py 同口径，测试本身不复读生产实现）。

const KINDS: NodeKind[] = [
  'rule',
  'fact',
  'object',
  'source_row',
  'source_file',
  'verify_item',
  'evidence',
  'hypothesis',
  'note',
  'function_result',
]

function expectedConnect(src: NodeKind, tgt: NodeKind, rel: string): boolean {
  if (src === tgt) return false
  if (tgt === 'note') return false
  if (tgt === 'source_row' || tgt === 'source_file') return false
  if (rel === '补充说明') return src === 'note'
  if (tgt !== 'hypothesis') return false
  if (rel === '推断为') return src === 'fact' || src === 'object'
  if (rel === '证实' || rel === '查否') {
    return src === 'verify_item' || src === 'evidence'
  }
  return false
}

describe('RC-203 canConnect 矩阵穷举', () => {
  it('matches independent truth table for all 10x10x4 combinations', () => {
    let positives = 0
    for (const src of KINDS) {
      for (const tgt of KINDS) {
        for (const rel of MANUAL_RELS) {
          const got = canConnect(src, tgt, rel)
          const want = expectedConnect(src, tgt, rel)
          if (got.ok !== want) {
            throw new Error(
              `mismatch ${src} --${rel}--> ${tgt}: got ${got.ok} want ${want}`,
            )
          }
          expect(got.ok).toBe(want)
          if (want) {
            positives += 1
            expect(got.reason).toBeUndefined()
          } else {
            expect(got.reason).toBeTruthy()
          }
        }
      }
    }
    // 推断为 2 + 证实 2 + 查否 2 + 补充说明 7 = 13
    expect(positives).toBe(13)
  })

  it('rejects unknown rel', () => {
    const r = canConnect('fact', 'hypothesis', '随便写')
    expect(r.ok).toBe(false)
    expect(r.code).toBe('bad_rel')
  })
})

describe('RC-203 allowedRels 白名单（关系选择气泡）', () => {
  it('fact/object → hypothesis only 推断为', () => {
    expect(allowedRels('fact', 'hypothesis')).toEqual(['推断为'])
    expect(allowedRels('object', 'hypothesis')).toEqual(['推断为'])
  })
  it('verify_item/evidence → hypothesis 证实+查否', () => {
    expect(allowedRels('verify_item', 'hypothesis')).toEqual(['证实', '查否'])
    expect(allowedRels('evidence', 'hypothesis')).toEqual(['证实', '查否'])
  })
  it('note → hypothesis only 补充说明', () => {
    expect(allowedRels('note', 'hypothesis')).toEqual(['补充说明'])
  })
  it('note → rule/fact/object/verify_item/evidence/function_result allowed', () => {
    for (const k of ['rule', 'fact', 'object', 'verify_item', 'evidence',
      'function_result'] as NodeKind[]) {
      expect(allowedRels('note', k)).toEqual(['补充说明'])
    }
  })
  it('all blocked pairs return empty', () => {
    expect(allowedRels('fact', 'object')).toEqual([])
    expect(allowedRels('rule', 'hypothesis')).toEqual([])
    expect(allowedRels('hypothesis', 'fact')).toEqual([])
    expect(allowedRels('fact', 'note')).toEqual([])
    expect(allowedRels('note', 'note')).toEqual([])
    expect(allowedRels('fact', 'source_row')).toEqual([])
    expect(allowedRels('fact', 'source_file')).toEqual([])
  })
})

describe('RC-203 findEdge 重复边检测', () => {
  const doc: CanvasDoc = {
    nodes: [],
    edges: [
      { id: 'e1', source: 'fact:f1', target: 'cn_1', rel: '推断为', system: false },
      { id: 'e2', source: 'evi:1', target: 'cn_1', rel: '证实', system: false },
    ],
  }
  it('finds same endpoint+rel edge', () => {
    expect(findEdge(doc, 'fact:f1', 'cn_1', '推断为')?.id).toBe('e1')
  })
  it('does not confuse different rel between same endpoints', () => {
    expect(findEdge(doc, 'fact:f1', 'cn_1', '证实')).toBeUndefined()
    expect(findEdge(doc, 'evi:1', 'cn_1', '推断为')).toBeUndefined()
  })
})

describe('RC-202 人工节点/边/快照表单校验', () => {
  it('hypothesis requires title and content with length limits', () => {
    expect(Object.keys(validateManualNode('hypothesis', {
      title: '  ', content: '',
    }))).toHaveLength(2)
    expect(validateManualNode('hypothesis', {
      title: 'a'.repeat(LIMITS.hypothesisTitle),
      content: 'b'.repeat(LIMITS.content),
    })).toEqual({})
    const errs = validateManualNode('hypothesis', {
      title: 'a'.repeat(LIMITS.hypothesisTitle + 1),
      content: 'b'.repeat(LIMITS.content + 1),
    })
    expect(errs.title).toContain(String(LIMITS.hypothesisTitle))
    expect(errs.content).toContain(String(LIMITS.content))
  })

  it('note ignores title and only validates content', () => {
    const errs = validateManualNode('note', { title: '', content: '' })
    expect(errs.title).toBeUndefined()
    expect(errs.content).toBeTruthy()
    expect(validateManualNode('note', { content: '一句话备注' })).toEqual({})
  })

  it('edge note ≤200', () => {
    expect(validateEdgeNote('')).toBeNull()
    expect(validateEdgeNote('a'.repeat(LIMITS.edgeNote))).toBeNull()
    expect(validateEdgeNote('a'.repeat(LIMITS.edgeNote + 1))).toContain('200')
  })

  it('snapshot label 1-100 non-empty', () => {
    expect(validateSnapshotLabel('')).toContain('100')
    expect(validateSnapshotLabel('   ')).toBeTruthy()
    expect(validateSnapshotLabel('初查节点')).toBeNull()
    expect(validateSnapshotLabel('a'.repeat(LIMITS.snapshotLabel))).toBeNull()
    expect(validateSnapshotLabel('a'.repeat(LIMITS.snapshotLabel + 1))).toContain('100')
  })

  it('manual node kinds are exactly hypothesis and note', () => {
    expect([...MANUAL_NODE_KINDS]).toEqual(['hypothesis', 'note'])
    expect(isManualNode({
      id: 'cn_x', kind: 'hypothesis', x: 0, y: 0, props: {},
    })).toBe(true)
    expect(isManualNode({
      id: 'cn_x', kind: 'note', x: 0, y: 0, props: {},
    })).toBe(true)
    expect(isManualNode({
      id: 'fact:f1', kind: 'fact', system: true, x: 0, y: 0, props: {},
    })).toBe(false)
    // kind 非 hypothesis/note 一律不是人工节点（无论 system 标记是否缺失）
    expect(isManualNode({
      id: 'fact:f1', kind: 'fact', x: 0, y: 0, props: {},
    })).toBe(false)
  })
})
