// 节点窗口（WIN）模型层：按节点类型分化窗口内容 + 屏幕定位。
//
// 为什么窗口只能做 DOM 浮层
// --------------------------
// 画布卡片是 `research-card`，继承 G6 的 Rect——**canvas 图形节点，不是 DOM**，
// 连内嵌一个 <div> 都做不到（CARD_WIDTH=186 / CARD_HEIGHT=50，卡内坐标全写死）。
// 所以"节点上的窗口"只能是 DOM 浮层，按节点**屏幕坐标**定位（WIN-02）。
//
// 位置纪律
// --------
// 结构视图的坐标是自由布局的表达层数据（进快照、可钉住），地图坐标是真实
// 经纬度。窗口定位用的是**投影后的屏幕坐标**，不是画布数据坐标——两者混用
// 会在缩放/平移后错位。
//
// 本文件是纯函数，不含 vue 依赖，可脱离 DOM 单测。

import {
  DIM_LABELS,
  PRECISION_LABEL,
  PRECISION_RANK,
  weightedOf,
  asPrecision,
  describeDim,
  nodeSymbol,
  radiusOfDimHit,
  round2,
  scoreNeverAlone,
  type DimCell,
  type Precision,
} from './canvas-symbol'
import {
  buildChainLayout,
  type ChainStep,
  type SnakeLayout,
} from './canvas-layout-chain'

/** 窗口类型：新模型 5 类 + 线索画布旧路由的 hypothesis/source（见文件尾部恢复段） */
export type WindowKind =
  | 'relation'
  | 'map'
  | 'time'
  | 'evidence'
  | 'holding'
  | 'hypothesis'
  | 'source'

/** 研判层节点类型（后端 server/app/canvas_case.py 同口径） */
export type CanvasNodeKind =
  | 'subject'
  | 'place'
  | 'event'
  | 'analysis_result'
  | 'item'
  | 'hypothesis'
  | 'note'
  | 'fact'
  | 'object'
  | string

export interface WindowNode {
  id: string
  kind: CanvasNodeKind
  label: string
  props?: Record<string, unknown>
}

export interface WindowResolution {
  kind: WindowKind | null
  /** 无窗口时也必须给出原因——正兵点了没反应比没有这个功能更糟 */
  reason: string
}

const KIND_TO_WINDOW: Record<string, WindowKind> = {
  subject: 'relation',
  place: 'map',
  event: 'time',
  hypothesis: 'hypothesis',
  analysis_result: 'evidence',
  item: 'holding',
}

/**
 * 节点 → 窗口类型。
 * 溯源类节点（fact/source_row）不给窗口：它们的明细在既有 FactDetailPopover /
 * 节点抽屉里，重复开一个窗口只会稀释"窗口=这一维的可视化"这个约定。
 */
export function windowKindFor(node: WindowNode | null | undefined): WindowResolution {
  if (!node) return { kind: null, reason: '未选中节点' }
  const k = KIND_TO_WINDOW[String(node.kind ?? '')]
  if (!k) {
    return { kind: null, reason: `该节点类型（${node.kind}）无专属窗口，明细见节点抽屉` }
  }
  return { kind: k, reason: '' }
}

// ------------------------------------------------------------------ //
// WIN-02 定位
// ------------------------------------------------------------------ //

export const WINDOW_BOX = { width: 340, height: 300, gap: 14, margin: 8 } as const

export interface Placement {
  left: number
  top: number
  /** 右侧空间不足，翻到锚点左侧 */
  flippedX: boolean
  /** 下方空间不足，上移 */
  flippedY: boolean
}

/**
 * 贴附定位：默认落在锚点右下方；越界则翻转并夹紧到视口内。
 * 翻转必须**可被断言**（flippedX/flippedY），否则测试只能看最终坐标，
 * 无法区分"恰好在界内"和"正确翻转"。
 */
export function placeWindow(
  anchor: { x: number; y: number },
  viewport: { width: number; height: number },
  box: { width: number; height: number } = WINDOW_BOX,
  margin: number = WINDOW_BOX.margin,
): Placement {
  const gap = WINDOW_BOX.gap
  let left = anchor.x + gap
  let flippedX = false
  if (left + box.width > viewport.width - margin) {
    left = anchor.x - gap - box.width
    flippedX = true
  }
  let top = anchor.y + gap
  let flippedY = false
  if (top + box.height > viewport.height - margin) {
    top = anchor.y - gap - box.height
    flippedY = true
  }
  // 翻转后仍可能溢出左/上边界，夹紧（夹紧不重置翻转标记：翻转已发生）
  left = Math.max(margin, Math.min(left, Math.max(margin, viewport.width - box.width - margin)))
  top = Math.max(margin, Math.min(top, Math.max(margin, viewport.height - box.height - margin)))
  return { left: round2(left), top: round2(top), flippedX, flippedY }
}

// ------------------------------------------------------------------ //
// WIN-03 关系窗口
// ------------------------------------------------------------------ //

