// 研判画布「符号口径」唯一真相源（SYM-01）。
//
// 为什么必须收在一处
// ------------------
// 同一条证据会在四处被画出来：卡内迷你符号、画布连线、节点窗口、地图。
// 三处各写一份颜色/线型/半径**必然分叉**——交汇侧已经踩过一次（离线 SVG 半径
// 硬编码 5/7/9，在线适配器另写一套）。而下面两条都不是样式偏好，是**误判源**：
//
//   1. date 档画成实线 → 12 条日期级（只知前后一天，同地异时）与 4 条分钟级
//      （时间窗真重叠）在图上长得一样，精度加权被一张漂亮图重新抹平。
//   2. 区划质心画成实心 → 把"区级推算"伪装成门牌级真实位置；地图天生看上去
//      精确，误导性比列表更强。
//
// 本文件是纯数据 + 纯函数，不含 vue/G6 依赖，可脱离 canvas 单测。
// 权重与后端 core/convergence.py 的 PRECISION_WEIGHT 对拍（见
// tests/test_canvas_symbol_sync.py），任一侧改动而另一侧不动即失败。

export type Precision = 'second' | 'minute' | 'hour' | 'date' | 'unknown'

/** 与后端同口径：只有能判定"时间窗真重叠"的档位才给满分 */
export const PRECISION_WEIGHT: Record<Precision, number> = {
  second: 1.0,
  minute: 1.0,
  hour: 0.5,
  date: 0.2,
  unknown: 0.1,
}

export const PRECISION_LABEL: Record<Precision, string> = {
  second: '秒级',
  minute: '分钟级',
  hour: '小时级',
  date: '日期级',
  unknown: '精度未知',
}

/** 精度自陈文案——任何一处呈现都不得只给数字而不给这句话 */
export const PRECISION_NOTE: Record<Precision, string> = {
  second: '可判定时间窗真重叠',
  minute: '可判定时间窗真重叠',
  hour: '仅能判定同一小时，时间窗未必重叠',
  date: '仅知前后一天先后出现，是同地异时、非同时',
  unknown: '时间精度不可判定',
}

/**
 * 精度强弱序（小=强）。用于**同分兜底**——
 * 加权分相同时必须让精度高者在前；若退回"次数多者在前"，
 * 「次数会骗人」就从后门重新钻了进来。
 */
export const PRECISION_RANK: Record<Precision, number> = {
  second: 0,
  minute: 1,
  hour: 2,
  date: 3,
  unknown: 4,
}

/** 归一化：后端可能给出任意字符串，未知档一律回落 unknown（不猜） */
export function asPrecision(v: string | undefined | null): Precision {
  const s = String(v ?? '').trim()
  if (s === 'second' || s === 'minute' || s === 'hour' || s === 'date') return s
  return 'unknown'
}

/** 能否判定时间窗真重叠——决定实线还是虚线 */
export function isOverlapDeterminable(p: Precision): boolean {
  return p === 'second' || p === 'minute'
}

/** 精度档色（与 convergence-graph 同色板，避免两处调色分叉） */
export const PRECISION_INK: Record<Precision, string> = {
  second: '#18a058',
  minute: '#18a058',
  hour: '#2f7ed8',
  date: '#f0a020',
  unknown: '#999999',
}

// ------------------------------------------------------------------ //
// 连线（SYM-02 / SYM-05）
// ------------------------------------------------------------------ //

export interface EdgeSymbolInput {
  /** 证据精度档 */
  precision?: string
  /** true=系统推断边；false=人工确认边 */
  system?: boolean
  /** 加权分（仅作线宽参考，不单独表达强度） */
  weight?: number
}

export interface EdgeSymbol {
  lineDash: number[]
  lineWidth: number
  stroke: string
  /** 屏幕提示：线型在说什么 */
  note: string
}

/**
 * 精度 → 线型（SYM-02）：实线粗边=可判时间窗重叠；虚线细边=仅同地异时。
 * 人机来源 → 灰/黑区分（SYM-05）：系统推断边灰细，人工确认边实粗。
 *
 * 两者叠加时**取更保守者**：系统推断的分钟级证据仍是虚线灰边——未经正兵
 * 认可的结论不该长得像已确认结论。
 */
