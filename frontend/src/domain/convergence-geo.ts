/**
 * 三维交汇地图构图层（纯函数，不依赖 leaflet / 高德 / G6）。
 *
 * 为什么抽这一层
 * --------------
 * 与 convergence-graph.ts 同理：地图引擎受授权闸口约束（未授权时任何渲染器
 * 都不得注入外联瓦片），降级到离线 SVG 是常态。构图若与渲染耦合，降级时
 * 图层数据无从校验；抽成纯函数可脱离 canvas/DOM 单测。
 *
 * 一条红线（与 G6 图、列表面板同口径）
 * ------------------------------------
 * **坐标精度必须在地图上可见。** 区划质心（admin_offline/admin/centroid/
 * raw_fallback）的坐标与门牌级长得一模一样。若不加区分，正兵会把"这个区
 * 大概在这儿"读成"人就在这个门牌"——地图天生看上去精确，误导性比列表更强。
 * 故 degraded 点必须：空心渲染 + 明写"区级推算"。
 *
 * 另一条：无坐标的锚点**不许静默消失**。它们照样是交汇结果，只是落不到
 * 地图上；单列到 unmapped，写明原因。
 */
import {
  DIM_LABEL,
  DIM_ORDER,
  type ConvergenceItem,
  type ConvergenceCoord,
} from '../api/endpoints/convergence'

/** 地图上单个可打点的位置簇（同址多条锚点聚合） */
export interface ConvMapPoint {
  id: string
  lat: number
  lng: number
  /** 质心坐标——视觉必须分档，不得画成实心精确点 */
  degraded: boolean
  coordNote: string | null
  geocodeSource: string | null
  address: string
  /** 落在此点上的锚点 key */
  convKeys: string[]
  persons: string[]
  dates: string[]
  /** 该点上的锚点条数（**不是强度**：须与 dims 的 precision 同看） */
  count: number
  /** 该点上最高命中维数 1..3，决定半径 */
  maxDimHit: number
  maxScore: number
  ambiguous: boolean
  /** 三维命中聚合明细：{dim: {hit, count, precision, weight}} */
  dims: Record<string, { hit: number; count: number; precision: string; weight: number }>
}

/** 无坐标、落不到地图上的锚点——单列，不消失 */
export interface ConvUnmapped {
  key: string
  personLabel: string
  date: string
  address: string
  reason: string
}

export interface ConvGeoBounds {
  minLat: number
  maxLat: number
  minLng: number
  maxLng: number
}

export interface ConvGeoModel {
  points: ConvMapPoint[]
  unmapped: ConvUnmapped[]
  /** 质心点数——用于顶部提示"其中 N 个为区级推算" */
  degradedCount: number
  bounds: ConvGeoBounds | null
  /** 坐标服务状态：unavailable 时地图整体不可用，须显式说明 */
  coordsStatus: 'ok' | 'unavailable' | 'error' | 'unknown'
  coordsNote: string
  note: string
}

/** 投影后的屏幕坐标（离线 SVG 渲染器消费） */
export interface ConvProjectedPoint extends ConvMapPoint {
  x: number
  y: number
  /** 半径（按命中维数分档） */
  r: number
}

const EMPTY_DIM = () => ({ hit: 0, count: 0, precision: 'unknown', weight: 0 })

function shortAddr(a: string | null | undefined): string {
  const s = String(a || '')
  const seg = s.split('/')
  return seg[seg.length - 1] || s || '—'
}

/**
 * 构造交汇地图模型。
 *
 * 聚合键含 degraded：质心点与门牌级点**不合并**——两者精度不同，
 * 合并后视觉分档就失真了。
 */
