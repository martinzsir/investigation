// 持有链「蛇形」布局（WIN-11）。
//
// 为什么现在才做
// --------------
// 第一版（E1–E20 验收）之前裁定「本版不调整」，理由是：链长分布、有无回环、
// 节点规模三项实测数据都没拿到，提前引入自定义布局是**为假想问题付代价**。
// 现在链型已经能真实产出，且下面三条判据已经落死在 buildHoldingWindow，
// 布局才有了事实依据，不再是选型偏好。
//
// 蛇形到底是什么（重要，别搞混）
// ----------------------------
// G6 的 snake-flow-diagram 里，「蛇形」不是内置布局算法——是两件事叠加：
//
//   1. 节点按行**折返排布**（第一行左→右，第二行右→左，依次蛇行）；
//   2. 边用自定义 **S 型折线**（`class SnakePolyline extends Polyline`，
//      重写 getEndpoints 取值点后走行间空隙）。
//
// 所以本文件同样拆两块：坐标计算（纯函数，可脱离 G6 单测）+ 折线 path 生成
// （纯字符串，可单测）。**渲染层用什么引擎与本文件无关**——窗口是 DOM 浮层，
// 用内联 SVG 画即可；换成 G6 只需换渲染层，布局不用动。
//
// 为什么不在浮窗里再开一个 G6 Graph 实例
// -------------------------------------
// 主画布已经有一个 Graph 实例，浮窗内再开第二个要额外管生命周期（关窗销毁、
// resize 转发、坐标投影各一套），而持有链通常只有几手节点、无需力导向与
// 交互图语义。蛇形的核心是**坐标与绕行路径**，SVG 精确可控且零实例开销。
//
// 三条红线（改动前先看这里）
// ------------------------
//   1. 时间不可判定的环节**不进链序**。把它按 start 排进链里（空串自然排到
//      最前/最后）会给出一条**虚假的链序**——与「重名不自裁」同源：不知道
//      就不排。这类环节单独成行，且不连任何边。
//   2. 错序**不自动重排**。按时间重排会把「登记顺序与登记时间矛盾」这个信号
//      抹掉，而那正是虚假登记、物证链断裂的线索。链按登记顺序画，错序标在边上。
//   3. 非相邻边**必须绕行**。同一时刻被多方持有（重叠）画的边跨越中间环节，
//      直线会穿过中间节点，看起来像「中间那手也参与了这次重叠」。走行间空隙
//      的 S 型折线是这个问题的唯一解，不是美观选择。

/** 每行环节数。实测链多为 3~6 手；3 列时窗口内两行即容纳 6 手，不需滚动 */
export const CHAIN_COLS = 3
/** 环节盒宽（px；卡内要放持有人名 + 区间两行） */
export const CHAIN_NODE_W = 132
/** 环节盒高 */
export const CHAIN_NODE_H = 46
/** 同行相邻盒水平间距 */
export const CHAIN_GAP_X = 28
/**
 * 行间空隙（px）。**不能压小**——这是 S 型折线的走线通道，
 * 折返边与重叠边都从这里过；压到贴边会让两条边视觉粘连。
 */
export const CHAIN_GAP_Y = 44
/** SVG 四周留白：给箭头、端点圆留出空间 */
export const CHAIN_PAD = 12
/** 折线圆角半径（px），实际取值会按段长收敛，避免短段圆角过冲 */
export const CHAIN_CORNER_R = 8

/** 进入布局的一个持有环节 */
export interface ChainStep {
  /** 稳定 id（同 holder 多次持有时不冲突） */
  id: string
  holder: string
  /** 起始时间（已归一化；空串=不可判定） */
  start: string
  /** 结束时间（空串=仍持有 或 不可判定） */
  end: string
  /** 登记顺序——链按它排，不按时间排（见红线 2） */
  order: number
  /** 时间区间不可判定：不是"没有"，是"不知道" */
  unknownRange: boolean
}

export interface ChainNodeBox {
  id: string
  holder: string
  /** 左上角坐标（SVG 视口内） */
  x: number
  y: number
  w: number
  h: number
  row: number
  col: number
  /** 蛇形行进方向：1=本行左→右；-1=本行右→左（折返行） */
  dir: 1 | -1
  start: string
  end: string
  unknownRange: boolean
  /** 显示用文案：无结束时间=在持 */
  rangeText: string
}

export interface ChainEdgeBox {
  from: string
  to: string
  /** SVG path d */
  d: string
  /** transfer=相邻流转；overlap=区间重叠（同物同时被多方持有） */
  kind: 'transfer' | 'overlap'
  /** 后一手开始早于前一手——登记顺序与登记时间矛盾 */
  outOfOrder: boolean
  /** 该边是否走了绕行通道（重叠边恒为 true） */
  detoured: boolean
}

export interface SnakeLayout {
  nodes: ChainNodeBox[]
  /** 时间不可判定、未进链序的环节（红线 1） */
  detached: ChainNodeBox[]
  edges: ChainEdgeBox[]
  width: number
  height: number
  rows: number
}

