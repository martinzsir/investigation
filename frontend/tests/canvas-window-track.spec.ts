// 物品轨迹在地图窗口的呈现（WIN-04 扩展 / E14）。
//
// 沙盒装不上 vitest，但断言必须真跑——"看着没问题"不算验证。本 spec 只
// 用 .tsenv/shim/vitest.ts 已实现的 matcher。
import { describe, expect, it } from 'vitest'
import {
  buildMapWindow,
  toTrackGeoModel,
  type MapTrackDot,
  type WindowNode,
} from '../src/domain/canvas-window'

const N = (id: string, kind: string, label: string, props?: Record<string, unknown>): WindowNode => ({
  id,
  kind,
  label,
  props,
})

/** 后端 canvas_item_source._track_point 的产出形态 */
const TP = (
  at: string,
  lat: number | null,
  lng: number | null,
  precision: string | null,
  location = '浙江省杭州市拱墅区莫干山路111号',
) => ({ date: at.slice(0, 10), timestamp: at || null, location, lat, lng, time_precision: precision })

const MOGAN = { lat: 30.3147, lng: 120.1501 }

describe('物品轨迹 → 地图窗口', () => {
  it('有轨迹时解析出可落图点，并按时刻升序', () => {
    const node = N('i1', 'item', '黑色别克轿车', {
      lat: MOGAN.lat,
      lng: MOGAN.lng,
      track: [
        TP('2021-09-22 16:50:00', MOGAN.lat, MOGAN.lng, 'minute'),
        TP('2021-09-08 14:35:00', MOGAN.lat, MOGAN.lng, 'minute'),
        TP('2021-09-15 14:20:00', MOGAN.lat, MOGAN.lng, 'minute'),
      ],
    })
    const m = buildMapWindow(node)
    expect(m.track).toHaveLength(3)
    expect(m.track[0].at).toBe('2021-09-08 14:35:00')
    expect(m.track[2].at).toBe('2021-09-22 16:50:00')
    expect(m.trackUnmappable).toBe(0)
    expect(m.trackNote).toContain('轨迹 3 点')
  })

  it('分钟级标 determinable；无时刻/整点档不可据此判时段', () => {
    const node = N('i2', 'item', '车', {
      lat: MOGAN.lat,
      lng: MOGAN.lng,
      track: [
        TP('2021-09-08 14:35:00', MOGAN.lat, MOGAN.lng, 'minute'),
        TP('2021-09-29', MOGAN.lat, MOGAN.lng, null),
        TP('2021-09-27 10:00:00', MOGAN.lat, MOGAN.lng, 'hour'),
      ],
    })
    const m = buildMapWindow(node)
    const byAt = new Map(m.track.map((t) => [t.at, t]))
    expect(byAt.get('2021-09-08 14:35:00')?.determinable).toBe(true)
    // 无时刻 → 判不出"下午"
    expect(byAt.get('2021-09-29')?.determinable).toBe(false)
    // 整点派生为 hour 档，同样判不出
    expect(byAt.get('2021-09-27 10:00:00')?.determinable).toBe(false)
  })

  it('坐标为 0 视为补 0 哨兵，剔除并计入未落图', () => {
    const node = N('i3', 'item', '车', {
      lat: MOGAN.lat,
      lng: MOGAN.lng,
      track: [TP('2021-09-08 14:35:00', 0, 0, 'minute')],
    })
    const m = buildMapWindow(node)
    expect(m.track).toHaveLength(0)
    expect(m.trackUnmappable).toBe(1)
    expect(m.trackNote).toContain('无可用坐标')
  })

  it('缺坐标的点不画，但必须报条数（不能静默当都画上了）', () => {
    const node = N('i4', 'item', '耗材批次', {
      track: [
        TP('2021-03-05 10:15:00', null, null, 'minute', ''),
        TP('2021-03-06 10:15:00', MOGAN.lat, MOGAN.lng, 'minute'),
      ],
    })
    const m = buildMapWindow(node)
    expect(m.track).toHaveLength(1)
    expect(m.trackUnmappable).toBe(1)
  })

  it('节点自身无坐标但轨迹有坐标时，仍按轨迹呈现且写明', () => {
    const node = N('i5', 'item', '银色U盘', {
      track: [TP('2020-03-17 15:20:00', MOGAN.lat, MOGAN.lng, 'minute')],
    })
    const m = buildMapWindow(node)
    expect(m.point).toBeNull()
    expect(m.track).toHaveLength(1)
    expect(m.unmappable).toContain('仅按轨迹点呈现')
  })

  it('无轨迹节点不受影响，track 为空数组', () => {
    const m = buildMapWindow(N('p1', 'place', '莫干山路', { lat: MOGAN.lat, lng: MOGAN.lng }))
    expect(m.track).toHaveLength(0)
    expect(m.trackNote).toBe('')
    expect(m.point?.address).toContain('莫干山路')
  })

  it('在线引擎模型：有轨迹时多点并入，避免授权后轨迹消失', () => {
    const node = N('i6', 'item', '车', {
      lat: MOGAN.lat,
      lng: MOGAN.lng,
      track: [
        TP('2021-09-08 14:35:00', MOGAN.lat, MOGAN.lng, 'minute'),
        TP('2021-09-15 14:20:00', 30.281, 120.132, 'minute'),
      ],
    })
    const m = buildMapWindow(node)
    const g = toTrackGeoModel(m.point, m.track)
    expect(g === null).toBe(false)
    // 2 个轨迹点 + 1 个节点自身位置
    expect(g?.points).toHaveLength(3)
    expect(g?.note).toContain('不表达移动路径')
    // bounds 必须覆盖全部点，否则有点画到框外
    expect(g?.bounds.minLat as number).toBeLessThan(30.281)
    expect(g?.bounds.maxLng as number).toBeGreaterThan(120.1501)
  })

  it('空轨迹时多点模型返回 null，回落单点', () => {
    const m = buildMapWindow(N('p2', 'place', '莫干山路', { lat: MOGAN.lat, lng: MOGAN.lng }))
    expect(toTrackGeoModel(m.point, m.track)).toBeNull()
  })
})
