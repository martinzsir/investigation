// 画布初始视口纯函数（不依赖 G6，可单测）。
//
// 背景：G6 的 `autoFit: 'view'` 按「全部元素 bbox」缩放，会把 10 个泳道伪节点
// （band 高 = maxY+30、head 在内容上方）一起算进去，节点纵向一多 bbox 就破千，
// 卡片 186×50 的 12px 标题被压到 5~6px 完全不可读。
// 这里改为「只按真实节点 bbox 计算缩放」，泳道不参与，并按可读下限 clamp。

/** 卡片尺寸（与 g6-card-node.ts CARD_WIDTH/CARD_HEIGHT 同口径） */
export const CARD_WIDTH = 186
export const CARD_HEIGHT = 50
/** 低于此缩放卡片文字不可读（12px × 0.7 ≈ 8.4px） */
export const VIEWPORT_MIN_ZOOM = 0.7
/** 初始不放大超过 1（放大只由用户主动触发） */
export const VIEWPORT_MAX_ZOOM = 1
/** 内容四周留白（视口像素） */
export const VIEWPORT_PADDING = 48

export interface ContentBox {
  minX: number
  minY: number
  maxX: number
  maxY: number
}

/** 真实节点（不含泳道伪节点）的内容包围盒；无节点返回 null */
export function contentBox(
  nodes: ReadonlyArray<{ x: number; y: number }>,
): ContentBox | null {
  if (!nodes.length) return null
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const n of nodes) {
    if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) continue
    minX = Math.min(minX, n.x - CARD_WIDTH / 2)
    maxX = Math.max(maxX, n.x + CARD_WIDTH / 2)
    minY = Math.min(minY, n.y - CARD_HEIGHT / 2)
    maxY = Math.max(maxY, n.y + CARD_HEIGHT / 2)
  }
  if (minX === Infinity) return null
  return { minX, minY, maxX, maxY }
}

/** 内容中心（画布坐标），居中定位用 */
export function contentCenter(box: ContentBox | null): [number, number] {
  if (!box) return [0, 0]
  return [(box.minX + box.maxX) / 2, (box.minY + box.maxY) / 2]
}

/** 按视口尺寸算适配缩放，并 clamp 到可读区间 */
export function fitScale(
  box: ContentBox | null,
  viewWidth: number,
  viewHeight: number,
  padding: number = VIEWPORT_PADDING,
): number {
  if (!box || viewWidth <= 0 || viewHeight <= 0) return VIEWPORT_MAX_ZOOM
  const w = Math.max(box.maxX - box.minX, 1)
  const h = Math.max(box.maxY - box.minY, 1)
  const raw = Math.min(
    (viewWidth - padding * 2) / w,
    (viewHeight - padding * 2) / h,
  )
  if (!Number.isFinite(raw) || raw <= 0) return VIEWPORT_MAX_ZOOM
  return Math.min(VIEWPORT_MAX_ZOOM, Math.max(VIEWPORT_MIN_ZOOM, raw))
}

/** 缩放百分比（状态栏展示） */
export function zoomPercent(zoom: number): number {
  if (!Number.isFinite(zoom) || zoom <= 0) return 100
  return Math.round(zoom * 100)
}

// ---------------------------------------------------------------------
// 泳道几何：列头/色带贴着内容包围盒生成，避免把视口 bbox 撑大
// ---------------------------------------------------------------------
export const LANE_HEAD_HEIGHT = 26
/** 列头底边到内容顶边的留白 */
const LANE_HEAD_GAP = 34
/** 色带上下各外扩的余量 */
const LANE_BAND_PAD = 30

export interface LaneGeometry {
  /** 列头矩形中心 y */
  headY: number
  /** 色带矩形中心 y */
  bandCenterY: number
  /** 色带高度 */
  bandHeight: number
}

// ---------------------------------------------------------------------
// P2 视口矩形 / minimap 几何（等比利特盒居中；画布坐标 ↔ 缩略图像素）
//
// 为什么自己算：G6 v5 已移除 v4 的 minimap 插件（lib/plugins 下没有），
// 只保留坐标换算原语——缩略图用「内容 bbox → 一个小矩形」的纯映射即可，
// 不值得为此挂第二个 Graph 实例（双倍渲染成本 + 双份事件）。
// ---------------------------------------------------------------------
export interface ViewportRect {
  /** 视口左上角对应的画布坐标 */
  x: number
  y: number
  /** 视口宽高（画布坐标；= 容器像素 / zoom） */
  w: number
  h: number
}

/**
 * 当前可见区域（画布坐标）。
 * G6 的 getCanvasByViewport([0,0]) 直接给出视口左上角的画布坐标；
 * 拿不到时可按 getViewportByCanvas([0,0]) 反推（v = c·zoom + t ⇒ c = −t/zoom）。
 */
export function viewportRect(
  topLeft: [number, number] | null,
  viewWidth: number,
  viewHeight: number,
  zoom: number,
): ViewportRect | null {
  if (!topLeft || !Number.isFinite(zoom) || zoom <= 0) return null
  if (viewWidth <= 0 || viewHeight <= 0) return null
  const [x, y] = topLeft
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  return { x, y, w: viewWidth / zoom, h: viewHeight / zoom }
}

/** 缩略图四周留白（像素） */
export const MINIMAP_PAD = 6

export interface MinimapFrame {
  scale: number
  /** letterbox 居中后的绘制原点（像素） */
  offX: number
  offY: number
}

/** 内容 bbox → 缩略图的等比映射（letterbox 居中，不拉伸） */
export function minimapFrame(
  box: ContentBox | null,
  mapWidth: number,
  mapHeight: number,
  pad: number = MINIMAP_PAD,
): MinimapFrame | null {
  if (!box || mapWidth <= 0 || mapHeight <= 0) return null
  const w = Math.max(box.maxX - box.minX, 1)
  const h = Math.max(box.maxY - box.minY, 1)
  const scale = Math.min(
    (mapWidth - pad * 2) / w,
    (mapHeight - pad * 2) / h,
  )
  if (!Number.isFinite(scale) || scale <= 0) return null
  return {
    scale,
    offX: (mapWidth - w * scale) / 2,
    offY: (mapHeight - h * scale) / 2,
  }
}

/** 画布坐标 → 缩略图像素 */
export function minimapProject(
  frame: MinimapFrame | null,
  box: ContentBox | null,
  x: number,
  y: number,
): [number, number] {
  if (!frame || !box) return [0, 0]
  return [
    frame.offX + (x - box.minX) * frame.scale,
    frame.offY + (y - box.minY) * frame.scale,
  ]
}

/** 缩略图像素 → 画布坐标（点击定位） */
export function minimapUnproject(
  frame: MinimapFrame | null,
  box: ContentBox | null,
  px: number,
  py: number,
): [number, number] {
  if (!frame || !box || frame.scale <= 0) return [0, 0]
  return [
    box.minX + (px - frame.offX) / frame.scale,
    box.minY + (py - frame.offY) / frame.scale,
  ]
}

export function laneGeometry(box: ContentBox | null): LaneGeometry {
  const minY = box?.minY ?? 0
  const maxY = box?.maxY ?? 120
  const bandTop = minY - LANE_BAND_PAD
  const bandBottom = maxY + LANE_BAND_PAD
  return {
    headY: minY - LANE_HEAD_GAP - LANE_HEAD_HEIGHT / 2,
    bandCenterY: (bandTop + bandBottom) / 2,
    bandHeight: Math.max(bandBottom - bandTop, 120),
  }
}