export interface RelationRow {
  id: string
  label: string
  rel: string
  precision: Precision
  /** 次数——必须与精度同看 */
  count: number
  weight: number
  /** 系统推断还是人工确认 */
  system: boolean
  text: string
}

export interface RelationWindow {
  center: { id: string; label: string; note: string }
  rows: RelationRow[]
  /** 无关联时写明原因，不得显示成空白 */
  empty: string
}

export function buildRelationWindow(
  node: WindowNode,
  edges: Array<{
    source: string
    target: string
    rel?: string
    /** 早期形态：属性挂在 data 上（人工边仍可能是这种） */
    data?: Record<string, unknown>
    /** 现行形态：装配层（物品源层/镜头层）写 props。data 与 props 并存时
     *  **props 优先**——两处各写一份迟早分叉，必须有单一优先顺序。 */
    props?: Record<string, unknown>
  }>,
  nodeById: ReadonlyMap<string, WindowNode>,
): RelationWindow {
  const ambiguous = node.props?.person_pk_ambiguous === true
  const symbol = nodeSymbol({
    ambiguous,
    precision: String(node.props?.precision ?? ''),
    unanchored: node.props?.person_pk == null,
  })
  const rows: RelationRow[] = []
  for (const e of edges) {
    const otherId = e.source === node.id ? e.target : e.target === node.id ? e.source : null
    if (!otherId) continue
    const other = nodeById.get(otherId)
    if (!other) continue
    const d = e.data ?? {}
    const precision = asPrecision(String(d.precision ?? ''))
    const count = Number(d.count ?? 1)
    rows.push({
      id: otherId,
      label: other.label,
      rel: String(e.rel ?? '关联'),
      precision,
      count,
      weight: weightedOf(count, precision),
      system: d.system !== false,
      text: `${String(e.rel ?? '关联')}｜${count} 次/${PRECISION_LABEL[precision]}`,
    })
  }
  // 排序：加权分 → 精度强弱 → 名称。
  // 第二关键字**绝不能退回裸次数**——15 次日期级与 3 次时刻级加权后都是 3.0，
  // 若按次数兜底，15 次那条例外地又排回了前面，"次数会骗人"从后门钻回来。
  rows.sort(
    (a, b) =>
      b.weight - a.weight ||
      PRECISION_RANK[a.precision] - PRECISION_RANK[b.precision] ||
      a.label.localeCompare(b.label),
  )
  return {
    center: { id: node.id, label: node.label, note: symbol.note },
    rows,
    empty: rows.length
      ? ''
      : symbol.note.includes('未锚定')
        ? '该主体未锚定主键，无法检索关联'
        : '画布内暂无该主体的关联边',
  }
}

// ------------------------------------------------------------------ //
// WIN-04 地图窗口
// ------------------------------------------------------------------ //

export interface MapWindowPoint {
  id: string
  label: string
  lng: number
  lat: number
  /** 门牌级真实坐标（false=区划质心） */
  precise: boolean
  address: string
  ambiguous: boolean
  radius: number
  maxDimHit: number
  dims: DimCell[]
}

/** 物品轨迹点（WIN-04 扩展：多点轨迹，用于"车三日下午在某地"这类结论） */
export interface MapTrackDot {
  /** 时刻原文；无时刻时为日期——**不可据此判"下午"**，见 determinable */
  at: string
  lng: number
  lat: number
  precise: boolean
  address: string
  /** 精度达 minute 及以上，才谈得上"同时/同时段" */
  determinable: boolean
}

export interface MapWindow {
  point: MapWindowPoint | null
  /** 落不了地图时必须写明原因；锚点不消失，只是不画 */
  unmappable: string
  /** 坐标不可用（语义层不可达）时的全局提示 */
  coordsNote: string
  /** 可落图的轨迹点（升序按 at）；无轨迹时为空数组 */
  track: MapTrackDot[]
  /** 轨迹点里因缺坐标而未能落图的条数——必须报出来，否则正兵以为都画上了 */
  trackUnmappable: number
  trackNote: string
}

