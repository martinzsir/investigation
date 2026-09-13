import { describe, expect, it } from 'vitest'
import {
  VIEWPORT_MIN_ZOOM,
  contentBox,
  contentCenter,
  fitScale,
  laneGeometry,
  minimapFrame,
  minimapProject,
  minimapUnproject,
  viewportRect,
  zoomPercent,
} from '../src/domain/canvas-viewport'

describe('contentBox', () => {
  it('returns null for empty input', () => {
    expect(contentBox([])).toBeNull()
  })

  it('expands by card size around node center', () => {
    const box = contentBox([{ x: 0, y: 0 }, { x: 960, y: 208 }])
    expect(box).not.toBeNull()
    // 卡片 186×50：左右各 93、上下各 25
    expect(box?.minX).toBe(-93)
    expect(box?.maxX).toBe(1053)
    expect(box?.minY).toBe(-25)
    expect(box?.maxY).toBe(233)
  })

  it('ignores non-finite coordinates', () => {
    const box = contentBox([
      { x: 0, y: 0 },
      { x: Number.NaN, y: Number.NaN },
    ])
    expect(box?.minX).toBe(-93)
    expect(box?.maxX).toBe(93)
  })
})

describe('fitScale', () => {
  const box = contentBox([{ x: 0, y: 0 }, { x: 960, y: 900 }])

  it('never zooms below the readable floor', () => {
    // 视口远小于内容：按原 fit 逻辑会缩到 0.1，这里被 clamp 到可读下限
    expect(fitScale(box, 300, 200)).toBe(VIEWPORT_MIN_ZOOM)
  })

  it('never zooms above 1 on initial render', () => {
    expect(fitScale(contentBox([{ x: 0, y: 0 }]), 1920, 1080)).toBe(1)
  })

  it('fits large content into the viewport', () => {
    const s = fitScale(box, 1200, 700)
    expect(s).toBeGreaterThanOrEqual(VIEWPORT_MIN_ZOOM)
    expect(s).toBeLessThanOrEqual(1)
    expect((box!.maxX - box!.minX) * s).toBeLessThanOrEqual(1200)
  })

  it('falls back to 1 without box or viewport', () => {
    expect(fitScale(null, 1000, 600)).toBe(1)
    expect(fitScale(box, 0, 0)).toBe(1)
  })
})

describe('contentCenter', () => {
  it('returns box center', () => {
    expect(contentCenter({ minX: -93, minY: -25, maxX: 1053, maxY: 233 }))
      .toEqual([480, 104])
  })
  it('returns origin without box', () => {
    expect(contentCenter(null)).toEqual([0, 0])
  })
})

describe('laneGeometry', () => {
  it('keeps the head above content and the band around it', () => {
    const box = contentBox([{ x: 0, y: 0 }, { x: 960, y: 208 }])
    const geo = laneGeometry(box)
    // 列头底边应在内容顶边之上
    expect(geo.headY + 26 / 2).toBeLessThan(box!.minY)
    // 色带必须覆盖整段内容
    expect(geo.bandCenterY - geo.bandHeight / 2).toBeLessThanOrEqual(box!.minY)
    expect(geo.bandCenterY + geo.bandHeight / 2).toBeGreaterThanOrEqual(box!.maxY)
  })

  it('has sane defaults without content', () => {
    const geo = laneGeometry(null)
    expect(geo.bandHeight).toBeGreaterThan(0)
    expect(Number.isFinite(geo.headY)).toBe(true)
  })
})

describe('zoomPercent', () => {
  it('rounds to percent', () => {
    expect(zoomPercent(0.7)).toBe(70)
    expect(zoomPercent(1)).toBe(100)
  })
  it('falls back to 100 for invalid zoom', () => {
    expect(zoomPercent(Number.NaN)).toBe(100)
    expect(zoomPercent(0)).toBe(100)
  })
})

describe('viewportRect', () => {
  it('把视口像素转成画布坐标：宽高 = 视口 / zoom', () => {
    const r = viewportRect([100, 200], 1000, 600, 2)
    expect(r).toEqual({ x: 100, y: 200, w: 500, h: 300 })
  })
  it('返回 null 当参数非法', () => {
    expect(viewportRect(null, 1000, 600, 1)).toBeNull()
    expect(viewportRect([0, 0], 0, 600, 1)).toBeNull()
    expect(viewportRect([Number.NaN, 0], 1000, 600, 1)).toBeNull()
    expect(viewportRect([0, 0], 1000, 600, 0)).toBeNull()
  })
})

describe('minimapFrame', () => {
  const box = contentBox([{ x: 0, y: 0 }, { x: 960, y: 200 }])

  it('等比映射、内容居中', () => {
    const f = minimapFrame(box, 200, 120)!
    // 内容宽 = 93 + 960 + 93 = 1146；高 = 25 + 200 + 25 = 250
    expect(f.offX).toBeCloseTo((200 - 1146 * f.scale) / 2, 5)
    expect(f.offY).toBeCloseTo((120 - 250 * f.scale) / 2, 5)
    // 上下边界留 6px、左右也留 6px
    expect(1146 * f.scale).toBeLessThanOrEqual(200 - 12 + 0.001)
    expect(250 * f.scale).toBeLessThanOrEqual(120 - 12 + 0.001)
  })
  it('无 bbox 或尺寸返回 null', () => {
    expect(minimapFrame(null, 200, 120)).toBeNull()
    expect(minimapFrame(box, 0, 0)).toBeNull()
  })
})

describe('minimapProject / minimapUnproject', () => {
  const box = contentBox([{ x: 0, y: 0 }, { x: 960, y: 200 }])!
  const f = minimapFrame(box, 200, 120)!

  it('project 画布左上 → 缩略图原点附近', () => {
    const [px, py] = minimapProject(f, box, box.minX, box.minY)
    expect(px).toBeCloseTo(f.offX, 5)
    expect(py).toBeCloseTo(f.offY, 5)
  })
  it('project 画布右上 → 横向极右', () => {
    const [px] = minimapProject(f, box, box.maxX, box.minY)
    expect(px).toBeCloseTo(f.offX + (box.maxX - box.minX) * f.scale, 5)
  })
  it('unproject 是 project 的逆：往返回到原坐标', () => {
    for (const x of [0, 480, 960]) {
      for (const y of [0, 100, 200]) {
        const [px, py] = minimapProject(f, box, x, y)
        const [cx, cy] = minimapUnproject(f, box, px, py)
        expect(cx).toBeCloseTo(x, 5)
        expect(cy).toBeCloseTo(y, 5)
      }
    }
  })
  it('frame 为 null 时 project/unproject 兜底返回 [0,0]', () => {
    expect(minimapProject(null, box, 1, 2)).toEqual([0, 0])
    expect(minimapUnproject(null, box, 1, 2)).toEqual([0, 0])
  })
})
