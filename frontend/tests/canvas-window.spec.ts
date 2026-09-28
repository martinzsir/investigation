import { describe, expect, it } from 'vitest'
import { NODE_KINDS } from '../src/domain/canvas'
import {
  AMBIGUOUS_BORDER,
  hasWindow,
  parseTimeMs,
  precisionSymbol,
  timeAxisRangeOf,
  timeBarSpec,
  windowEnlargeRoute,
  windowFor,
  windowPosition,
  type WindowKind,
} from '../src/domain/canvas-window'

// P0 符号口径（PRD V1.0.0 功能 2 / REQ-P0-01、02）：
// 纯函数测试，不涉及组件渲染。红线 R1 的反向验证写死在「date 档绝不允许实线/实心」用例——
// 若有人把 date 档映射改成实线，该用例必须失败。

describe('precisionSymbol（红线 R1：精度档符号单一来源）', () => {
  it('minute 档：实线 + 实心 + 粗边（可判时间窗真重叠）', () => {
    const s = precisionSymbol('minute')
    expect(s.lineDash).toEqual([])
    expect(s.fillMode).toBe('solid')
    expect(s.strokeWidth).toBeGreaterThan(precisionSymbol('date').strokeWidth)
    expect(s.visible).toBe(true)
  })

  it('date 档：虚线 + 空心 + 细边（仅同地异时，非同时）', () => {
    const s = precisionSymbol('date')
    expect(s.lineDash.length).toBeGreaterThan(0)
    expect(s.fillMode).toBe('hollow')
    expect(s.visible).toBe(true)
  })

  it('反向验证：date 档绝不允许实线/实心（改映射本用例必失败）', () => {
    const d = precisionSymbol('date')
    expect(d.lineDash.length).toBeGreaterThan(0)
    expect(d.fillMode).not.toBe('solid')
    // 两档必须可一眼区分：线型与填充至少一项不同
    const m = precisionSymbol('minute')
    expect(m.lineDash).not.toEqual(d.lineDash)
    expect(m.fillMode).not.toEqual(d.fillMode)
  })

  it('null/undefined/脏值降级为弱化空心、visible=false、不抛错', () => {
    for (const bad of [null, undefined, '', 'hour', 3, {}]) {
      const s = precisionSymbol(bad)
      expect(s.fillMode).toBe('hollow')
      expect(s.visible).toBe(false)
      expect(s.visible).not.toBe(true) // 渲染方据此跳过绘制
    }
    // 脏数据降级规格不得与 date 档混淆：颜色与线型均不同
    const none = precisionSymbol(null)
    const date = precisionSymbol('date')
    expect(none.color).not.toBe(date.color)
    expect(none.lineDash).not.toEqual(date.lineDash)
  })
})

describe('windowFor（节点 kind → 窗口类型路由）', () => {
  const VALID: ReadonlySet<WindowKind> = new Set([
    'relation',
    'map',
    'time',
    'evidence',
    'hypothesis',
    'source',
  ])

  it('四类研判节点路由正确', () => {
    expect(windowFor('subject')).toBe('relation')
    expect(windowFor('place')).toBe('map')
    expect(windowFor('event')).toBe('time')
    expect(windowFor('analysis_result')).toBe('evidence')
  })

  it('hypothesis → 假设窗口；fact / source_row → 溯源窗口', () => {
    expect(windowFor('hypothesis')).toBe('hypothesis')
    expect(windowFor('fact')).toBe('source')
    expect(windowFor('source_row')).toBe('source')
  })

  it('其余既有 kind 返回 null（沿用既有溯源弹层）', () => {
    for (const k of ['rule', 'object', 'source_file', 'verify_item', 'evidence', 'note', 'function_result']) {
      expect(windowFor(k)).toBeNull()
    }
  })

  it('未知 kind / 非字符串返回 null 不抛错', () => {
    for (const bad of ['foo', '', null, undefined, 42, {}]) {
      expect(windowFor(bad)).toBeNull()
    }
  })

  it('全 kind 覆盖：P1 后 NODE_KINDS 即 14 类（既有 10 + 研判 4），结果恒为合法窗口或 null', () => {
    expect(NODE_KINDS.length).toBe(14)
    for (const k of NODE_KINDS) {
      const w = windowFor(k)
      expect(w === null || VALID.has(w)).toBe(true)
    }
  })
})

