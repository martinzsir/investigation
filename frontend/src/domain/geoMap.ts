// 地理画像图层领域模型（PLAN-GEO-001 P4）。
//
// 数据全部来自既有「观察详情」端点的 detail（geo_* 镜头产物），本文件只做
// 只读解析与派生，不发请求、不碰地图引擎——三个渲染器（高德/Leaflet/离线 SVG）
// 消费同一份 GeoLayerModel，保证图层语义与渲染技术解耦。
//
// 坐标纪律：全线 GCJ-02（obj_location.coord_sys 声明），直叠高德底图无偏移；
// 本文件不做任何 WGS84 转换。GeoJSON 坐标序为 [lng, lat]。

import type { ObservationDetailResult } from '../api/endpoints/observations'

/** P3 地理镜头包的两个 skill_id（packs/geo/pack.json 同源值） */
export const GEO_SKILL_SITE = 'geo_site_profile'
export const GEO_SKILL_SERIAL = 'geo_serial_profile'

export type GeoLayerKind = 'site' | 'serial'

/** 落脚点（geo_subject_sites 产物条目；坐标缺失时 coordDegraded=true） */
export interface GeoSite {
  locationId: string | null
  stdAddress: string
  adminPath: string
  lat: number | null
  lng: number | null
  coordSys: string | null
  geocodeSource: string | null
  geocodeConfidence: number | null
  coordDegraded: boolean
  visits: number
  firstDate: string | null
  lastDate: string | null
  eventPks: string[]
}

/** 优先排查网格（geo_profile_cgt.priority_zones 条目） */
export interface GeoZone {
  rank: number
  lat: number
  lng: number
  probability: number
}

/** 概率面格子（GeoJSON Polygon → 渲染器无关的环结构） */
export interface GeoCell {
  /** Polygon 外环：[[lng, lat], ...]（首尾闭合） */
  ring: Array<[number, number]>
  probability: number
}

export interface GeoCoordCoverage {
  total: number
  withCoords: number
}

/** 一次地理画像观察解析出的完整图层模型 */
export interface GeoLayerModel {
  kind: GeoLayerKind
  observationId: string
  skillId: string
  subject: string
  /** 落脚点（site 观察原生；serial 观察由同主体 site 观察并轨补入） */
  sites: GeoSite[]
  /** 优先排查区（serial 观察） */
  zones: GeoZone[]
  /** 概率面多边形（serial 观察 detail.geojson） */
  cells: GeoCell[]
  topZone: GeoZone | null
  grid: { rows: number | null; cols: number | null }
  gridMeters: number | null
  bufferM: number | null
  eventsTotal: number | null
  eventsUsed: number | null
  eventsDroppedNoCoord: number | null
  coordCoverage: GeoCoordCoverage | null
  degraded: boolean
  degradedReason: string
}

function rec(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
}

function num(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) {
    return Number(v)
  }
  return null
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

function bool(v: unknown): boolean {
  return v === true
}

/** 解析单条落脚点（geo_subject_sites sites[] 条目），脏值降级不抛错 */
export function parseSite(raw: unknown): GeoSite | null {
  const r = rec(raw)
  const lat = num(r.lat)
  const lng = num(r.lng)
  const stdAddress = str(r.std_address)
  if (!stdAddress && lat === null && lng === null) return null
  return {
    locationId: r.location_id ? str(r.location_id) : null,
    stdAddress,
    adminPath: str(r.admin_path),
    lat,
    lng,
    coordSys: r.coord_sys ? str(r.coord_sys) : null,
    geocodeSource: r.geocode_source ? str(r.geocode_source) : null,
    geocodeConfidence: num(r.geocode_confidence),
    coordDegraded: bool(r.coord_degraded) || lat === null || lng === null,
    visits: num(r.visits) ?? 0,
    firstDate: r.first_date ? str(r.first_date) : null,
    lastDate: r.last_date ? str(r.last_date) : null,
    eventPks: Array.isArray(r.event_pks) ? r.event_pks.map(str) : [],
  }
}

