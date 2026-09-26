import { describe, expect, it } from 'vitest'
import {
  boundsOf,
  GEO_SKILL_SERIAL,
  GEO_SKILL_SITE,
  geocodeSourceLabel,
  mergeSites,
  parseCells,
  parseGeoObservation,
  parseSite,
  parseSites,
  probabilityBand,
  type GeoLayerModel,
} from '../src/domain/geoMap'

// PLAN-GEO-001 P4：地理画像图层模型纯函数测试（不涉及地图引擎/网络）。

/** 构造镜头观察详情最小骨架 */
function obsOf(skillId: string, detail: Record<string, unknown>, subject = '张卫国') {
  return { observation_id: 'obs-1', skill_id: skillId, subject, detail }
}

describe('parseSite', () => {
  it('正常条目解析，数字坐标字符串也接受', () => {
    const s = parseSite({
      location_id: 'loc-1',
      std_address: '宏村镇宏村',
      admin_path: '安徽省/黄山市/黟县/宏村镇',
      lat: '29.901234',
      lng: 117.987654,
      coord_sys: 'GCJ-02',
      geocode_source: 'admin_offline',
      geocode_confidence: '0.6',
      visits: 4,
      first_date: '2026-01-02',
      last_date: '2026-03-04',
      event_pks: ['tp1', 'tp2'],
    })
    expect(s).not.toBeNull()
    expect(s?.lat).toBe(29.901234)
    expect(s?.lng).toBe(117.987654)
    expect(s?.geocodeConfidence).toBe(0.6)
    expect(s?.coordDegraded).toBe(false)
    expect(s?.eventPks).toEqual(['tp1', 'tp2'])
  })

  it('坐标缺失标 coordDegraded（不丢弃条目）', () => {
    const s = parseSite({ std_address: '滨江路', lat: null, lng: null, visits: 1 })
    expect(s?.coordDegraded).toBe(true)
    expect(s?.lat).toBeNull()
  })

  it('地址与坐标全空返回 null', () => {
    expect(parseSite({ visits: 0 })).toBeNull()
    expect(parseSite(null)).toBeNull()
  })
})

describe('parseCells', () => {
  const fc = (features: unknown[]) => ({ type: 'FeatureCollection', features })

  it('解析 Polygon 概率格，坐标序 [lng,lat]', () => {
    const cells = parseCells(
      fc([
        {
          type: 'Feature',
          geometry: {
            type: 'Polygon',
            coordinates: [[[1, 2], [1.1, 2], [1.1, 2.1], [1, 2.1], [1, 2]]],
          },
          properties: { probability: 0.73 },
        },
      ]),
    )
    expect(cells).toHaveLength(1)
    expect(cells[0].ring[0]).toEqual([1, 2])
    expect(cells[0].probability).toBe(0.73)
  })

  it('非 FeatureCollection / 非 Polygon / probability<=0 / 环点不足 均跳过', () => {
    const cells = parseCells(
      fc([
        { type: 'Feature', geometry: { type: 'Point', coordinates: [1, 2] } },
        {
          type: 'Feature',
          geometry: { type: 'Polygon', coordinates: [[[1, 2], [1.1, 2]]] },
          properties: { probability: 0.5 },
        },
        {
          type: 'Feature',
          geometry: {
            type: 'Polygon',
            coordinates: [[[1, 2], [1.1, 2], [1.1, 2.1], [1, 2.1], [1, 2]]],
          },
          properties: { probability: 0 },
        },
        'junk',
      ]),
    )
    expect(cells).toHaveLength(0)
  })

  it('入参脏值不抛错', () => {
    expect(parseCells(null)).toEqual([])
    expect(parseCells({ type: 'Point' })).toEqual([])
  })
})