describe('AMBIGUOUS_BORDER（红线 R2：重名绝不自裁）', () => {
  it('虚线边框 + ? 标记', () => {
    expect(AMBIGUOUS_BORDER.lineDash.length).toBeGreaterThan(0)
    expect(AMBIGUOUS_BORDER.marker).toBe('?')
    expect(AMBIGUOUS_BORDER.strokeWidth).toBeGreaterThan(0)
  })

  it('与精度档 date 虚线可区分（线型数组不同，不混淆两种语义）', () => {
    expect(AMBIGUOUS_BORDER.lineDash).not.toEqual(precisionSymbol('date').lineDash)
  })
})

// ======================================================================
// P2 窗口框架纯函数（PRD 功能 2/3：卡内迷你符号 + 贴附浮窗）
// ======================================================================

describe('parseTimeMs（宽容时间解析，脏数据静默降级）', () => {
  it('合法时间串 → 毫秒', () => {
    expect(parseTimeMs('2026-01-02')).toBe(Date.parse('2026-01-02'))
    expect(parseTimeMs('2026-01-02 08:30:00')).toBe(
      Date.parse('2026-01-02T08:30:00'),
    )
  })

  it('空串/非法串/非字符串 → null，不抛错', () => {
    for (const bad of ['', '   ', 'not-a-time', '2026-13-99', null, undefined, 42, {}]) {
      expect(parseTimeMs(bad)).toBeNull()
    }
  })
})

describe('timeAxisRangeOf（案件时间轴 = 全节点时间极值）', () => {
  it('取 time_start/time_end 的全局极值（props 嵌套形态）', () => {
    const range = timeAxisRangeOf([
      { props: { time_start: '2026-01-02' } },
      { props: { time_start: '2026-01-10', time_end: '2026-01-12' } },
      { props: {} }, // 无时间的节点不参与
    ])
    expect(range).toEqual({
      startMs: Date.parse('2026-01-02'),
      endMs: Date.parse('2026-01-12'),
    })
  })

  it('兼容平铺形态（无 props 包装）', () => {
    const range = timeAxisRangeOf([
      { time_start: '2026-03-01' },
      { time_start: '2026-02-01' },
    ])
    expect(range).toEqual({
      startMs: Date.parse('2026-02-01'),
      endMs: Date.parse('2026-03-01'),
    })
  })

  it('无任何合法时间 / 单一时间点（max≤min）→ null', () => {
    expect(timeAxisRangeOf([{ props: { time_start: 'garbage' } }])).toBeNull()
    expect(timeAxisRangeOf([])).toBeNull()
    expect(timeAxisRangeOf([{ props: { time_start: '2026-01-02' } }])).toBeNull()
  })
})

describe('timeBarSpec（卡内时间条几何；R1：符号只经 precisionSymbol 派生）', () => {
  const AXIS = { startMs: Date.parse('2026-01-01'), endMs: Date.parse('2026-01-11') }

  it('minute 档：start/span 为轴上 0~1 比例，符号实心可见', () => {
    const bar = timeBarSpec(
      { time_start: '2026-01-02', time_end: '2026-01-04', time_precision: 'minute' },
      AXIS,
    )
    expect(bar).not.toBeNull()
    expect(bar!.start).toBeCloseTo(0.1)
    expect(bar!.span).toBeCloseTo(0.2)
    expect(bar!.visible).toBe(true)
    expect(bar!.symbol.fillMode).toBe('solid')
  })

  it('date 档符号为虚线空心（与 minute 档在条上一眼可分）', () => {
    const bar = timeBarSpec(
      { time_start: '2026-01-02', time_precision: 'date' },
      AXIS,
    )
    expect(bar!.symbol.fillMode).toBe('hollow')
    expect(bar!.symbol.lineDash.length).toBeGreaterThan(0)
    // 无合法 time_end → span=0，只画时点标记
    expect(bar!.span).toBe(0)
  })

  it('脏精度档随符号降级：条位置照算但 visible=false（渲染方跳过）', () => {
    const bar = timeBarSpec(
      { time_start: '2026-01-02', time_precision: 'hour' },
      AXIS,
    )
    expect(bar!.start).toBeCloseTo(0.1)
    expect(bar!.visible).toBe(false)
  })

  it('越界时间 clamp 到 [0,1]；time_end 早于 start 时 span=0', () => {
    const clamped = timeBarSpec(
      { time_start: '2025-01-01', time_precision: 'minute' },
      AXIS,
    )
    expect(clamped!.start).toBe(0)
    const inverted = timeBarSpec(
      { time_start: '2026-01-05', time_end: '2026-01-02', time_precision: 'minute' },
      AXIS,
    )
    expect(inverted!.span).toBe(0)
  })

  it('无 time_start / 轴缺失 / 轴倒挂 → null（不画占位误导）', () => {
    expect(timeBarSpec({ time_precision: 'minute' }, AXIS)).toBeNull()
    expect(timeBarSpec({ time_start: '2026-01-02' }, null)).toBeNull()
    expect(
      timeBarSpec(
        { time_start: '2026-01-02' },
        { startMs: AXIS.endMs, endMs: AXIS.startMs },
      ),
    ).toBeNull()
  })
})