/** 区间显示文案。**不给文案的区间会被读成"没有时间"**——与精度自陈同源 */
export function rangeTextOf(start: string, end: string, unknown: boolean): string {
  if (unknown || !start) return '时间不可判定'
  return `${start} → ${end ? end : '在持'}`
}

/**
 * 按登记顺序拆分：可判定进链，不可判定单独成行。
 * 返回的两段都**保持登记顺序**，不做时间排序（红线 2）。
 */
export function splitChainSteps(steps: ChainStep[]): {
  ordered: ChainStep[]
  detached: ChainStep[]
} {
  const ordered: ChainStep[] = []
  const detached: ChainStep[] = []
  for (const s of steps) {
    if (s.unknownRange || !s.start) detached.push(s)
    else ordered.push(s)
  }
  return { ordered, detached }
}

/**
 * 区间重叠对（同一时刻被多方持有）。
 * 任一侧缺 start/end 就**不参与判定**——缺时间不是"不重叠"，是"不知道"，
 * 把它当不重叠会让冲突静默消失（这比误报更危险：误报还能人工排除）。
 */
export function chainOverlapPairs(steps: ChainStep[]): Array<[string, string]> {
  const pairs: Array<[string, string]> = []
  const usable = steps.filter((s) => !s.unknownRange && s.start && s.end)
  for (let i = 0; i < usable.length; i += 1) {
    for (let j = i + 1; j < usable.length; j += 1) {
      const a = usable[i]
      const b = usable[j]
      if (a.start <= b.end && b.start <= a.end) pairs.push([a.id, b.id])
    }
  }
  return pairs
}

/** 蛇形坐标：偶数行左→右，奇数行右→左（折返）。 */
export function placeSnake(step: ChainStep, idx: number, cols: number): {
  row: number
  col: number
  dir: 1 | -1
  x: number
  y: number
} {
  const row = Math.floor(idx / cols)
  const col = idx % cols
  const dir: 1 | -1 = row % 2 === 0 ? 1 : -1
  // 折返行：col 增大而 x 减小，视觉上行进方向从右向左
  const effCol = dir === 1 ? col : cols - 1 - col
  const x = CHAIN_PAD + effCol * (CHAIN_NODE_W + CHAIN_GAP_X)
  const y = CHAIN_PAD + row * (CHAIN_NODE_H + CHAIN_GAP_Y)
  return { row, col: effCol, dir, x, y }
}

/**
 * S 型折线（阶梯 + 圆角），走行间空隙，不穿过任何节点盒。
 *
 * 走线规则：
 *   - 同 y（水平段）：直接连；
 *   - 否则：从起点垂直走到 `midY` 通道 → 水平移到目标 x → 垂直进入终点。
 *     通道取**两行之间的空隙中线**，这是唯一不与节点盒相交的横向带。
 *
 * @param midY 走线通道的 y（行间空隙中线）
 */
export function snakePath(
  ax: number,
  ay: number,
  bx: number,
  by: number,
  midY: number,
): string {
  const dx = bx - ax
  // 纯垂直：折返处（行尾→下行行首）恒为这条，不需要绕行
  if (Math.abs(dx) < 0.5) return `M ${r2(ax)} ${r2(ay)} L ${r2(bx)} ${r2(by)}`
  // 已在通道上或极近：直接横过去再进，不画无意义的折角
  if (Math.abs(ay - midY) < 0.5 && Math.abs(by - midY) < 0.5) {
    return `M ${r2(ax)} ${r2(ay)} L ${r2(bx)} ${r2(by)}`
  }
  const dirH = dx > 0 ? 1 : -1
  const dirIn = midY > ay ? 1 : -1
  const dirOut = by > midY ? 1 : -1
  // 圆角按最短段收敛，否则短段会出现过冲的怪异曲线
  const r = Math.min(
    CHAIN_CORNER_R,
    Math.abs(midY - ay) / 2,
    Math.abs(by - midY) / 2,
    Math.abs(dx) / 2,
  )
  const rr = r < 1 ? 0 : r
  if (rr === 0) {
    return `M ${r2(ax)} ${r2(ay)} L ${r2(ax)} ${r2(midY)} L ${r2(bx)} ${r2(midY)} L ${r2(bx)} ${r2(by)}`
  }
  return [
    `M ${r2(ax)} ${r2(ay)}`,
    `L ${r2(ax)} ${r2(midY - dirIn * rr)}`,
    `Q ${r2(ax)} ${r2(midY)} ${r2(ax + dirH * rr)} ${r2(midY)}`,
    `L ${r2(bx - dirH * rr)} ${r2(midY)}`,
    `Q ${r2(bx)} ${r2(midY)} ${r2(bx)} ${r2(midY + dirOut * rr)}`,
    `L ${r2(bx)} ${r2(by)}`,
  ].join(' ')
}

function r2(n: number): number {
  return Math.round(n * 100) / 100
}

