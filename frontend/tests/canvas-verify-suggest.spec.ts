import { describe, expect, it } from 'vitest'
import type { CanvasDoc, CanvasNode } from '../src/domain/canvas'
import {
  VERIFY_CHANNEL_LABELS,
  VERIFY_PRIORITY,
  buildVerifySuggestions,
  collectAdoptedVerifyTexts,
  filterAdopted,
  makeSuggestionContext,
  sortVerifySuggestions,
  type VerifySuggestion,
} from '../src/domain/canvas-verify-suggest'

function node(
  id: string,
  kind: CanvasNode['kind'],
  props: Record<string, unknown> = {},
  extra: Partial<CanvasNode> = {},
): CanvasNode {
  return {
    id,
    kind,
    ref: `${kind}:${id}`,
    label: `节点 ${id}`,
    system: kind !== 'hypothesis' && kind !== 'note',
    pinned: false,
    x: 0,
    y: 0,
    props,
    ...extra,
  }
}

function doc(nodes: CanvasNode[], edges: CanvasDoc['edges'] = []): CanvasDoc {
  return { nodes, edges }
}

describe('VERIFY_CHANNEL_LABELS', () => {
  it('三种渠道都有中文标签', () => {
    expect(VERIFY_CHANNEL_LABELS.external).toBe('外部调取')
    expect(VERIFY_CHANNEL_LABELS.function).toBe('只读查询')
    expect(VERIFY_CHANNEL_LABELS.manual).toBe('手工跟踪')
  })
})

describe('buildVerifySuggestions 入口守卫', () => {
  it('节点不在 doc 内 → 返回空（防御性）', () => {
    const d = doc([node('r1', 'rule')])
    const n = node('orphan', 'fact')
    expect(buildVerifySuggestions(n, makeSuggestionContext(d))).toEqual([])
  })

  it('人工假设/备注 → 返回空（不该有核查建议）', () => {
    const d = doc([node('h1', 'hypothesis'), node('n1', 'note')])
    expect(buildVerifySuggestions(node('h1', 'hypothesis'), makeSuggestionContext(d))).toEqual([])
    expect(buildVerifySuggestions(node('n1', 'note'), makeSuggestionContext(d))).toEqual([])
  })

  it('rule / verify_item / evidence / function_result → 返回空（这些节点有自己的入口）', () => {
    const d = doc([
      node('r1', 'rule'),
      node('vi1', 'verify_item'),
      node('ev1', 'evidence'),
      node('fr1', 'function_result'),
    ])
    expect(buildVerifySuggestions(node('r1', 'rule'), makeSuggestionContext(d))).toEqual([])
    expect(buildVerifySuggestions(node('vi1', 'verify_item'), makeSuggestionContext(d))).toEqual([])
    expect(buildVerifySuggestions(node('ev1', 'evidence'), makeSuggestionContext(d))).toEqual([])
    expect(buildVerifySuggestions(node('fr1', 'function_result'), makeSuggestionContext(d))).toEqual([])
  })
})

describe('buildVerifySuggestions / fact', () => {
  it('基础事实：核验来源（critical 优先级）', () => {
    const f = node('f1', 'fact', { source: '转账流水表' })
    const out = buildVerifySuggestions(f, makeSuggestionContext(doc([f])))
    expect(out[0].channel).toBe('function')
    expect(out[0].priority).toBe(VERIFY_PRIORITY.critical)
    expect(out[0].text).toContain('核验事实')
    expect(out[0].text).toContain('转账流水表')
    expect(out[0].ref_function).toContain('verify_fact_source')
    expect(out[0].falsification).toBeTruthy()
  })

  it('有 timestamp 字段 → 第二条建议出现（normal）', () => {
    const f = node('f1', 'fact', { source: 'X', timestamp: '2026-09-01' })
    const out = buildVerifySuggestions(f, makeSuggestionContext(doc([f])))
    expect(out).toHaveLength(2)
    expect(out[1].text).toContain('时间戳')
    expect(out[1].ref_function).toBe('verify_fact_timewindow')
  })

  it('无 source → 退化为 manual 渠道', () => {
    const f = node('f1', 'fact')
    const out = buildVerifySuggestions(f, makeSuggestionContext(doc([f])))
    expect(out[0].channel).toBe('manual')
    expect(out[0].ref_function).toBeUndefined()
  })

  it('空 label → 兜底「该节点」字样', () => {
    const f: CanvasNode = { ...node('f1', 'fact'), label: '' }
    const out = buildVerifySuggestions(f, makeSuggestionContext(doc([f])))
    expect(out[0].text).toContain('该节点')
  })
})

describe('buildVerifySuggestions / object', () => {
  it('实体：补全与别名两条（high + normal）', () => {
    const o = node('o1', 'object', { type_title: '账户' })
    const out = buildVerifySuggestions(o, makeSuggestionContext(doc([o])))
    expect(out).toHaveLength(2)
    expect(out[0].priority).toBe(VERIFY_PRIORITY.high)
    expect(out[0].channel).toBe('manual')
    expect(out[0].text).toContain('账户')
    expect(out[1].priority).toBe(VERIFY_PRIORITY.normal)
    expect(out[1].text).toContain('别名')
  })

  it('无 type_title → 不影响输出条数（兜底为空串）', () => {
    const o = node('o1', 'object')
    const out = buildVerifySuggestions(o, makeSuggestionContext(doc([o])))
    expect(out).toHaveLength(2)
    expect(out[0].text).toContain('实体')
  })
})

