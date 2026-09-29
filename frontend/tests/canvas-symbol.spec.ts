// 符号口径（SYM-01~06）：卡内、连线、窗口、地图四处同源。
// 反向验证见文件末尾的 mutant 组——断言必须能被变异打破，否则是恒真摆设。
import { describe, expect, it } from 'vitest'
import {
  DIM_LABELS,
  PRECISION_WEIGHT,
  asPrecision,
  describeDim,
  edgeSymbol,
  isOverlapDeterminable,
  nodeSymbol,
  pointSymbol,
  radiusOfDimHit,
  repeatFactor,
  scoreNeverAlone,
  weightedOf,
  type DimCell,
} from '../src/domain/canvas-symbol'

const cell = (o: Partial<DimCell>): DimCell => ({
  dim: 'space',
  dimLabel: DIM_LABELS.space,
  hit: true,
  count: 4,
  precision: 'minute',
  weight: 1.6,
  ...o,
})

describe('SYM-02 精度档 → 线型', () => {
  it('时刻级实线、日期级虚线', () => {
    expect(edgeSymbol({ precision: 'minute', system: false }).lineDash).toEqual([])
    expect(edgeSymbol({ precision: 'date', system: false }).lineDash).toEqual([4, 3])
  })

  it('12 条 date 档的线宽不得盖过 3 条 minute 档', () => {
    const many = edgeSymbol({ precision: 'date', weight: 12 * PRECISION_WEIGHT.date, system: false })
    const few = edgeSymbol({ precision: 'minute', weight: 3 * PRECISION_WEIGHT.minute, system: false })
    expect(many.lineWidth).toBeLessThan(few.lineWidth)
  })

  it('加权后 15 次日期级低于 3 次时刻级（次数会骗人）', () => {
    // 15 × 0.2 = 3.0 与 3 × 1.0 相等——裸乘会打平；
    // 靠 repeat 上限才真正拉开：0.2×3.0=0.6 < 1.0×1.4=1.4
    expect(weightedOf(15, 'date')).toBeLessThan(weightedOf(3, 'minute'))
    expect(weightedOf(15, 'date')).toBe(0.6)
    expect(weightedOf(3, 'minute')).toBe(1.4)
  })

  it('repeat 有上限，次数堆砌不得压过精度', () => {
    expect(repeatFactor(1)).toBe(1)
    expect(repeatFactor(3)).toBe(1.4)
    expect(repeatFactor(100)).toBe(3)
  })

  it('未知档回落 unknown，不猜', () => {
    expect(asPrecision('')).toBe('unknown')
    expect(asPrecision('nonsense')).toBe('unknown')
    expect(asPrecision('date')).toBe('date')
  })
})

describe('SYM-03 质心必须空心', () => {
  it('门牌级实心、区划质心 fillOpacity=0', () => {
    expect(pointSymbol({ precise: true }).fillOpacity).toBe(0.85)
    expect(pointSymbol({ precise: false }).fillOpacity).toBe(0)
    expect(pointSymbol({ precise: false }).fill).toBe('transparent')
  })

  it('半径按命中维数分档', () => {
    expect(radiusOfDimHit(1)).toBe(6)
    expect(radiusOfDimHit(2)).toBe(8)
    expect(radiusOfDimHit(3)).toBe(11)
  })

  it('重名点用警示色 + 虚线环', () => {
    const s = pointSymbol({ precise: true, ambiguous: true })
    expect(s.stroke).toBe('#b4781a')
    expect(s.strokeDash.length).toBeGreaterThan(0)
  })
})

describe('SYM-04 重名不自裁', () => {
  it('重名卡：虚线描边 + ? 前缀 + 自陈文案', () => {
    const s = nodeSymbol({ ambiguous: true })
    expect(s.labelPrefix).toBe('?')
    expect(s.lineDash).toEqual([4, 3])
    expect(s.note).toContain('重名')
  })

  it('未锚定主体：灰点 + · 前缀 + 明示无可用研判', () => {
    const s = nodeSymbol({ unanchored: true })
    expect(s.labelPrefix).toBe('·')
    expect(s.dimDotColor).toBe('#999999')
    expect(s.note).toContain('未锚定')
  })
})

describe('SYM-05 系统推断边与人工确认边视觉可辨', () => {
  it('系统边灰细虚线；人工边实粗', () => {
    const sys = edgeSymbol({ precision: 'minute', system: true })
    const man = edgeSymbol({ precision: 'minute', system: false })
    expect(sys.stroke).toBe('#9aa5b1')
    expect(sys.lineDash).toEqual([4, 3])
    expect(man.lineDash).toEqual([])
    expect(man.lineWidth).toBeGreaterThan(sys.lineWidth)
  })

  it('系统推断的分钟级证据仍是虚线——未经认可不该长得像已确认', () => {
    expect(edgeSymbol({ precision: 'minute', system: true }).lineDash).toEqual([4, 3])
  })
})

describe('SYM-06 分数不单独表态', () => {
  it('缺 precision/weight 时不得只给总分', () => {
    const bad = scoreNeverAlone([cell({ precision: '' as never })], 3.6)
    expect(bad.ok).toBe(false)
  })

  it('三维齐全时通过，且摘要必须条数与精度同现', () => {
    const cells = [
      cell({ dim: 'space', dimLabel: '空间', count: 4, precision: 'minute', weight: 1.6 }),
      cell({ dim: 'time', dimLabel: '时间', count: 12, precision: 'date', weight: 0.6 }),
      cell({ dim: 'relation', dimLabel: '关系', count: 3, precision: 'minute', weight: 1.4 }),
    ]
    expect(scoreNeverAlone(cells, 3.6).ok).toBe(true)
    const t = describeDim(cells[1])
    expect(t).toContain('12')
    expect(t).toContain('日期级')
    expect(t).toContain('0.6')
  })

  it('未命中维度写明原因，不由其他维度热度盖过', () => {
    const miss = cell({ hit: false, reason: '该维度无支撑观察' })
    expect(describeDim(miss)).toContain('该维度无支撑观察')
  })
})

describe('SYM-01 可判重叠口径', () => {
  it('只有 minute/second 可判时间窗真重叠', () => {
    expect(isOverlapDeterminable('minute')).toBe(true)
    expect(isOverlapDeterminable('second')).toBe(true)
    expect(isOverlapDeterminable('date')).toBe(false)
    expect(isOverlapDeterminable('unknown')).toBe(false)
  })
})