export function buildConvergenceGeo(
  items: ConvergenceItem[],
  diagnostics?: { coords?: { status?: string; detail?: string; effect?: string } } | null,
): ConvGeoModel {
  const points = new Map<string, ConvMapPoint>()
  const unmapped: ConvUnmapped[] = []
  let degradedCount = 0

  for (const it of items) {
    const personLabel = (it.person_names || []).join('/') || it.person_key || '—'
    const coords: ConvergenceCoord[] = it.coords || []

    if (coords.length === 0) {
      unmapped.push({
        key: it.key,
        personLabel,
        date: it.date,
        address: (it.std_addresses || []).map(shortAddr).join('、') || '—',
        reason: '该锚点无可用坐标（无地点实体或语义层不可达），不落点',
      })
      continue
    }

    for (const cd of coords) {
      if (typeof cd.lat !== 'number' || typeof cd.lng !== 'number') continue
      const id = `${cd.lat.toFixed(5)}|${cd.lng.toFixed(5)}|${cd.coord_degraded ? 'c' : 'g'}`
      let p = points.get(id)
      if (!p) {
        const degraded = !!cd.coord_degraded
        p = {
          id,
          lat: cd.lat,
          lng: cd.lng,
          degraded,
          coordNote: cd.coord_note ?? null,
          geocodeSource: cd.geocode_source ?? null,
          address: shortAddr(cd.std_address),
          convKeys: [],
          persons: [],
          dates: [],
          count: 0,
          maxDimHit: 0,
          maxScore: 0,
          ambiguous: false,
          dims: {},
        }
        points.set(id, p)
        if (degraded) degradedCount += 1
      }
      if (!p.convKeys.includes(it.key)) p.convKeys.push(it.key)
      if (!p.persons.includes(personLabel)) p.persons.push(personLabel)
      if (!p.dates.includes(it.date)) p.dates.push(it.date)
      p.count += 1
      p.maxDimHit = Math.max(p.maxDimHit, it.dim_hit_count || 0)
      p.maxScore = Math.max(p.maxScore, it.score || 0)
      p.ambiguous = p.ambiguous || !!it.person_ambiguous

      for (const dim of DIM_ORDER) {
        const d = it.dimensions?.[dim]
        if (!d?.hit) continue
        const cur = p.dims[dim] || EMPTY_DIM()
        cur.hit += 1
        cur.count += d.count || 0
        cur.weight = Math.max(cur.weight, d.weight || 0)
        // 木桶效应：同址多条锚点取**最低**精度档。
        // 首次命中直接采用——初始占位档 unknown 最弱，若参与比较会永远
        // 胜出，把真实精度吞掉（实测：minute + date 得 unknown 而非 date）。
        cur.precision = cur.hit === 1
          ? (d.precision || 'unknown')
          : _weaker(cur.precision, d.precision || 'unknown')
        p.dims[dim] = cur
      }
    }
  }

  const list = [...points.values()]
  const bounds = _bounds(list)
  const status = diagnostics?.coords?.status || 'unknown'

  return {
    points: list,
    unmapped,
    degradedCount,
    bounds,
    coordsStatus: status === 'ok' || status === 'unavailable' || status === 'error'
      ? status : 'unknown',
    coordsNote: diagnostics?.coords?.detail || '',
    note:
      '实心点 = 门牌级坐标；空心点 = 区划质心（区级推算，不代表实际位置）。' +
      '点半径随命中维数增大；虚线环 = 重名待裁决。无坐标锚点单列于下方，不落点。',
  }
}

/** 精度档取较弱者（与后端木桶效应同口径） */
const PRECISION_RANK: Record<string, number> = {
  unknown: 0, date: 1, hour: 2, minute: 3, second: 4,
}

function _weaker(a: string, b: string): string {
  return (PRECISION_RANK[a] ?? 0) <= (PRECISION_RANK[b] ?? 0) ? a : b
}

function _bounds(points: ConvMapPoint[]): ConvGeoBounds | null {
  if (points.length === 0) return null
  let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity
  for (const p of points) {
    minLat = Math.min(minLat, p.lat); maxLat = Math.max(maxLat, p.lat)
    minLng = Math.min(minLng, p.lng); maxLng = Math.max(maxLng, p.lng)
  }
  return { minLat, maxLat, minLng, maxLng }
}

/**
 * 经纬度 → SVG 屏幕坐标（等距柱面近似 + cos(中心纬度) 经度修正）。
 *
 * 城市级（数十公里）范围内该近似误差可忽略，**不做**墨卡托——
 * 本案坐标全域 GCJ-02，直叠高德底图无偏移；本层不做任何坐标系转换。
 */