describe('parseGeoObservation', () => {
  it('非 geo 镜头返回 null', () => {
    expect(parseGeoObservation(obsOf('timeline_rhythm', {}))).toBeNull()
  })

  it('site 镜头：sites 解析 + {total,with_coords} 覆盖率口径', () => {
    const m = parseGeoObservation(
      obsOf(GEO_SKILL_SITE, {
        subject: { name: '张卫国', pk: 'p1', type: 'person' },
        sites: [
          { std_address: 'A 地', lat: 30, lng: 118, visits: 3 },
          { std_address: 'B 地', visits: 1 },
        ],
        coord_coverage: { total: 2, with_coords: 1 },
        degraded: true,
        degraded_reason: '1/2 个落脚点无坐标',
      }),
    )
    expect(m?.kind).toBe('site')
    expect(m?.subject).toBe('张卫国')
    expect(m?.sites).toHaveLength(2)
    expect(m?.coordCoverage).toEqual({ total: 2, withCoords: 1 })
    expect(m?.degraded).toBe(true)
  })

  it('serial 镜头：优先区排序 + geojson 格子 + 事件口径覆盖率', () => {
    const m = parseGeoObservation(
      obsOf(GEO_SKILL_SERIAL, {
        subject: { name: '李四' },
        events_total: 7,
        events_used: 6,
        events_dropped_no_coord: 1,
        grid_meters: 200,
        buffer_m: 1000,
        grid: { rows: 12, cols: 9 },
        top_zone: { rank: 1, lat: 30.1, lng: 118.1, probability: 1 },
        priority_zones: [
          { rank: 2, lat: 30.2, lng: 118.2, probability: 0.4 },
          { rank: 1, lat: 30.1, lng: 118.1, probability: 1 },
        ],
        coord_coverage: 0.8571,
        geojson: {
          type: 'FeatureCollection',
          features: [
            {
              type: 'Feature',
              geometry: {
                type: 'Polygon',
                coordinates: [[[118.1, 30.1], [118.11, 30.1], [118.11, 30.11], [118.1, 30.11], [118.1, 30.1]]],
              },
              properties: { probability: 1 },
            },
          ],
        },
      }),
    )
    expect(m?.kind).toBe('serial')
    expect(m?.zones[0].rank).toBe(1) // 按 rank 排序
    expect(m?.topZone?.lat).toBe(30.1)
    expect(m?.cells).toHaveLength(1)
    // 比率口径回退为事件计数
    expect(m?.coordCoverage).toEqual({ total: 7, withCoords: 6 })
    expect(m?.grid).toEqual({ rows: 12, cols: 9 })
  })

  it('detail 整体缺失/脏值不抛错，产出空图层', () => {
    const m = parseGeoObservation(obsOf(GEO_SKILL_SITE, {}))
    expect(m?.sites).toEqual([])
    expect(m?.zones).toEqual([])
    expect(m?.topZone).toBeNull()
    expect(m?.coordCoverage).toBeNull()
  })
})

describe('mergeSites', () => {
  const base: GeoLayerModel = {
    kind: 'serial',
    observationId: 'o1',
    skillId: GEO_SKILL_SERIAL,
    subject: '张卫国',
    sites: [
      {
        locationId: 'loc-1', stdAddress: 'A', adminPath: '', lat: 1, lng: 1,
        coordSys: null, geocodeSource: null, geocodeConfidence: null,
        coordDegraded: false, visits: 9, firstDate: null, lastDate: null, eventPks: [],
      },
    ],
    zones: [],
    cells: [],
    topZone: null,
    grid: { rows: null, cols: null },
    gridMeters: null,
    bufferM: null,
    eventsTotal: null,
    eventsUsed: null,
    eventsDroppedNoCoord: null,
    coordCoverage: null,
    degraded: false,
    degradedReason: '',
  }

  it('location_id 去重，新点按到访降序并入', () => {
    const peer = parseSites({
      sites: [
        { location_id: 'loc-1', std_address: 'A 重复', visits: 1 },
        { location_id: 'loc-2', std_address: 'B', lat: 2, lng: 2, visits: 5 },
      ],
    })
    const merged = mergeSites(base, peer)
    expect(merged.sites).toHaveLength(2)
    expect(merged.sites[0].visits).toBe(9) // 原条目保留，不被 peer 覆盖
    expect(merged.sites[1].locationId).toBe('loc-2')
  })

  it('无 location_id 时按 std_address 去重', () => {
    const merged = mergeSites(base, parseSites({ sites: [{ std_address: 'A', visits: 2 }] }))
    expect(merged.sites).toHaveLength(1)
  })
})

describe('probabilityBand 边界', () => {
  it.each([
    [0, 0],
    [0.19, 0],
    [0.2, 1],
    [0.4, 2],
    [0.6, 3],
    [0.8, 4],
    [1, 4],
  ])('p=%s → 档 %s', (p, expected) => {
    expect(probabilityBand(p)).toBe(expected)
  })
})

describe('boundsOf', () => {
  function modelWith(cells: number[][][], sites: Array<{ lat: number | null; lng: number | null }>) {
    return parseGeoObservation(
      obsOf(GEO_SKILL_SERIAL, {
        geojson: {
          type: 'FeatureCollection',
          features: cells.map((ring) => ({
            type: 'Feature',
            geometry: { type: 'Polygon', coordinates: [ring] },
            properties: { probability: 0.5 },
          })),
        },
        sites: sites.map((s) => ({ std_address: 'x', lat: s.lat, lng: s.lng })),
      }),
    ) as GeoLayerModel
  }

  it('无任何坐标返回 null', () => {
    const m = parseGeoObservation(obsOf(GEO_SKILL_SITE, { sites: [{ std_address: '无坐标' }] }))
    expect(boundsOf(m as GeoLayerModel)).toBeNull()
  })

  it('单点退化给 ~500m 半框（min<max）', () => {
    const ring = [[118, 30], [118, 30], [118, 30], [118, 30], [118, 30]]
    const m = modelWith([ring], [])
    const b = boundsOf(m)
    expect(b).not.toBeNull()
    expect(b!.maxLat).toBeGreaterThan(b!.minLat)
  })
})

describe('geocodeSourceLabel', () => {
  it('已知来源中文标签，未知回落原值', () => {
    expect(geocodeSourceLabel('gaode')).toBe('高德在线')
    expect(geocodeSourceLabel('admin_offline')).toBe('区划离线质心')
    expect(geocodeSourceLabel('manual')).toBe('人工标注')
    expect(geocodeSourceLabel('weird')).toBe('weird')
    expect(geocodeSourceLabel(null)).toBe('未知')
  })
})