/** 行间空隙中线（第 row 行**下方**那条通道） */
export function channelYOf(row: number): number {
  return CHAIN_PAD + row * (CHAIN_NODE_H + CHAIN_GAP_Y) + CHAIN_NODE_H + CHAIN_GAP_Y / 2
}

/**
 * 装配完整蛇形布局。
 *
 * 边分两类：
 *   - 相邻流转（i → i+1）：同行走水平直线，折返处走垂直直线，**不绕行**——
 *     相邻边本来就该紧挨着，绕行反而看不出"紧接在这一手之后"。
 *   - 重叠边（非相邻）：恒走行间通道绕行（红线 3）。
 */
export function buildChainLayout(
  steps: ChainStep[],
  opts?: { cols?: number },
): SnakeLayout {
  const cols = Math.max(1, Math.floor(opts?.cols ?? CHAIN_COLS))
  const { ordered, detached } = splitChainSteps(steps)

  const nodes: ChainNodeBox[] = ordered.map((s, i) => {
    const p = placeSnake(s, i, cols)
    return {
      id: s.id,
      holder: s.holder,
      x: p.x,
      y: p.y,
      w: CHAIN_NODE_W,
      h: CHAIN_NODE_H,
      row: p.row,
      col: p.col,
      dir: p.dir,
      start: s.start,
      end: s.end,
      unknownRange: s.unknownRange,
      rangeText: rangeTextOf(s.start, s.end, s.unknownRange),
    }
  })

  // 不可判定环节：链下方单独成行，**不连任何边**（红线 1）
  const rows = Math.max(1, Math.ceil(ordered.length / cols))
  const detachedNodes: ChainNodeBox[] = detached.map((s, i) => ({
    id: s.id,
    holder: s.holder,
    x: CHAIN_PAD + i * (CHAIN_NODE_W + CHAIN_GAP_X),
    y: CHAIN_PAD + rows * (CHAIN_NODE_H + CHAIN_GAP_Y) + CHAIN_GAP_Y,
    w: CHAIN_NODE_W,
    h: CHAIN_NODE_H,
    row: rows,
    col: i,
    dir: 1,
    start: s.start,
    end: s.end,
    unknownRange: true,
    rangeText: rangeTextOf(s.start, s.end, true),
  }))

  const byId = new Map<string, ChainNodeBox>()
  for (const n of nodes) byId.set(n.id, n)

  const edges: ChainEdgeBox[] = []
  // 相邻流转边
  for (let i = 0; i + 1 < nodes.length; i += 1) {
    const a = nodes[i]
    const b = nodes[i + 1]
    const sameRow = a.row === b.row
    const ax = sameRow ? a.dir === 1 ? a.x + a.w : a.x : a.x + a.w / 2
    const bx = sameRow ? b.dir === 1 ? b.x : b.x + b.w : b.x + b.w / 2
    const ay = sameRow ? a.y + a.h / 2 : a.y + a.h
    const by = sameRow ? b.y + b.h / 2 : b.y
    const d =
      sameRow || Math.abs(ax - bx) < 0.5
        ? `M ${r2(ax)} ${r2(ay)} L ${r2(bx)} ${r2(by)}`
        : snakePath(ax, ay, bx, by, channelYOf(a.row))
    const prevStart = ordered[i]?.start ?? ''
    const curStart = ordered[i + 1]?.start ?? ''
    edges.push({
      from: a.id,
      to: b.id,
      d,
      kind: 'transfer',
      outOfOrder: !!prevStart && !!curStart && curStart < prevStart,
      detoured: false,
    })
  }

  // 重叠边：恒绕行（红线 3）。通道取两行之间靠上那条，避免压到最后一行下方
  const overlaps = chainOverlapPairs(ordered)
  for (const [from, to] of overlaps) {
    const a = byId.get(from)
    const b = byId.get(to)
    if (!a || !b) continue
    const ax = a.x + a.w / 2
    const bx = b.x + b.w / 2
    const ay = a.y + a.h
    const by = b.y // 从下方进入 b 的底边？b 在 a 之后，若同行则从其底部进入
    const sameRow = a.row === b.row
    const channel = channelYOf(Math.min(a.row, b.row))
    const d = sameRow
      ? // 同行非相邻：从 a 底部下到通道 → 横移 → 回到 b 底部（绕开中间节点）
        snakePath(ax, a.y + a.h, bx, b.y + b.h, channel)
      : snakePath(ax, ay, bx, by, channel)
    edges.push({
      from: a.id,
      to: b.id,
      d,
      kind: 'overlap',
      outOfOrder: false,
      detoured: true,
    })
  }

  const width =
    CHAIN_PAD * 2 + cols * CHAIN_NODE_W + (cols - 1) * CHAIN_GAP_X
  const totalRows = rows + (detached.length ? 1 : 0)
  const height =
    CHAIN_PAD * 2 + totalRows * CHAIN_NODE_H + Math.max(0, totalRows - 1) * CHAIN_GAP_Y +
    (detached.length ? CHAIN_GAP_Y : 0)

  return { nodes, detached: detachedNodes, edges, width, height, rows: totalRows }
}