export function projectConvergencePoints(
  points: ConvMapPoint[],
  bounds: ConvGeoBounds | null,
  width: number,
  height: number,
  padding = 28,
): ConvProjectedPoint[] {
  if (!bounds || points.length === 0 || width <= 0 || height <= 0) return []

  const latSpan = Math.max(bounds.maxLat - bounds.minLat, 1e-6)
  const lngSpan = Math.max(bounds.maxLng - bounds.minLng, 1e-6)
  const centerLat = (bounds.maxLat + bounds.minLat) / 2
  // 经度跨度按中心纬度收缩，保持视觉长宽比不失真
  const xSpan = lngSpan * Math.cos((centerLat * Math.PI) / 180)
  const ySpan = latSpan

  const usableW = Math.max(width - padding * 2, 1)
  const usableH = Math.max(height - padding * 2, 1)
  const scale = Math.min(usableW / xSpan, usableH / ySpan)

  const drawnW = xSpan * scale
  const drawnH = ySpan * scale
  const offsetX = padding + (usableW - drawnW) / 2
  const offsetY = padding + (usableH - drawnH) / 2

  return points.map((p) => ({
    ...p,
    x: offsetX + (p.lng - bounds.minLng) * Math.cos((centerLat * Math.PI) / 180) * scale,
    y: offsetY + (bounds.maxLat - p.lat) * scale,
    // 半径按命中维数分档——与在线引擎共用 MAP_SYMBOL，避免两处口径分叉
    r: MAP_SYMBOL.radius(p.maxDimHit),
  }))
}

/** 地图图例（渲染到地图下方，避免样式口径散落模板里） */
export const MAP_LEGEND = [
  { kind: 'solid', text: '门牌级坐标（位置可信）' },
  { kind: 'hollow', text: '区划质心（区级推算，不代表实际位置）' },
  { kind: 'ring', text: '重名待裁决，不可当作单一自然人' },
  { kind: 'size', text: '半径随命中维数增大（1→3）' },
] as const

/**
 * 在线地图符号口径（离线 SVG / 高德 / Leaflet 三个渲染器共用）。
 *
 * 为什么必须在 domain 层收敛
 * --------------------------
 * 三个渲染器各写一份颜色/半径/文案必然分叉，而"质心点是否空心"是**红线**
 * 不是样式偏好——空心画成实心，等于把区级推算伪装成门牌精度。口径收在一处，
 * 且是纯数据，可脱离 canvas/DOM 单测。
 */
export const MAP_SYMBOL = {
  /** 命中维数 → 半径（px）：1→6、2→8、3→11 */
  radius: (maxDimHit: number): number => (maxDimHit >= 3 ? 11 : maxDimHit === 2 ? 8 : 6),
  /** 门牌级坐标：实心填充 */
  fillPrecise: '#2f7ed8',
  /** 区划质心：**不填充**（空心）——视觉必须分档 */
  fillDegraded: 'transparent',
  fillOpacityPrecise: 0.85,
  /** 质心点用低不透明度描边即可，绝不给实心填充 */
  fillOpacityDegraded: 0,
  stroke: '#33475b',
  /** 重名待裁决：警示色 + 虚线，不可当作单一自然人 */
  strokeAmbiguous: '#b4781a',
  strokeWidth: 1.5,
  strokeWidthAmbiguous: 2,
} as const

/**
 * 点的可读摘要（三个渲染器共用的 tooltip / title 文案）。
 *
 * 必须带**坐标精度自陈**与**三维各自的条数+精度档**——只给一个点而不说
 * 精度，正兵会把"区级推算"读成"人就在这个门牌"。
 */
export function describeConvPoint(p: ConvMapPoint): string {
  const prec = p.degraded ? '区划质心（区级推算，不代表实际位置）' : '门牌级坐标'
  const who = p.persons.join('/')
  const dims = DIM_ORDER
    .filter((d) => p.dims[d]?.hit)
    .map((d) => `${DIM_LABEL[d] ?? d} ${p.dims[d].count}条/${p.dims[d].precision}`)
    .join('，')
  const amb = p.ambiguous ? '｜重名待裁决' : ''
  return `${p.address}｜${prec}${amb}\n${who}｜${p.dates.join('、')}\n命中 ${p.maxDimHit} 维：${dims || '—'}`
}

/** 单行摘要（SVG title / 紧凑场景用） */
export function describeConvPointBrief(p: ConvMapPoint): string {
  const prec = p.degraded ? '区划质心（区级推算）' : '门牌级坐标'
  return `${p.address}｜${prec}｜${p.count} 条锚点｜命中 ${p.maxDimHit} 维`
}
