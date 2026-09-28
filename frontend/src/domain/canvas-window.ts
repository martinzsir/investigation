// 研判层画布窗口符号口径单一来源（PRD V1.0.0 红线 R1 / REQ-P0-01、02）。
// 精度档→符号（线型/线宽/填充/色）与节点 kind→窗口类型路由表只在本文件定义；
// 卡内迷你符号（g6-card-node）与五类分型窗口（components/research/win）一律消费本模块，
// 禁止在别处二次硬编码线型/填充判断——否则「12 条 date 档」和「4 条 minute 档」
// 在图上长得一样，前面的精度加权会被一张漂亮图抹平。
// 注意：subject/place/event/analysis_result 四类 kind 在 P1 才进入 canvas.ts NODE_KINDS，
// 本文件的路由表先行声明，P1 落地时与 NODE_KINDS 对齐。

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
 */
export const AMBIGUOUS_BORDER = {
  lineDash: [5, 3],
  strokeWidth: 1.5,
  marker: '?',
  color: '#E8A23C',
} as const

/** 窗口类型（PRD 功能 3：每种节点打开它自己那一维的东西） */
export type WindowKind = 'relation' | 'map' | 'time' | 'evidence' | 'hypothesis' | 'source'

/**
 * 节点 kind → 窗口类型路由表：
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

// ======================================================================
// P2 卡内迷你符号（PRD 功能 2）：时间条几何与精度点同源消费 precisionSymbol。
// 渲染方（g6-card-node / 窗口）只画规格，不判断精度档——判断只在本文件。
// ======================================================================

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

/** 窗口类型 → 放大目标路由（无放大入口的窗口返回 null） */
const WINDOW_ENLARGE_ROUTE: Record<Exclude<WindowKind, 'source'>, string> = {
  relation: '/c/graph',
  map: '/c/geo',
  time: '/c/convergence',
  evidence: '/c/convergence',
  hypothesis: '',
}

/**
 * 窗口放大入口目标（PRD 功能 3：跳对应全局视图）。
 * hypothesis 窗口无全局视图（假设是画布本域对象）返回空串；
 * source 沿用溯源弹层无放大。调用方对空串不渲染放大按钮。
 */
export function windowEnlargeRoute(kind: WindowKind | null): string {
  if (!kind || kind === 'source') return ''
  return WINDOW_ENLARGE_ROUTE[kind] ?? ''
}

// ======================================================================
// P2 浮窗贴附定位（PRD 功能 3 验收点：定位正确、越界翻转、视口跟随）。
// 纯函数不依赖 DOM，供 CanvasNodeWindow 消费与单测。
// ======================================================================

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