/** 从任意 geo 观察 detail 提取落脚点列表（serial detail 本身不含 sites） */
export function parseSites(detail: Record<string, unknown>): GeoSite[] {
  const raw = detail.sites
  if (!Array.isArray(raw)) return []
  return raw.map(parseSite).filter((s): s is GeoSite => s !== null)
}

function parseZone(raw: unknown): GeoZone | null {
  const r = rec(raw)
  const lat = num(r.lat)
  const lng = num(r.lng)
  const probability = num(r.probability)
  if (lat === null || lng === null || probability === null) return null
  return { rank: num(r.rank) ?? 0, lat, lng, probability }
}

/**
 * 概率面 GeoJSON → GeoCell[]。
 * 只消费 Polygon（CGT 产出物保证）；坐标序 [lng, lat]，非数组/越界结构跳过，
 * 不让单个脏格子炸掉整图。
 */
export function parseCells(geojson: unknown): GeoCell[] {
  const fc = rec(geojson)
  if (str(fc.type) !== 'FeatureCollection' || !Array.isArray(fc.features)) {
    return []
  }
  const out: GeoCell[] = []
  for (const f0 of fc.features) {
    const f = rec(f0)
    const g = rec(f.geometry)
    if (str(g.type) !== 'Polygon') continue
    const polys = g.coordinates
    if (!Array.isArray(polys) || !Array.isArray(polys[0])) continue
    const ring: Array<[number, number]> = []
    for (const pt of polys[0]) {
      if (!Array.isArray(pt) || pt.length < 2) continue
      const lng = num(pt[0])
      const lat = num(pt[1])
      if (lng === null || lat === null) continue
      ring.push([lng, lat])
    }
    if (ring.length < 3) continue
    const probability = num(rec(f.properties).probability)
    if (probability === null || probability <= 0) continue
    out.push({ ring, probability })
  }
  return out
}

/**
 * 兼容两种 coord_coverage 口径：
 *  - site 镜头：{total, with_coords}（地点口径）
 *  - serial 镜头：比率小数（事件口径），配合 events_total/events_used 还原计数
 */
function parseCoverage(detail: Record<string, unknown>): GeoCoordCoverage | null {
  const c = detail.coord_coverage
  if (c && typeof c === 'object') {
    const r = c as Record<string, unknown>
    const total = num(r.total)
    const withCoords = num(r.with_coords)
    if (total !== null && withCoords !== null) {
      return { total, withCoords: withCoords }
    }
  }
  const total = num(detail.events_total)
  const used = num(detail.events_used)
  if (total !== null && used !== null) return { total, withCoords: used }
  return null
}

/**
 * 观察详情 → 图层模型。skill_id 非 geo_* 返回 null（调用方据此忽略）。
 * 解析全程容错：镜头产物演进时旧观察缺字段只降级为空图层，不抛异常。
 */
export function parseGeoObservation(
  obs: Pick<ObservationDetailResult, 'observation_id' | 'skill_id' | 'subject' | 'detail'>,
): GeoLayerModel | null {
  const skillId = str(obs.skill_id)
  const kind: GeoLayerKind | null =
    skillId === GEO_SKILL_SITE
      ? 'site'
      : skillId === GEO_SKILL_SERIAL
        ? 'serial'
        : null
  if (kind === null) return null

  const detail = rec(obs.detail)
  const subjectInDetail = rec(detail.subject)
  const subject = str(subjectInDetail.name) || str(obs.subject)

  const sites = kind === 'site' ? parseSites(detail) : []
  const zones: GeoZone[] = []
  if (Array.isArray(detail.priority_zones)) {
    for (const z of detail.priority_zones.map(parseZone)) {
      if (z) zones.push(z)
    }
    zones.sort((a, b) => a.rank - b.rank)
  }
  const cells = kind === 'serial' ? parseCells(detail.geojson) : []
  const topRaw = parseZone(detail.top_zone)
  const gridRec = rec(detail.grid)

  return {
    kind,
    observationId: str(obs.observation_id),
    skillId,
    subject,
    sites,
    zones,
    cells,
    topZone: topRaw ?? (zones[0] ? zones[0] : null),
    grid: { rows: num(gridRec.rows), cols: num(gridRec.cols) },
    gridMeters: num(detail.grid_meters),
    bufferM: num(detail.buffer_m),
    eventsTotal: num(detail.events_total),
    eventsUsed: num(detail.events_used),
    eventsDroppedNoCoord: num(detail.events_dropped_no_coord),
    coordCoverage: parseCoverage(detail),
    degraded: bool(detail.degraded),
    degradedReason: str(detail.degraded_reason),
  }
}