export function buildMapWindow(
  node: WindowNode,
  coordOf?: (node: WindowNode) => { lng?: number; lat?: number; precise?: boolean; address?: string } | null,
): MapWindow {
  const p = node.props ?? {}
  const raw = coordOf ? coordOf(node) : null
  const lng = Number(raw?.lng ?? p.lng ?? p.longitude)
  const lat = Number(raw?.lat ?? p.lat ?? p.latitude)
  const address = String(raw?.address ?? p.address ?? node.label ?? '')
  const precise = raw?.precise ?? p.coord_precise === true
  const usable = Number.isFinite(lng) && Number.isFinite(lat)

  // 轨迹点：与 point 独立解析——节点本身无坐标时，轨迹仍可落图。
  // 三条纪律：坐标为 0 视为补 0 哨兵一并剔除（会画到几内亚湾）；
  // 缺坐标的点计入 trackUnmappable 而不是静默丢弃；
  // determinable 只认 minute/second，整点（hour 档）判不出"下午"。
  const rawTrack = Array.isArray(p.track) ? (p.track as Array<Record<string, unknown>>) : []
  const track: MapTrackDot[] = []
  let trackUnmappable = 0
  for (const t of rawTrack) {
    const tl = Number(t?.lng)
    const tla = Number(t?.lat)
    if (!Number.isFinite(tl) || !Number.isFinite(tla) || tl === 0 || tla === 0) {
      trackUnmappable += 1
      continue
    }
    const prec = String((t?.time_precision as string) ?? '')
    track.push({
      at: String(t?.timestamp ?? t?.date ?? ''),
      lng: tl,
      lat: tla,
      precise: prec !== 'date' && prec !== 'hour',
      address: String(t?.location ?? ''),
      determinable: prec === 'minute' || prec === 'second',
    })
  }
  track.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
  const trackNote = track.length
    ? `轨迹 ${track.length} 点${trackUnmappable ? `（${trackUnmappable} 点无坐标未画）` : ''}`
    : rawTrack.length
      ? '有轨迹但无可用坐标，未落图'
      : ''

  if (!usable) {
    return {
      point: null,
      unmappable: track.length
        ? '该节点自身无坐标，仅按轨迹点呈现'
        : '该地点无可用经纬度（无地点实体或语义层不可达），不参与地图呈现',
      coordsNote: '',
      track,
      trackUnmappable,
      trackNote,
    }
  }
  const dims = dimCellsOf(p)
  const maxDimHit = dims.filter((d) => d.hit).length
  return {
    point: {
      id: node.id,
      label: node.label,
      lng,
      lat,
      precise,
      address,
      ambiguous: p.person_pk_ambiguous === true,
      radius: radiusOfDimHit(maxDimHit),
      maxDimHit,
      dims,
    },
    unmappable: '',
    coordsNote: precise ? '' : '区划质心（区级推算，不代表实际门牌位置）',
    track,
    trackUnmappable,
    trackNote,
  }
}

// ------------------------------------------------------------------ //
// WIN-05 时间窗口
// ------------------------------------------------------------------ //

export interface TimeRow {
  id: string
  at: string
  label: string
  precision: Precision
  dimLabel: string
}

export interface TimeWindow {
  rows: TimeRow[]
  /** 可判时间窗真重叠的条数——是"3 次时刻级"的那个 3 */
  determinableCount: number
  /** 仅同地异时的条数——是"15 次日期级"的那个 15 */
  degradedCount: number
  empty: string
}

export function buildTimeWindow(
  node: WindowNode,
  events: Array<{ id: string; at?: string; label?: string; precision?: string; dim?: string }>,
): TimeWindow {
  const rows: TimeRow[] = events
    .map((e) => ({
      id: e.id,
      at: String(e.at ?? ''),
      label: String(e.label ?? ''),
      precision: asPrecision(e.precision),
      dimLabel: DIM_LABELS[String(e.dim ?? '')] ?? '',
    }))
    .filter((r) => r.at !== '')
    .sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
  const determinableCount = rows.filter(
    (r) => r.precision === 'minute' || r.precision === 'second',
  ).length
  const degradedCount = rows.filter((r) => r.precision === 'date' || r.precision === 'unknown').length
  return {
    rows,
    determinableCount,
    degradedCount,
    empty: rows.length ? '' : '该事件节点无可用时刻',
  }
}

// ------------------------------------------------------------------ //
// WIN-06 证据窗口
// ------------------------------------------------------------------ //

export interface SupportRow {
  obs_id: string
  label: string
  precision: Precision
  dim: string
}

export interface EvidenceWindow {
  dims: DimCell[]
  score: number
  /** 分数能否单独呈现——false 时必须渲染矩阵 */
  scoreAlone: boolean
  scoreAloneReason: string
  supports: SupportRow[]
  falsification: string[]
}

export function buildEvidenceWindow(
  node: WindowNode,
  supports?: Array<{ obs_id?: string; label?: string; precision?: string; dim?: string }>,
): EvidenceWindow {
  const p = node.props ?? {}
  const dims = dimCellsOf(p)
  const score = Number(p.score ?? dims.reduce((s, d) => s + (d.hit ? d.weight : 0), 0))
  const chk = scoreNeverAlone(dims, score)
  const rows: SupportRow[] = (supports ?? []).map((s) => ({
    obs_id: String(s.obs_id ?? ''),
    label: String(s.label ?? ''),
    precision: asPrecision(s.precision),
    dim: DIM_LABELS[String(s.dim ?? '')] ?? String(s.dim ?? ''),
  }))
  const falsification = Array.isArray(p.falsification)
    ? (p.falsification as unknown[]).map((x) => String(x))
    : []
  return {
    dims,
    score: round2(score),
    scoreAlone: chk.ok,
    scoreAloneReason: chk.reason,
    supports: rows,
    falsification,
  }
}

// ------------------------------------------------------------------ //
// WIN-09 持有链窗口（物品）
// ------------------------------------------------------------------ //

export interface HoldingLink {
  holder: string
  start?: string
  end?: string
  /** 区间不可判定——不是"没有"，是"不知道" */
  unknownRange: boolean
}