describe('hasWindow（「窗口」按钮显示口径 = 研判 5 类）', () => {
  it('研判 5 类可开窗', () => {
    for (const k of ['subject', 'place', 'event', 'analysis_result', 'hypothesis']) {
      expect(hasWindow(k)).toBe(true)
    }
  })

  it('note / 溯源类不开窗（fact 在路由表但案件画布不含该 kind）', () => {
    for (const k of ['note', 'fact', 'source_row', 'rule', 'evidence']) {
      expect(hasWindow(k)).toBe(false)
    }
    expect(hasWindow('foo')).toBe(false)
    expect(hasWindow(null)).toBe(false)
  })
})

describe('windowEnlargeRoute（放大入口 → 全局视图路由）', () => {
  it('关系→图谱、地图→地理画像、时间/证据→三维交汇', () => {
    expect(windowEnlargeRoute('relation')).toBe('/c/graph')
    expect(windowEnlargeRoute('map')).toBe('/c/geo')
    expect(windowEnlargeRoute('time')).toBe('/c/convergence')
    expect(windowEnlargeRoute('evidence')).toBe('/c/convergence')
  })

  it('假设窗口无全局视图（画布本域对象）、source/null → 空串不渲染按钮', () => {
    expect(windowEnlargeRoute('hypothesis')).toBe('')
    expect(windowEnlargeRoute('source')).toBe('')
    expect(windowEnlargeRoute(null)).toBe('')
  })
})

describe('windowPosition（贴附定位：右默认、越界翻左、clamp 兜底）', () => {
  const BASE = { viewportW: 800, viewportH: 600, winW: 300, winH: 320 }

  it('右侧放得下 → 贴右侧、垂直居中', () => {
    const p = windowPosition({ ...BASE, anchorX: 300, anchorY: 300 })
    expect(p.side).toBe('right')
    expect(p.x).toBe(300 + 93 + 12) // nodeHalfW + margin
    expect(p.y).toBe(300 - 320 / 2)
  })

  it('右侧越界、左侧放得下 → 翻转左侧', () => {
    const p = windowPosition({ ...BASE, anchorX: 600, anchorY: 300 })
    expect(p.side).toBe('left')
    expect(p.x).toBe(600 - 93 - 12 - 300)
  })

  it('两侧都放不下 → clamp 在视口右缘内侧', () => {
    const p = windowPosition({ ...BASE, anchorX: 400, anchorY: 300 })
    expect(p.x).toBe(800 - 300) // clamp 到右缘
    expect(p.y).toBe(300 - 160)
  })

  it('垂直越界 clamp 进视口（下缘/上缘）', () => {
    const low = windowPosition({ ...BASE, anchorX: 300, anchorY: 590 })
    expect(low.y).toBe(600 - 320)
    const high = windowPosition({ ...BASE, anchorX: 300, anchorY: 20 })
    expect(high.y).toBe(0)
  })

  it('窗口高于视口 → y=0；NaN/负尺寸入参不抛错', () => {
    expect(windowPosition({ ...BASE, viewportH: 200, anchorY: 100 }).y).toBe(0)
    expect(() =>
      windowPosition({ ...BASE, anchorX: NaN, anchorY: NaN, winW: -5, winH: -1 }),
    ).not.toThrow()
  })
})