export function edgeSymbol(input: EdgeSymbolInput): EdgeSymbol {
  const p = asPrecision(input.precision)
  const system = input.system === true
  const determinable = isOverlapDeterminable(p)
  // 虚线两个来源：精度不足(date/unknown) 或 未经人工确认(system)
  const dashed = !determinable || system
  const lineWidth = !determinable ? 1 : system ? 1.5 : 2 + Math.min(2, Math.round((input.weight ?? 1) * 0.5))
  const stroke = system ? '#9aa5b1' : PRECISION_INK[p]
  const note = system
    ? '系统推断（未经人工确认）'
    : PRECISION_NOTE[p]
  return {
    lineDash: dashed ? [4, 3] : [],
    lineWidth,
    stroke,
    note,
  }
}

// ------------------------------------------------------------------ //
// 点（SYM-03）
// ------------------------------------------------------------------ //

export interface PointSymbolInput {
  /** true=门牌级真实坐标；false/null=区划质心（区级推算） */
  precise?: boolean
  /** 命中维数 → 半径分档 */
  maxDimHit?: number
  ambiguous?: boolean
}

export interface PointSymbol {
  fill: string
  /** 质心恒为 0——空心是红线，不是样式 */
  fillOpacity: number
  stroke: string
  strokeWidth: number
  strokeDash: number[]
  radius: number
}

/** 命中维数 → 半径（px）：1→6、2→8、3→11，与 convergence-geo 的 MAP_SYMBOL 同值 */
export function radiusOfDimHit(maxDimHit: number): number {
  return maxDimHit >= 3 ? 11 : maxDimHit === 2 ? 8 : 6
}

export function pointSymbol(input: PointSymbolInput): PointSymbol {
  const precise = input.precise === true
  const ambiguous = input.ambiguous === true
  return {
    fill: precise ? '#2f7ed8' : 'transparent',
    fillOpacity: precise ? 0.85 : 0,
    stroke: ambiguous ? '#b4781a' : '#33475b',
    strokeWidth: ambiguous ? 2 : 1.5,
    strokeDash: ambiguous ? [3, 2] : [],
    radius: radiusOfDimHit(input.maxDimHit ?? 1),
  }
}

// ------------------------------------------------------------------ //
// 卡内迷你符号（WIN-01 / SYM-04）
// ------------------------------------------------------------------ //

export interface NodeSymbolInput {
  /** 重名待裁决 */
  ambiguous?: boolean
  precision?: string
  /** 未锚定主键（跑不出任何研判） */
  unanchored?: boolean
}

export interface NodeSymbol {
  /** 卡面色点：精度色；未锚定给灰 */
  dimDotColor: string
  /** 卡面描边线型：重名虚线 */
  lineDash: number[]
  /** 标题前缀：重名 `?`，未锚定 `·` */
  labelPrefix: string
  /** 卡内第二行（副标题）自陈文案 */
  note: string
}

/**
 * 卡内符号（186×50 内可辨）：一个色点 + 描边线型 + 标题前缀。
 * 信息量刻意小——卡片只负责"这个节点有内容、是什么精度、能不能信"，
 * 完整可视化放在点开后的窗口里（WIN-02）。
 */
export function nodeSymbol(input: NodeSymbolInput): NodeSymbol {
  const p = asPrecision(input.precision)
  const ambiguous = input.ambiguous === true
  const unanchored = input.unanchored === true
  const prefix = ambiguous ? '?' : unanchored ? '·' : ''
  const note = ambiguous
    ? '重名待裁决，未锚定到唯一主键'
    : unanchored
      ? '未锚定主键，无可用研判'
      : PRECISION_NOTE[p]
  return {
    dimDotColor: unanchored ? '#999999' : PRECISION_INK[p],
    lineDash: ambiguous || unanchored ? [4, 3] : [],
    labelPrefix: prefix,
    note,
  }
}

// ------------------------------------------------------------------ //
// 三维矩阵（SYM-06）：分数不单独表态
// ------------------------------------------------------------------ //

export interface DimCell {
  dim: string
  dimLabel: string
  hit: boolean
  count: number
  precision: Precision
  weight: number
  /** 未命中时必须写明原因，不得由其他维度热度盖过去 */
  reason?: string
}

export const DIM_LABELS: Record<string, string> = {
  space: '空间',
  time: '时间',
  relation: '关系',
}

/**
 * 单维可读摘要：`空间 4 条/分钟级 权 1.6`。
 * 条数与精度档必须同现——只给"12 条"会让人以为它比"4 条/分钟级"更重。
 */
export function describeDim(c: DimCell): string {
  if (!c.hit) return `${c.dimLabel}：${c.reason || '该维度无命中'}`
  return `${c.dimLabel} ${c.count} 条/${PRECISION_LABEL[c.precision]} 权 ${round2(c.weight)}`
}

export function round2(v: number): number {
  return Math.round(v * 100) / 100
}