export interface HoldingWindow {
  item: string
  links: HoldingLink[]
  /** 时序错序（后一手的开始早于前一手的开始） */
  outOfOrder: boolean
  /** 同一时刻被多人持有——冲突，需人工判 */
  overlapping: boolean
  note: string
  /**
   * WIN-11：链型布局。
   *   snake —— 蛇形折返：节点折返排布 + 重叠边走行间空隙绕行；
   *   flat  —— 无可排序环节（全部时间不可判定）时退回列表。
   *            **这不是降级兜底**：此时画成图只剩一行互不相连的盒，
   *            列表比图更清楚，是刻意选择而非画不出来。
   */
  layoutKind: 'snake' | 'flat'
  /** snake 时的布局结果；flat 时为 null——渲染层不得自行排布 */
  layout: SnakeLayout | null
}

/**
 * 画布持有边的关系名。后端 server/app/canvas_hold.py 的 HOLD_REL 同值。
 * 关系名是协议，前后端各持一份常量可接受，但**值必须一致**——
 * 后端 tests/test_canvas_hold.py 的 HOLD_REL 断言即这条协议的守卫。
 */
export const HOLD_REL = '持有'

/**
 * 从画布边派生某物品节点的持有链。
 *
 * 为什么在窗口内派生，而不是等宿主传 holdingLinks
 * ---------------------------------------------
 * 宿主（画布页）未必知道"物品节点还需要持有链"。一旦漏传，holdingLinks 的
 * 默认值是空数组，窗口就会显示"无持有记录"——**有数据却显示没有**，
 * 这比没有这个功能更糟：正兵会据此认为"这东西没人持有过"。
 * 放在窗口内按边派生，任何宿主都自动生效，漏传不再致命。
 *
 * 方向：人是 source、物品是 target（与本体 holds 一致）。反向边不认——
 * 方向错了是建模错，静默纠正会让错误一直藏着。
 */
export function deriveHoldingLinks(
  node: WindowNode | null | undefined,
  edges: Array<{
    source: string
    target: string
    rel?: string
    /** 早期形态：属性挂在 data 上（人工边仍可能是这种） */
    data?: Record<string, unknown>
    /** 现行形态：装配层写 props。并存时 **props 优先** */
    props?: Record<string, unknown>
  }>,
  nodeById?: ReadonlyMap<string, WindowNode>,
): HoldingLink[] {
  const id = node ? String(node.id ?? '') : ''
  if (!id) return []
  const out: HoldingLink[] = []
  for (const e of edges || []) {
    if (!e || e.rel !== HOLD_REL) continue
    if (String(e.target ?? '') !== id) continue
    // props 优先、data 兜底：后端 canvas_item_source 写 props，
    // 既有人工边写 data。只认一个键会让另一类边读不到——读不到时窗口
    // 显示"无持有记录"，是"有数据却说没有"，比没这功能更糟。
    const data = {
      ...((e.data ?? {}) as Record<string, unknown>),
      ...((e.props ?? {}) as Record<string, unknown>),
    }
    const src = String(e.source ?? '')
    const holder = String(data.holder ?? '') || (nodeById?.get(src)?.label ?? '') || src
    const start = String(data.start ?? data.start_date ?? '')
    const end = String(data.end ?? data.end_date ?? '')
    out.push({ holder, start, end, unknownRange: !start && !end })
  }
  return out
}

export function buildHoldingWindow(
  node: WindowNode,
  links: HoldingLink[],
): HoldingWindow {
  const norm = links.map((l) => ({
    holder: l.holder,
    start: l.start ?? '',
    end: l.end ?? '',
    unknownRange: l.unknownRange === true || !l.start || !l.end,
  }))
  let outOfOrder = false
  for (let i = 1; i < norm.length; i += 1) {
    const prev = norm[i - 1].start
    const cur = norm[i].start
    if (prev && cur && cur < prev) outOfOrder = true
  }
  let overlapping = false
  for (let i = 0; i < norm.length; i += 1) {
    for (let j = i + 1; j < norm.length; j += 1) {
      const a = norm[i]
      const b = norm[j]
      if (!a.start || !a.end || !b.start || !b.end) continue
      if (a.start <= b.end && b.start <= a.end) overlapping = true
    }
  }
  const note = outOfOrder
    ? '持有链时序错序，请核对登记时间'
    : overlapping
      ? '存在时间区间重叠，同物同时被多方持有需人工判定'
      : norm.some((l) => l.unknownRange)
        ? '部分持有环节无时间区间，链序不可判定'
        : '持有链时序自洽'
  // 链按**登记顺序**构造，不按时间排序——重排会抹掉"登记顺序与登记时间
  // 矛盾"这个信号（见 canvas-layout-chain 红线 2）。
  const steps: ChainStep[] = norm.map((l, i) => ({
    id: `h${i}`,
    holder: l.holder,
    start: l.start,
    end: l.end,
    order: i,
    unknownRange: l.unknownRange,
  }))
  // 至少一个环节时间可判定才成链；否则 flat（不是降级，见 layoutKind 说明）
  const sortable = steps.some((s) => !s.unknownRange)
  const layout = sortable ? buildChainLayout(steps) : null
  return {
    item: node.label,
    links: norm,
    outOfOrder,
    overlapping,
    note,
    layoutKind: layout ? 'snake' : 'flat',
    layout,
  }
}