/**
 * serial 观察本身只带事件引用（event_pks）不带坐标点；同主体的 site 观察
 * 持有归并后的落脚点坐标。视图层选中 serial 观察时调本函数把同主体
 * 落脚点并轨进图层（去重键：location_id > std_address）。
 */
export function mergeSites(model: GeoLayerModel, peer: GeoSite[]): GeoLayerModel {
  // 双索引：location_id 与 std_address 任一命中即视为同一落脚点——
  // 并轨两侧可能一侧有代理键、另一侧只有文本地址（serial 并 site 观察时）。
  const seenIds = new Set<string>()
  const seenAddrs = new Set<string>()
  for (const s of model.sites) {
    if (s.locationId) seenIds.add(s.locationId)
    if (s.stdAddress) seenAddrs.add(s.stdAddress)
  }
  const merged = [...model.sites]
  for (const s of peer) {
    if ((s.locationId && seenIds.has(s.locationId))
      || (s.stdAddress && seenAddrs.has(s.stdAddress))) {
      continue
    }
    if (s.locationId) seenIds.add(s.locationId)
    if (s.stdAddress) seenAddrs.add(s.stdAddress)
    merged.push(s)
  }
  merged.sort((a, b) => b.visits - a.visits)
  return { ...model, sites: merged }
}

/** 概率分档（0-4，五档）：渲染器统一按档取色，阈值集中在此便于校准 */
export function probabilityBand(p: number): number {
  if (p >= 0.8) return 4
  if (p >= 0.6) return 3
  if (p >= 0.4) return 2
  if (p >= 0.2) return 1
  return 0
}

export interface GeoBounds {
  minLat: number
  maxLat: number
  minLng: number
  maxLng: number
}

/** 图层可见坐标范围（概率格 + 有坐标落脚点 + 顶格区）；无任何坐标返回 null */
export function boundsOf(model: GeoLayerModel): GeoBounds | null {
  let minLat = Infinity
  let maxLat = -Infinity
  let minLng = Infinity
  let maxLng = -Infinity
  const touch = (lat: number, lng: number): void => {
    if (lat < minLat) minLat = lat
    if (lat > maxLat) maxLat = lat
    if (lng < minLng) minLng = lng
    if (lng > maxLng) maxLng = lng
  }
  for (const c of model.cells) {
    for (const [lng, lat] of c.ring) touch(lat, lng)
  }
  for (const s of model.sites) {
    if (s.lat !== null && s.lng !== null) touch(s.lat, s.lng)
  }
  if (model.topZone) touch(model.topZone.lat, model.topZone.lng)
  if (!Number.isFinite(minLat)) return null
  // 单点退化：给 ~500m 半框，避免 fitBounds 缩到世界
  if (minLat === maxLat && minLng === maxLng) {
    const d = 0.005
    return { minLat: minLat - d, maxLat: maxLat + d, minLng: minLng - d, maxLng: maxLng + d }
  }
  return { minLat, maxLat, minLng, maxLng }
}

/** 编码来源中文标签（坐标表层用） */
export function geocodeSourceLabel(source: string | null): string {
  switch (source) {
    case 'gaode':
      return '高德在线'
    case 'admin_offline':
      return '区划离线质心'
    case 'manual':
      return '人工标注'
    case 'raw_fallback':
      return '文本降级'
    default:
      return source ?? '未知'
  }
}