/**
 * 分数能否单独呈现（SYM-06 的判据）。
 * 只要存在任一命中维度缺 count/precision/weight，就不允许只给总分。
 */
export function scoreNeverAlone(cells: DimCell[], score: number): {
  ok: boolean
  reason: string
} {
  if (cells.length === 0) {
    return { ok: false, reason: '无维度明细，分数不得单独呈现' }
  }
  const hit = cells.filter((c) => c.hit)
  for (const c of hit) {
    // 用 String() 兜住脏数据：后端可能给空串，类型上不合法但运行期会出现
    if (String(c.precision ?? '') === '' || typeof c.weight !== 'number') {
      return { ok: false, reason: `维度 ${c.dim} 缺 precision/weight，分数不得单独呈现` }
    }
  }
  if (typeof score !== 'number' || Number.isNaN(score)) {
    return { ok: false, reason: '分数非数值' }
  }
  return { ok: true, reason: '' }
}

/**
 * 重复出现加成（与后端 core/convergence.py 同口径，缺一即与后端算分分叉）。
 *
 * 为什么必须有上限：没有 cap 时，次数堆砌能重新压过精度——
 * 15 次 × 0.2 = 3.0 正好等于 3 次 × 1.0，**"3 次分量远重于 15 次"就不成立了**。
 * 加了 cap 之后：15 次日期级 = 0.2 × 3.0 = 0.6，3 次时刻级 = 1.0 × 1.4 = 1.4。
 * 这条不是微调，是"次数会骗人"能否真正落地的关键。
 */
export const REPEAT_STEP = 0.2
export const REPEAT_CAP = 3.0

export function repeatFactor(count: number): number {
  const n = Math.max(0, Number(count) || 0)
  return Math.min(1 + REPEAT_STEP * (n - 1), REPEAT_CAP)
}

/**
 * 跨精度比较用的加权值：**必须**与后端 `_dim_score` 同式
 * （dim_weight 三维平权，恒为 1，故此处省略）。
 *
 * 前端若图省事写成 `count × weight`，界面给出的"加权分"就会与后端排序用的
 * 分数不一致：同一条证据，列表按后端分排第三，图上却画得最粗。
 */
export function weightedOf(count: number, precision: string): number {
  return round2(PRECISION_WEIGHT[asPrecision(precision)] * repeatFactor(count))
}

// ------------------------------------------------------------------ //
// 持有链边（WIN-11 / SYM-06）
// ------------------------------------------------------------------ //

/**
 * 链冲突色。**这是新增色**（项目色板里原本没有红），理由必须写清：
 * "同一时刻被多方持有"是链冲突，严重性高于"日期级证据精度不足"，
 * 若复用 date 档的橙，两种性质完全不同的告警会长得一样。
 * 多处各写一份色值是符号分叉的起点，所以收在这里，与 PRECISION_INK 同处。
 */
export const CHAIN_CONFLICT_INK = '#e54545'

export interface ChainEdgeSymbolInput {
  /** transfer=相邻流转；overlap=区间重叠 */
  kind: 'transfer' | 'overlap'
  /** 后一手登记时间早于前一手 */
  outOfOrder: boolean
}

export interface ChainEdgeSymbol {
  stroke: string
  lineDash: number[]
  lineWidth: number
  /** 图例与悬浮提示**都取它**——不另写文案，否则图例和线各说一套 */
  note: string
}

/**
 * 持有链边符号。沿用精度边那三条规矩：
 *   1. 异常态**必须虚线**：错序与冲突都属于"需人工判"，
 *      长得像已确认流转会让人直接跳过；
 *   2. 色与线型成对出现，不单靠色——色觉障碍下红橙难分，虚线是冗余通道；
 *   3. 必给 note，图例直接渲染它。
 */
export function chainEdgeSymbol(input: ChainEdgeSymbolInput): ChainEdgeSymbol {
  if (input.kind === 'overlap') {
    return {
      stroke: CHAIN_CONFLICT_INK,
      lineDash: [4, 3],
      lineWidth: 1.8,
      note: '区间重叠：同一时刻被多方持有，需人工判定',
    }
  }
  if (input.outOfOrder) {
    return {
      stroke: PRECISION_INK.date,
      lineDash: [4, 3],
      lineWidth: 1.6,
      note: '时序错序：后一手登记时间早于前一手，请核对',
    }
  }
  return {
    stroke: PRECISION_INK.minute,
    lineDash: [],
    lineWidth: 2,
    note: '流转：相邻两手的持有移交',
  }
}