// ------------------------------------------------------------------ //
// 共用：三维矩阵
// ------------------------------------------------------------------ //

/**
 * 从节点 props 还原三维矩阵。
 * 未命中维度**保留并写明原因**——缺维信息不能从窗口里消失，否则正兵会
 * 把"该维度没查"读成"该维度查了没事"。
 *
 * 三种历史形状都认（缺一会让镜头重建层节点三维全灰、加权分恒 0）：
 * 1. ``dim_cells`` 对象——growth 重建层现口径，逐维带
 *    hit/count/precision/weight（由观察聚合，最完整）；
 * 2. ``dims`` 对象——旧口径（clue 画布/测试夹具）；
 * 3. ``dims`` 字符串数组——只声明查过哪几维，count/精度回退节点自身档。
 */
export function dimCellsOf(props: Record<string, unknown> | undefined): DimCell[] {
  const p = props ?? {}
  const dimCells = p.dim_cells
  const dimsRaw = p.dims
  const rawObject: Record<string, Record<string, unknown>> =
    (dimCells && typeof dimCells === 'object' && !Array.isArray(dimCells)
      ? dimCells
      : (dimsRaw && typeof dimsRaw === 'object' && !Array.isArray(dimsRaw)
        ? dimsRaw
        : {})) as Record<string, Record<string, unknown>>
  // 字符串数组兜底：只知道"查过这几维"，条数不可得（置 0），
  // 精度/权重继承节点整档——比全 miss 诚实，但不伪造条数。
  const listed = Array.isArray(dimsRaw)
    ? new Set(dimsRaw.map((x) => String(x)).filter(Boolean))
    : (Array.isArray(dimCells) ? new Set(dimCells.map((x) => String(x))) : null)
  const fallbackPrecision = asPrecision(String(p.precision ?? ''))
  const fallbackWeight = Number(p.precision_weight ?? 0)
  const order = ['space', 'time', 'relation']
  return order.map((dim) => {
    const d = rawObject[dim]
    if (d) {
      const hit = !!d && (Number(d.hit ?? d.count ?? 0) > 0)
      const count = Number(d.count ?? 0)
      const precision = asPrecision(String(d.precision ?? ''))
      const weight = d.weight != null ? Number(d.weight) : 0
      return {
        dim,
        dimLabel: DIM_LABELS[dim] ?? dim,
        hit,
        count,
        precision,
        weight: round2(weight),
        reason: hit ? undefined : String(d.reason ?? '该维度无支撑观察'),
      }
    }
    if (listed?.has(dim)) {
      return {
        dim,
        dimLabel: DIM_LABELS[dim] ?? dim,
        hit: true,
        count: 0,
        precision: fallbackPrecision,
        weight: round2(fallbackWeight),
        reason: undefined,
      }
    }
    return {
      dim,
      dimLabel: DIM_LABELS[dim] ?? dim,
      hit: false,
      count: 0,
      precision: 'unknown' as Precision,
      weight: 0,
      reason: '该维度无支撑观察',
    }
  })
}

/** 矩阵的可读块（调试/降级列表用） */
export function describeMatrix(cells: DimCell[]): string[] {
  return cells.map(describeDim)
}

// ------------------------------------------------------------------ //
// 在线引擎适配（WIN-07/08 用）
// ------------------------------------------------------------------ //

/**
 * 单地点 → 在线引擎可消费的地理模型。
 *
 * 这里刻意**不复用 buildConvergenceGeo**：那个函数吃的是 ConvergenceItem
 * （人-时-地锚点），而窗口里是单个 place 节点；硬套会把"地点节点"伪装成
 * "交汇锚点"，count/maxScore 等字段全是编的。单点模型只填确定字段，
 * 其余留空并在 note 里说明。
 *
 * 单点也要给 bounds，否则引擎不知道该把视野放哪；跨度取固定小值，
 * 只表达"这一个点在哪"，不暗示任何聚集形态。
 */
export function toSinglePointGeoModel(p: MapWindowPoint): {
  points: Array<Record<string, unknown>>
  unmapped: unknown[]
  degradedCount: number
  bounds: { minLat: number; maxLat: number; minLng: number; maxLng: number }
  coordsStatus: 'ok' | 'unavailable'
  coordsNote: string
  note: string
} {
  const span = 0.01
  return {
    points: [
      {
        id: p.id,
        lat: p.lat,
        lng: p.lng,
        degraded: !p.precise,
        coordNote: p.precise ? null : '区划质心（区级推算）',
        geocodeSource: null,
        address: p.address,
        convKeys: [],
        persons: [p.label],
        dates: [],
        count: 1,
        maxDimHit: p.maxDimHit,
        maxScore: 0,
        ambiguous: p.ambiguous,
        dims: Object.fromEntries(
          p.dims.map((d) => [d.dim, { hit: d.hit ? 1 : 0, count: d.count, precision: d.precision, weight: d.weight }]),
        ),
      },
    ],
    unmapped: [],
    degradedCount: p.precise ? 0 : 1,
    bounds: {
      minLat: p.lat - span,
      maxLat: p.lat + span,
      minLng: p.lng - span,
      maxLng: p.lng + span,
    },
    coordsStatus: 'ok',
    coordsNote: p.precise ? '' : '区划质心（区级推算，不代表实际门牌位置）',
    note: '单点窗口视图：仅表达该地点位置，不代表任何聚集形态',
  }
}