describe('buildVerifySuggestions / source_row', () => {
  it('有 fields → 第一条拼接字段名', () => {
    const r = node('r1', 'source_row', {
      fields: [{ name: 'account_no' }, { name: 'amount' }, { name: 'tx_time' }],
    })
    const out = buildVerifySuggestions(r, makeSuggestionContext(doc([r])))
    expect(out[0].text).toContain('account_no')
    expect(out[0].text).toContain('amount')
    expect(out[0].text).toContain('tx_time')
    expect(out[0].ref_function).toBe('fetch_row_fields')
  })

  it('字段 > 3 → 截断 + 省略号', () => {
    const r = node('r1', 'source_row', {
      fields: [
        { name: 'a' },
        { name: 'b' },
        { name: 'c' },
        { name: 'd' },
        { name: 'e' },
      ],
    })
    const out = buildVerifySuggestions(r, makeSuggestionContext(doc([r])))
    expect(out[0].text).toContain('…')
    expect(out[0].text.split('、').length).toBeLessThanOrEqual(4) // 3 + 1 个「…」
  })

  it('无 fields → 退化文案', () => {
    const r = node('r1', 'source_row')
    const out = buildVerifySuggestions(r, makeSuggestionContext(doc([r])))
    expect(out[0].text).toContain('字段是否完整')
  })

  it('第二条是脱敏检查（manual 渠道）', () => {
    const r = node('r1', 'source_row')
    const out = buildVerifySuggestions(r, makeSuggestionContext(doc([r])))
    expect(out[1].text).toContain('脱敏')
    expect(out[1].channel).toBe('manual')
  })
})

describe('buildVerifySuggestions / 过滤与排序', () => {
  it('已采纳文本自动剔除', () => {
    // 用 source_row 这种「自然 2 条」的节点验证「剔除 1 后剩 1」
    const r = node('r1', 'source_row', { fields: [{ name: 'a' }] })
    const d1 = doc([r])
    const out1 = buildVerifySuggestions(r, makeSuggestionContext(d1))
    expect(out1).toHaveLength(2)

    // 给其中一条建议文本「预采纳」后 → 剩 1
    const adopted = new Set<string>([out1[0].text])
    const out2 = buildVerifySuggestions(r, makeSuggestionContext(d1, adopted))
    expect(out2).toHaveLength(1)
    expect(out2[0].text).not.toBe(out1[0].text)
  })

  it('同节点多次调用结果稳定（按 text 字典序排）', () => {
    const r = node('r1', 'source_row')
    const d = doc([r])
    const a = buildVerifySuggestions(r, makeSuggestionContext(d))
    const b = buildVerifySuggestions(r, makeSuggestionContext(d))
    expect(a).toEqual(b)
    // 优先级排序：high 必然在 normal 之前
    expect(a[0].priority).toBeLessThanOrEqual(a[1].priority)
  })

  it('sortVerifySuggestions 直接排序稳定', () => {
    const list: VerifySuggestion[] = [
      {
        id: 'x',
        text: 'B',
        channel: 'manual',
        priority: VERIFY_PRIORITY.normal,
        reason: '',
      },
      {
        id: 'y',
        text: 'A',
        channel: 'manual',
        priority: VERIFY_PRIORITY.normal,
        reason: '',
      },
      {
        id: 'z',
        text: 'C',
        channel: 'manual',
        priority: VERIFY_PRIORITY.critical,
        reason: '',
      },
    ]
    const out = sortVerifySuggestions(list)
    expect(out.map((s) => s.id)).toEqual(['z', 'y', 'x'])
  })

  it('filterAdopted 单独可复用', () => {
    const list: VerifySuggestion[] = [
      {
        id: 'a',
        text: '已采纳',
        channel: 'manual',
        priority: 1,
        reason: '',
      },
      {
        id: 'b',
        text: '新条目',
        channel: 'manual',
        priority: 1,
        reason: '',
      },
    ]
    expect(filterAdopted(list, new Set(['已采纳'])).map((s) => s.id)).toEqual(['b'])
  })
})

describe('collectAdoptedVerifyTexts', () => {
  it('只收 verify_item + adopted=true + 有 text', () => {
    const d = doc([
      node('vi1', 'verify_item', { text: '已核1' }, { adopted: true }),
      node('vi2', 'verify_item', { text: '未采纳' }, { adopted: false }),
      node('vi3', 'verify_item', { text: '   ' }, { adopted: true }),
      node('h1', 'hypothesis', { text: '假设' }, { adopted: true }),
      node('f1', 'fact'),
    ])
    expect([...collectAdoptedVerifyTexts(d)]).toEqual(['已核1'])
  })
})