/**
 * 多点轨迹模型（供在线引擎消费）。
 *
 * 为什么必须有它：授权后若仍走单点模型，正兵会看到"轨迹点消失了"——授权
 * 反而丢信息，那是倒退。故有轨迹时优先用多点模型，并把节点自身位置并入。
 *
 * 不画移动路径，只画出现位置：两次出现之间没有观测，连线会把"路径未知"
 * 伪装成"沿此路行驶"。
 */
export function toTrackGeoModel(
  base: MapWindowPoint | null,
  track: MapTrackDot[],
): ReturnType<typeof toSinglePointGeoModel> | null {
  if (!track.length) return null
  const pts: Array<Record<string, unknown>> = track.map((t, i) => ({
    id: `${base?.id ?? 'track'}#${i}`,
    lat: t.lat,
    lng: t.lng,
    degraded: !t.precise,
    coordNote: t.precise ? null : '区划质心（区级推算）',
    geocodeSource: null,
    address: t.address,
    convKeys: [],
    persons: base ? [base.label] : [],
    dates: [t.at],
    count: 1,
    maxDimHit: 0,
    maxScore: 0,
    ambiguous: false,
    dims: {},
  }))
  if (base) {
    pts.unshift({
      id: `${base.id}#node`,
      lat: base.lat,
      lng: base.lng,
      degraded: !base.precise,
      coordNote: base.precise ? null : '区划质心（区级推算）',
      geocodeSource: null,
      address: base.address,
      convKeys: [],
      persons: [base.label],
      dates: [],
      count: 1,
      maxDimHit: base.maxDimHit,
      maxScore: 0,
      ambiguous: base.ambiguous,
      dims: Object.fromEntries(
        base.dims.map((d) => [
          d.dim,
          { hit: d.hit ? 1 : 0, count: d.count, precision: d.precision, weight: d.weight },
        ]),
      ),
    })
  }
  const lats = pts.map((q) => q.lat as number)
  const lngs = pts.map((q) => q.lng as number)
  const span = 0.01
  return {
    points: pts,
    unmapped: [],
    degradedCount: pts.filter((q) => q.degraded).length,
    bounds: {
      minLat: Math.min(...lats) - span,
      maxLat: Math.max(...lats) + span,
      minLng: Math.min(...lngs) - span,
      maxLng: Math.max(...lngs) + span,
    },
    coordsStatus: 'ok',
    coordsNote: '',
    note: '轨迹点视图：仅呈现各次出现的位置，不表达移动路径',
  }
}

// ======================================================================
// 恢复段：精度符号口径单一来源（PRD V1.0.0 红线 R1 / REQ-P0-01、02）。
// 本段在窗口模型层重构中被误删，导致 g6-card-node / win/TimeWindow /
// CanvasNodeWindow 三处 import 断裂（运行期 SyntaxError）。恢复后：
// 卡内迷你符号（g6-card-node）与分型窗口（components/research/win）继续
// 消费本模块，禁止在别处二次硬编码线型/填充判断——否则「12 条 date 档」
// 和「4 条 minute 档」在图上长得一样，精度加权会被一张漂亮图抹平。
// WindowKind 已与上方新模型合并为并集（holding 属新模型、hypothesis/source
// 属旧路由）。
// ======================================================================

/** 时间精度档（core/time_semantics.py 的 minute/date 两档；脏数据按无时间降级） */
export type TimePrecision = 'minute' | 'date'

/** 精度符号：卡内精度点与窗口内精度徽标共用的口径 */
export interface PrecisionSymbol {
  /** 描边线型：实线为空数组，虚线为 dash 间隔数组（G6 lineDash 语义） */
  lineDash: number[]
  /** 描边宽度：时刻级粗、日期级细、无时间最细 */
  strokeWidth: number
  /** 填充模式：solid=实心（可判时间窗真重叠），hollow=空心（仅同地异时） */
  fillMode: 'solid' | 'hollow'
  /** 精度色：时刻级=时间维度暖橙，日期级=查询蓝灰 */
  color: string
  /** 是否渲染符号；无时间档返回弱化空心规格但 visible=false（不画占位误导） */
  visible: boolean
}

/** 时刻级（minute）：实线 + 实心 + 粗边。色相同 canvasTokens.band.burst（时间维度暖橙） */
const MINUTE_SYMBOL: PrecisionSymbol = {
  lineDash: [],
  strokeWidth: 2.5,
  fillMode: 'solid',
  color: '#FF7043',
  visible: true,
}

/** 日期级（date）：虚线 + 空心 + 细边。色相同 canvasTokens.band.collision_window（查询蓝灰） */
const DATE_SYMBOL: PrecisionSymbol = {
  lineDash: [4, 3],
  strokeWidth: 1.5,
  fillMode: 'hollow',
  color: '#6E87B5',
  visible: true,
}

/** 无时间档：弱化空心规格，visible=false（渲染方据此跳过绘制，不画占位） */
const NONE_SYMBOL: PrecisionSymbol = {
  lineDash: [2, 3],
  strokeWidth: 1,
  fillMode: 'hollow',
  color: '#84A2B5',
  visible: false,
}

/**
 * 精度档 → 符号。入参宽容（unknown）：合法枚举外（null/undefined/脏值）
 * 一律降级为无时间档，不抛错（PRD 功能 2 异常处理：脏数据静默降级）。
 */
export function precisionSymbol(precision: unknown): PrecisionSymbol {
  if (precision === 'minute') return { ...MINUTE_SYMBOL }
  if (precision === 'date') return { ...DATE_SYMBOL }
  return { ...NONE_SYMBOL }
}

/**
 * 重名待裁决边框符号（红线 R2：绝不自裁）。虚线边框 + ? 标记，
 * 琥珀警示色（区别于人工新增褐橙 strokeManual 与已采纳金 strokeAdopted）。
 * 注意与 date 档虚线线型不同（[5,3] vs [4,3]），两种语义不混淆。
 */
export const AMBIGUOUS_BORDER = {
  lineDash: [5, 3],
  strokeWidth: 1.5,
  marker: '?',
  color: '#E8A23C',
} as const

/**
 * 节点 kind → 窗口类型路由表（线索画布旧路由）：
 * subject→关系、place→地图、event→时间、analysis_result→证据、
 * hypothesis→假设、fact/source_row→溯源。
 * 其余 kind（rule/object/source_file/verify_item/evidence/note）返回 null，
 * 沿用既有 FactDetailPopover；遗留 function_result 同样走 null（旧线索画布只读域）。
 */
const WINDOW_FOR: Record<string, WindowKind> = {
  subject: 'relation',
  place: 'map',
  event: 'time',
  analysis_result: 'evidence',
  hypothesis: 'hypothesis',
  fact: 'source',
  source_row: 'source',
}

/**
 * 节点 kind → 窗口类型。返回 null 表示该 kind 无分型窗口；
 * 未知 kind / 非字符串入参一律 null，不抛错。
 */
export function windowFor(kind: unknown): WindowKind | null {
  if (typeof kind !== 'string') return null
  return WINDOW_FOR[kind] ?? null
}

/** 时间条规格：时点位置与跨度均为案件时间轴上的 0~1 比例 */
export interface TimeBarSpec {
  /** 时点比例（time_start 在轴上的位置，clamp 到 [0,1]） */
  start: number
  /** 跨度占比；无合法 time_end 时为 0（只画时点标记，不画条长） */
  span: number
  /** 精度符号（线型/填充/色与精度点同源，minute=实线 date=虚线） */
  symbol: PrecisionSymbol
  /** 与 symbol.visible 等价的显式开关（渲染方据此跳过绘制） */
  visible: boolean
}

/** 时间轴范围（毫秒）。startMs ≥ endMs 视为无效轴（除零/倒挂不画） */
export interface TimeAxisRange {
  startMs: number
  endMs: number
}

/** 宽容解析时间串为毫秒；非法/空值返回 null（脏数据静默降级，不抛错） */
export function parseTimeMs(value: unknown): number | null {
  if (typeof value !== 'string') return null
  const raw = value.trim()
  if (!raw) return null
  const ms = Date.parse(raw)
  return Number.isFinite(ms) ? ms : null
}

/**
 * 节点 props → 卡内时间条规格。
 * - 无 time_start / 时间解析失败 → null（不画占位误导）
 * - 轴无效（null 或 startMs ≥ endMs）→ null（位置条无意义）
 * - time_start 越界 clamp 到 [0,1]；span = (end−start)/轴长，clamp [0,1]
 * - 精度档经 precisionSymbol 派生（minute=实线粗 date=虚线细）
 */
export function timeBarSpec(
  props: unknown,
  axis: TimeAxisRange | null,
): TimeBarSpec | null {
  const p = (props ?? {}) as Record<string, unknown>
  const startMs = parseTimeMs(p.time_start)
  if (startMs === null) return null
  if (!axis || !Number.isFinite(axis.startMs) || !Number.isFinite(axis.endMs))
    return null
  if (axis.endMs <= axis.startMs) return null
  const axisSpan = axis.endMs - axis.startMs
  const start = Math.min(1, Math.max(0, (startMs - axis.startMs) / axisSpan))
  const endMs = parseTimeMs(p.time_end)
  let span = 0
  if (endMs !== null && endMs > startMs) {
    const end = Math.min(1, Math.max(0, (endMs - axis.startMs) / axisSpan))
    span = Math.min(1 - start, Math.max(0, end - start))
  }
  const symbol = precisionSymbol(p.time_precision)
  return { start, span, symbol, visible: symbol.visible }
}

/**
 * 案件级时间轴范围：取全部节点 time_start/time_end 的极值。
 * 无任何合法时间返回 null（调用方据此不为时间条传轴，卡片不画条）。
 */
export function timeAxisRangeOf(
  nodes: ReadonlyArray<unknown>,
): TimeAxisRange | null {
  let min = Infinity
  let max = -Infinity
  for (const raw of nodes) {
    const p = (raw ?? {}) as Record<string, unknown>
    const props = (p.props ?? raw ?? {}) as Record<string, unknown>
    const s = parseTimeMs(props.time_start)
    if (s !== null) {
      min = Math.min(min, s)
      max = Math.max(max, s)
    }
    const e = parseTimeMs(props.time_end)
    if (e !== null) {
      min = Math.min(min, e)
      max = Math.max(max, e)
    }
  }
  if (min === Infinity || max === -Infinity || max <= min) return null
  return { startMs: min, endMs: max }
}

/**
 * 可开分型窗口的节点 kind（复合节点「窗口」按钮的显示口径）。
 * fact/source_row 虽在 windowFor 路由表里（溯源弹层语义），
 * 但案件级画布不含这两类节点，窗口按钮只对研判 5 类显示。
 */
const WINDOW_NODE_KIND_SET: ReadonlySet<string> = new Set([
  'subject',
  'place',
  'event',
  'analysis_result',
  'hypothesis',
])

/** 该节点是否显示「窗口」按钮（研判 5 类；note/溯源类不开窗） */
export function hasWindow(kind: unknown): boolean {
  return typeof kind === 'string' && WINDOW_NODE_KIND_SET.has(kind)
}

/** 窗口类型 → 放大目标路由（无放大入口的窗口返回空串，调用方不渲染放大按钮） */
const WINDOW_ENLARGE_ROUTE: Partial<Record<WindowKind, string>> = {
  relation: '/c/graph',
  map: '/c/geo',
  time: '/c/convergence',
  evidence: '/c/convergence',
  hypothesis: '',
  holding: '',
}

/**
 * 窗口放大入口目标（PRD 功能 3：跳对应全局视图）。
 * hypothesis 窗口无全局视图（假设是画布本域对象）返回空串；
 * holding 暂无全局视图同样空串；source 沿用溯源弹层无放大。
 */
export function windowEnlargeRoute(kind: WindowKind | null): string {
  if (!kind || kind === 'source') return ''
  return WINDOW_ENLARGE_ROUTE[kind] ?? ''
}

/** 窗口锚点（节点中心在画布容器内的像素坐标）与视口尺寸 */
export interface WindowAnchorInput {
  anchorX: number
  anchorY: number
  /** 节点半宽/半高（像素，窗口与节点卡片留出间距） */
  nodeHalfW?: number
  nodeHalfH?: number
  viewportW: number
  viewportH: number
  /** 窗口尺寸（像素） */
  winW: number
  winH: number
  /** 与节点/视口边缘的间距 */
  margin?: number
}

export interface WindowPosition {
  x: number
  y: number
  /** 实际贴附侧：right=节点右侧（默认），left=越界翻转 */
  side: 'right' | 'left'
}

/**
 * 贴附浮窗定位：默认节点右侧居中，水平越界翻左侧，垂直 clamp 进视口。
 * 入参宽容（NaN/负尺寸一律按 0 处理），不抛错。
 */
export function windowPosition(input: WindowAnchorInput): WindowPosition {
  const nodeHalfW = Math.max(0, input.nodeHalfW ?? 93)
  const nodeHalfH = Math.max(0, input.nodeHalfH ?? 25)
  const margin = Math.max(0, input.margin ?? 12)
  const vw = Math.max(0, input.viewportW)
  const vh = Math.max(0, input.viewportH)
  const winW = Math.max(0, input.winW)
  const winH = Math.max(0, input.winH)
  const ax = Number.isFinite(input.anchorX) ? input.anchorX : 0
  const ay = Number.isFinite(input.anchorY) ? input.anchorY : 0

  const rightX = ax + nodeHalfW + margin
  const leftX = ax - nodeHalfW - margin - winW
  // 右侧放得下（右缘不超视口）→ 右；否则左侧还能放下 → 左；两侧都放不下 → clamp 右
  const rightFits = rightX + winW <= vw
  const leftFits = leftX >= 0
  const x = rightFits ? rightX : leftFits ? leftX : Math.max(0, Math.min(rightX, vw - winW))
  // 垂直：窗口中心对齐节点中心，越界 clamp（翻转不做，纵向上下都有窗口语义）
  let y = ay - winH / 2
  if (winH >= vh) y = 0
  else if (y + winH > vh) y = vh - winH
  else if (y < 0) y = 0
  return { x, y, side: rightFits || !leftFits ? 'right' : 'left' }
}
