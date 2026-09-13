<script setup lang="ts">
// RC-101/207/205 线索研判画布（M1 成图 + 自动保存底座）
// + M2 RC-102 规则双视图抽屉 / RC-103 逐层溯源懒加载四态机 / RC-104 遮蔽复用
// + M3 RC-201 钉住与分层重排 / RC-202 人工节点增删改 / RC-203 连线矩阵
//        / RC-206 快照与回滚。
// 纪律与 GraphCanvas 一致：G6 经动态 import 装载；装载/渲染失败降级
// 「节点-关系列表」（禁空白）；结构写操作一律走专用端点（逐动作审计），
// 坐标/钉住走防抖 PATCH 白名单；组件不直读数据层、不生成自由 SQL。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { NAlert, NButton, NIcon, NSpin, useDialog, useMessage } from 'naive-ui'
import { RefreshOutline } from '@vicons/ionicons5'
import { canvasApi } from '../../api/endpoints/canvas'
import { waitForTerminal } from '../../api/endpoints/tasks'
import { ApiError, presentError } from '../../api/errors'
import {
  allowedRels,
  defaultDirection,
  findEdge,
  incidentManualEdges,
  isManualNode,
  isSuggestionNode,
  KIND_LABELS,
  KIND_ORDER,
  type AdoptUiState,
  type CanvasDoc,
  type CanvasEdge,
  type CanvasEnvelope,
  type CanvasNode,
  type CanvasSnapshot,
  type ExpandDirection,
  type ExpandEnvelope,
  type ExpandNotice,
  type ExpandUiState,
  type FunctionForm,
  type FunctionQuerySkippedEnvelope,
  type ManualNodeKind,
  type ManualRel,
  type NodeDetail,
  type NodeKind,
  type RuleAudit,
} from '../../domain/canvas'
import { RANK_X, layoutNodes } from '../../domain/canvas-layout'
import {
  TIER_LABELS,
  TIER_3_OPACITY,
  layoutByTier,
  tierCounts as tierCountsOf,
  tierOf,
  type Tier,
} from '../../domain/canvas-layout-tier'
import {
  buildVerifySuggestions,
  collectAdoptedVerifyTexts,
  makeSuggestionContext,
  type VerifySuggestion,
} from '../../domain/canvas-verify-suggest'
import {
  contentBox,
  contentCenter,
  fitScale,
  viewportRect,
  zoomPercent,
  type ViewportRect,
} from '../../domain/canvas-viewport'
import {
  OVERVIEW_LINK_PREFIX,
  OVERVIEW_NODE_PREFIX,
  buildOverview,
  isOverviewNodeId,
  overviewColumnKey,
  overviewColumnLayer,
  overviewCountText,
  overviewLinkWidth,
  type OverviewModel,
} from '../../domain/canvas-overview'
import { createAutosaver } from '../../domain/canvas-autosave'
import {
  FOCUS_HOPS_MAX,
  SYNTHETIC_EDGE_PREFIX,
  buildDetailModel,
  collapseChain,
  buildRowOrdinals,
  DETAIL_LAYERS,
  focusChain,
  isLaneId,
  isToggleable,
  nodeSubtitle,
  projectView,
  type DetailLayerKind,
  type DetailModel,
  type ViewMode,
} from '../../domain/canvas-view'
import { canvasTokens, colors } from '../../design/tokens'
import CanvasNodeDrawer from './CanvasNodeDrawer.vue'
import CanvasToolbar from './CanvasToolbar.vue'
import CanvasLegend from './CanvasLegend.vue'
import CanvasMinimap, { type MinimapNode } from './CanvasMinimap.vue'
import CanvasPathBar, { type PathSegment } from './CanvasPathBar.vue'
import CanvasChatPanel from './CanvasChatPanel.vue'
import FactDetailPopover from './FactDetailPopover.vue'
import ManualNodeModal from './ManualNodeModal.vue'
import SnapshotDrawer from './SnapshotDrawer.vue'
import EdgeCreatePopover from './EdgeCreatePopover.vue'
import FunctionQueryModal from './FunctionQueryModal.vue'
import {
  RESEARCH_CARD_NODE,
  ensureResearchCardNode,
} from './g6-card-node'

/** doExpand 图层记账（仅用于四态机/去重；可见性改由 canvas-view 投影） */
interface ExpansionLayer {
  nodes: string[]
  edges: string[]
}

const props = defineProps<{
  caseId: string
  clueId: string
}>()

const emit = defineEmits<{
  (e: 'loaded', payload: { version: number; nodeCount: number; edgeCount: number }): void
}>()

type LoadState = 'loading' | 'ready' | 'error'
const state = ref<LoadState>('loading')
const errorMsg = ref('')
const doc = ref<CanvasDoc | null>(null)
const version = ref(0)
const semanticReady = ref(true)
const graphFailed = ref(false)

const containerEl = ref<HTMLDivElement | null>(null)

interface G6Instance {
  render?: () => Promise<unknown>
  setData?: (d: unknown) => void
  destroy?: () => void
  on?: (event: string, handler: (ev: unknown) => void) => void
  off?: (event: string, handler: (ev: unknown) => void) => void
  fitView?: () => Promise<unknown>
  zoomTo?: (zoom: number) => Promise<unknown>
  getZoom?: () => number
  getSize?: () => [number, number]
  translateBy?: (offset: [number, number]) => Promise<unknown>
  getViewportByCanvas?: (point: [number, number]) => [number, number]
  getCanvasByViewport?: (point: [number, number]) => [number, number]
  focusElement?: (id: string, animation?: boolean) => Promise<unknown>
  getElementPosition?: (id: string) => [number, number]
  setElementState?: (
    state: Record<string, string[]>,
    animation?: boolean,
  ) => Promise<void> | void
}

/** G6 函数式样式入参（datum：data 负载 + states + 原始 style） */
interface G6Datum {
  data?: Record<string, unknown>
  states?: string[]
  style?: Record<string, unknown>
}

interface G6Event {
  id?: unknown
  target?: { id?: unknown }
  originalTarget?: { className?: unknown }
}

let inst: G6Instance | null = null

const message = useMessage()
const dialog = useDialog()

/** 结构变更请求进行中（工具栏/快照按钮 loading） */
const busy = ref(false)

/** 节点类型 → 卡片中文单字（与图例一致） */
const KIND_GLYPH: Record<NodeKind, string> = {
  rule: '规',
  fact: '实',
  object: '体',
  source_row: '行',
  source_file: '档',
  verify_item: '核',
  evidence: '证',
  hypothesis: '假',
  note: '备',
  function_result: '查',
}

/** 五维名 → 色（canvas 不能消费 CSS 变量，取 tokens 常量） */
function jianColor(dimension: string | undefined): string | null {
  switch (dimension) {
    case '资金': return colors.jian.fund
    case '通讯': return colors.jian.comms
    case '行为': return colors.jian.behavior
    case '关系': return colors.jian.relation
    case '时间': return colors.jian.time
    default: return null
  }
}

/** 泳道色带/列头已下线：列名由「全局概览」列胶囊统一承载，
 *  明细视图靠节点 X 方向对齐识别列（col x 间隔 220px），色带背景会让
 *  节点 ↔ 列分界被噪声淹没。*/

// 降级列表（RC-207）恒为全量：视图折叠不影响降级信息面
const groupedNodes = computed(() => {
  const nodes = doc.value?.nodes ?? []
  return KIND_ORDER.map((kind) => ({
    kind,
    label: KIND_LABELS[kind],
    nodes: nodes.filter((n) => n.kind === kind),
  })).filter((g) => g.nodes.length > 0)
})

const edges = computed(() => doc.value?.edges ?? [])

const isEmpty = computed(
  () => state.value === 'ready' && (doc.value?.nodes.length ?? 0) === 0,
)

/** 徽标项（顺序即 badge-N 索引；action 供事件委托识别） */
interface BadgeSpec {
  text: string
  placement: string
  backgroundFill: string
  fill: string
  fontSize: number
  padding: [number, number]
  backgroundRadius: string | number
  action?: 'toggle' | 'rows' | 'objects'
  backgroundWidth?: number
  backgroundHeight?: number
}

function cardBadges(data: Record<string, unknown>): BadgeSpec[] {
  const out: BadgeSpec[] = []
  if (data.toggleable) {
    out.push({
      text: data.expanded ? '−' : '+',
      placement: 'right-top',
      backgroundFill: canvasTokens.toggleBg,
      fill: canvasTokens.toggleInk,
      fontSize: 13,
      padding: [1, 6],
      backgroundRadius: '50%',
      action: 'toggle',
    })
  }
  const rowCount = Number(data.rowCount ?? 0)
  if (rowCount > 0) {
    out.push({
      text: `${rowCount} 行`,
      placement: 'right-bottom',
      backgroundFill: canvasTokens.countBg,
      fill: canvasTokens.countText,
      fontSize: 10,
      padding: [1, 7],
      backgroundRadius: 9,
      action: 'rows',
    })
  }
  const objectCount = Number(data.objectCount ?? 0)
  if (objectCount > 0) {
    out.push({
      text: `${objectCount} 实体`,
      placement: 'left-bottom',
      backgroundFill: canvasTokens.countBg,
      fill: canvasTokens.countText,
      fontSize: 10,
      padding: [1, 7],
      backgroundRadius: 9,
      action: 'objects',
    })
  }
  if (data.stale) {
    out.push({
      text: '失效',
      placement: 'left-top',
      backgroundFill: colors.error.bg,
      fill: colors.error.text,
      fontSize: 9,
      padding: [1, 6],
      backgroundRadius: 8,
    })
  }
  return out
}

/** 泳道伪节点已下线：以前靠 buildLaneNodes 注入 5 色带 + 5 列头，
 *  现在「全局概览」列胶囊承担列名；明细视图不再有 lane 节点。
 *  函数保留以避免改动 toG6Data 的扁平结构。
 */

function toG6Data(d: CanvasDoc): unknown {
  const model = detailModel.value
  const factDim = factDimension.value
  return {
    nodes: d.nodes.map((n) => {
      const group = model.groupByFact.get(n.id)
      const dimension =
        n.kind === 'rule'
          ? dimensionMap[n.ref || n.id]
          : n.kind === 'fact'
            ? factDim.get(n.id)
            : undefined
      return {
        id: n.id,
        // RC-201：坐标是画布表达层数据（preset 渲染，不走 dagre 重排）
        style: { x: n.x, y: n.y },
        data: {
          label: n.label,
          kind: n.kind,
          glyph: KIND_GLYPH[n.kind],
          chip: canvasTokens.kind[n.kind].chip,
          chipInk: canvasTokens.kind[n.kind].ink,
          subtitle: subtitleOf(n),
          stale: n.stale === true,
          pinned: n.pinned === true,
          manual: n.system !== true,
          // M4 RC-105：未采纳手册建议（虚线态）
          suggestion: isSuggestionNode(n),
          // 简洁视图才给 +/−（完整视图所有节点恒显）
          toggleable: viewMode.value === 'compact' && isToggleable(n),
          expanded: expandedRoots.value.has(n.id),
          rowCount: group?.rows.length ?? 0,
          objectCount: group?.objects.length ?? 0,
          dimColor: jianColor(dimension),
          // P3：研判视角——证据强度（渲染层只读 tier 用于半透；
          // 坐标重排在 laidOutDoc 完成，不在 data 上回写）
          tier: tierOf(n),
        },
      }
    }),
    edges: [
      ...d.edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        data: {
          label: e.rel,
          system: e.system === true,
          note: e.note ?? '',
        },
      })),
      // P1：传递节点折叠后的合成边（点它可展开被吃掉的那一段）
      ...(collapsedChain.value?.syntheticEdges ?? []).map((s) => ({
        id: s.id,
        source: s.source,
        target: s.target,
        data: {
          label: `经 ${s.via.length} 个中间节点`,
          system: true,
          synthetic: true,
          via: s.via,
        },
      })),
    ],
  }
}

function cloneDoc(d: CanvasDoc): CanvasDoc {
  return JSON.parse(JSON.stringify(d)) as CanvasDoc
}

// ======================================================================
// RC-103/104：节点抽屉 + 展开编排（展开态仅内存，不持久化）
// ======================================================================
const drawerShow = ref(false)
const drawerNodeId = ref<string | null>(null)

/** 节点展开四态（缺省视为 collapsed） */
const uiStates = reactive<Record<string, ExpandUiState>>({})
/** 每个展开根累计引入的图层（不同方向多次展开取并集；仅四态机记账） */
const layersMap = reactive<Record<string, ExpansionLayer>>({})
/** 最近一次展开方向（失败重试复用） */
const lastDirection = reactive<Record<string, ExpandDirection>>({})
/** 最近一次展开的 notices/truncated（抽屉反馈区） */
const nodeNotices = reactive<Record<string, ExpandNotice[]>>({})
const nodeTruncated = reactive<Record<string, boolean>>({})
/** expand 回传抽屉负载（key=节点 id；不进本地 doc 副本） */
const detailsMap = reactive<Record<string, NodeDetail>>({})

/** RC-102 规则审计（按 rule_id 缓存；404=missing 态） */
const auditView = ref<'business' | 'audit'>('business')
const auditsMap = reactive<Record<string, RuleAudit>>({})
const auditLoading = reactive<Record<string, boolean>>({})
const auditMissing = reactive<Record<string, boolean>>({})

// ======================================================================
// 视图投影（UX P0）：简洁/完整 + 明细节点展开集 + 三层强制开关
// ======================================================================
function viewStorageKey(): string {
  return `canvas-view:${props.caseId}:${props.clueId}`
}

function loadViewPref(): {
  mode: ViewMode
  layers: Record<DetailLayerKind, boolean>
  legendCollapsed: boolean
} {
  const fallback = {
    mode: 'compact' as ViewMode,
    layers: { object: false, source_row: false, source_file: false },
    legendCollapsed: false,
  }
  try {
    const raw = localStorage.getItem(viewStorageKey())
    if (!raw) return fallback
    const parsed = JSON.parse(raw) as {
      mode?: ViewMode
      layers?: Record<string, boolean>
      legendCollapsed?: boolean
    }
    return {
      mode: parsed.mode === 'full' ? 'full' : 'compact',
      layers: {
        object: parsed.layers?.object === true,
        source_row: parsed.layers?.source_row === true,
        source_file: parsed.layers?.source_file === true,
      },
      legendCollapsed: parsed.legendCollapsed === true,
    }
  } catch {
    return fallback
  }
}

const initialPref = loadViewPref()
const viewMode = ref<ViewMode>(initialPref.mode)
/** 研判视角（横轴不变，纵向布局策略）：
 *  - process：流程视角，按 RANK_X 分列 + 数据血缘 y 序
 *  - tier：证据强度视角，三横带：已锁死 / 待核实 / 推测 */
type Perspective = 'process' | 'tier'
const perspective = ref<Perspective>('process')
/** 图例收起态（避免遮挡右侧数据源列） */
const legendCollapsed = ref<boolean>(initialPref.legendCollapsed)

function onLegendCollapse(collapsed: boolean): void {
  legendCollapsed.value = collapsed
  persistViewPref()
}
const layerForced = reactive<Record<DetailLayerKind, boolean>>({
  object: initialPref.layers.object,
  source_row: initialPref.layers.source_row,
  source_file: initialPref.layers.source_file,
})
/** 简洁视图下已展开的根（fact / object / source_row） */
const expandedRoots = ref<Set<string>>(new Set())
/** 单击固化的焦点节点（null=仅 hover 焦点） */
const selectedNodeId = ref<string | null>(null)
/** rule 节点 id → 五维名（ruleAudit 懒取，失败静默缺省） */
const dimensionMap = reactive<Record<string, string>>({})

function persistViewPref(): void {
  try {
    localStorage.setItem(
      viewStorageKey(),
      JSON.stringify({
        mode: viewMode.value,
        perspective: perspective.value,
        layers: { ...layerForced },
        legendCollapsed: legendCollapsed.value,
      }),
    )
  } catch {
    /* localStorage 不可用时静默（偏好非关键路径） */
  }
}

const detailModel = computed<DetailModel>(() =>
  doc.value ? buildDetailModel(doc.value) : EMPTY_MODEL,
)
const ordinals = computed(() =>
  doc.value ? buildRowOrdinals(doc.value) : EMPTY_ORDINALS,
)

/** fact → 其上游 rule 的五维（沿「命中」系统边） */
const factDimension = computed<Map<string, string>>(() => {
  const out = new Map<string, string>()
  if (!doc.value) return out
  // dimensionMap 以规则 ref（如 R6）为键；边的 source 用全 id（rule:R6）
  const dimByRuleId = new Map<string, string>()
  for (const n of doc.value.nodes) {
    if (n.kind !== 'rule') continue
    const dim = dimensionMap[n.ref || n.id]
    if (dim) dimByRuleId.set(n.id, dim)
  }
  for (const e of doc.value.edges) {
    if (e.rel !== '命中') continue
    const dim = dimByRuleId.get(e.source)
    if (dim) out.set(e.target, dim)
  }
  return out
})

const projection = computed(() =>
  doc.value
    ? projectView(doc.value, detailModel.value, {
        mode: viewMode.value,
        expandedRoots: expandedRoots.value,
        layers: { ...layerForced },
      })
    : null,
)

/** 简洁/完整投影后供 G6 渲染的文档子集（降级列表另用 doc 全量） */
const renderDoc = computed<CanvasDoc | null>(() => {
  if (!doc.value || !projection.value) return null
  const p = projection.value
  return {
    ...doc.value,
    nodes: doc.value.nodes.filter((n) => p.visibleNodeIds.has(n.id)),
    edges: doc.value.edges.filter((e) => p.visibleEdgeIds.has(e.id)),
  }
})

/**
 * 按当前研判视角重排坐标（仅节点 y；x 与 process 视角同）。
 * 流程视角直接返回 renderDoc；证据强度视角经 layoutByTier 重排。
 */
const laidOutDoc = computed<CanvasDoc | null>(() => {
  const base = renderDoc.value
  if (!base) return null
  if (perspective.value === 'tier') return layoutByTier(base)
  return base
})

/** 证据强度视角下的各带节点数（状态栏摘要） */
const tierCounts = computed<Record<Tier, number>>(() =>
  doc.value ? tierCountsOf(doc.value) : { 1: 0, 2: 0, 3: 0 },
)

// ----------------------------------------------------------------------
// P1 焦点轨迹：层层挖掘的来路显式化（路径条可点回退/跳转）
// ----------------------------------------------------------------------
/** 固化焦点经过的节点 id（栈式：重复命中同一节点则截断其后） */
const focusTrail = ref<string[]>([])

const trailItems = computed<PathSegment[]>(() => {
  const out: PathSegment[] = []
  for (const id of focusTrail.value) {
    const n = findNode(id)
    if (!n) continue
    out.push({
      id: n.id,
      label: n.label,
      glyph: KIND_GLYPH[n.kind],
      chip: canvasTokens.kind[n.kind].chip,
      ink: canvasTokens.kind[n.kind].ink,
    })
  }
  return out
})

function pushTrail(id: string): void {
  const idx = focusTrail.value.indexOf(id)
  focusTrail.value = idx >= 0
    ? focusTrail.value.slice(0, idx + 1)
    : [...focusTrail.value, id]
}

/** 点路径条某一段：截断其后轨迹并回到该节点 */
function onTrailJump(id: string): void {
  const n = findNode(id)
  if (!n) return
  const idx = focusTrail.value.indexOf(id)
  if (idx >= 0) focusTrail.value = focusTrail.value.slice(0, idx + 1)
  else pushTrail(id)
  unfoldedNodeIds.value = new Set()
  selectedNodeId.value = id
  applyFocus(id)
  try {
    void inst?.focusElement?.(id, true)
  } catch {
    /* 视口 API 不可用：不影响跳转 */
  }
}

function onTrailReset(): void {
  clearFocus()
}

/** 清掉固化焦点与来路（空白点击 / Esc / 进入全局概览共用） */
function clearFocus(): void {
  focusTrail.value = []
  unfoldedNodeIds.value = new Set()
  selectedNodeId.value = null
  applyFocus(null)
}

/** 压缩链路开关（P1：把度=2 的传递节点压成一条合成边） */
const passThroughCollapsed = ref(true)
/** 用户点开过的合成段：段内中间节点强制保留 */
const unfoldedNodeIds = ref<Set<string>>(new Set())

/**
 * 当前固化焦点下的折叠结果。
 * 只在「有固化焦点」时折叠——无焦点时整图就是全局视图，不该偷偷删节点。
 */
const collapsedChain = computed(() => {
  if (!passThroughCollapsed.value) return null
  const id = selectedNodeId.value
  // 口径必须按「当前投影」而非 doc 全量：否则合成边端点可能是被折叠/隐藏的节点，
  // G6 会因边引用不存在的节点而报错
  const d = renderDoc.value ?? doc.value
  if (!id || !d) return null
  const chain = focusChain(d, id, focusHops.value)
  if (!chain) return null
  return collapseChain(d, chain, { keep: unfoldedNodeIds.value })
})

/**
 * 交给 G6 的文档：投影 + 传递节点折叠 + 研判视角重排的结果。
 *
 * 顺序很关键——必须先按视角重排坐标，再做折叠（折叠按 renderDoc 算链路）；
 * 折叠用 renderDoc 不是 laidOutDoc，理由与 collapsedChain 注释一致：合成边端点
 * 必须在「实际参与投影渲染」的节点集合里。
 */
const graphDoc = computed<CanvasDoc | null>(() => {
  const base = laidOutDoc.value ?? renderDoc.value ?? doc.value
  if (!base) return null
  const c = collapsedChain.value
  if (!c || c.collapsedNodeIds.size === 0) return base
  const nodes = base.nodes.filter((n) => !c.collapsedNodeIds.has(n.id))
  const ids = new Set(nodes.map((n) => n.id))
  const edges = base.edges.filter((e) => ids.has(e.source) && ids.has(e.target))
  return { ...base, nodes, edges }
})

// ----------------------------------------------------------------------
// P2 全局层聚合视图：整图压成「列胶囊 + 主干边」
//
// 与投影/折叠不互相侵入：聚合直接吃 doc 全量（「全局」本就该是最终展开后的
// 全貌，不该再受简洁视图折叠影响），只在渲染层替换掉交给 G6 的数据。
// ----------------------------------------------------------------------
const overviewMode = ref(false)

const overviewModel = computed<OverviewModel | null>(() =>
  doc.value ? buildOverview(doc.value) : null,
)

/** 列胶囊沿用 research-card 尺寸（186×50）：卡内布局是写死的，换尺寸会错位 */
function toG6OverviewData(m: OverviewModel): unknown {
  return {
    nodes: m.columns.map((c) => {
      const primary = c.kinds[0]?.kind ?? 'object'
      return {
        id: `${OVERVIEW_NODE_PREFIX}${c.key}`,
        style: { x: c.x, y: 0 },
        data: {
          overview: true,
          columnKey: c.key,
          label: c.label,
          // chip 放计数：聚合视图里「这一层有多少」比类型字形更值得占圆底
          glyph: overviewCountText(c.count),
          chip: canvasTokens.kind[primary].chip,
          chipInk: canvasTokens.kind[primary].ink,
          subtitle:
            `${c.kinds.length} 类` + (c.inner > 0 ? ` · 列内 ${c.inner} 条` : ''),
        },
      }
    }),
    edges: m.links.map((l) => ({
      id: `${OVERVIEW_LINK_PREFIX}${l.source}--${l.target}`,
      source: `${OVERVIEW_NODE_PREFIX}${l.source}`,
      target: `${OVERVIEW_NODE_PREFIX}${l.target}`,
      data: {
        label: `${l.count} 条`,
        overview: true,
        weight: l.weight,
        system: true,
      },
    })),
  }
}

function currentG6Data(): unknown {
  if (overviewMode.value && overviewModel.value) {
    return toG6OverviewData(overviewModel.value)
  }
  const d = graphDoc.value ?? doc.value
  if (!d) return { nodes: [], edges: [] }
  return toG6Data(d)
}

function setOverviewMode(on: boolean): void {
  if (overviewMode.value === on) return
  overviewMode.value = on
  if (on) clearFocus()
}

/**
 * 点列胶囊 = 回到明细视图并落到该层（明细列顺手打开对应层开关，
 * 否则退出聚合后那层还是折叠的，等于没下钻）。
 */
function onOverviewColumnPick(key: string | null): void {
  overviewMode.value = false
  const layer = key ? overviewColumnLayer(key) : undefined
  // 仅 object/source_row/source_file 在 layerForced 上有意义（其它列在简洁视图恒可见）
  if (
    layer === 'object' ||
    layer === 'source_row' ||
    layer === 'source_file'
  ) {
    if (layerForced[layer] !== true) {
      layerForced[layer] = true
      pendingRefit = true
    }
  }
}

function subtitleOf(n: CanvasNode): string {
  const dims = new Map(Object.entries(dimensionMap))
  return nodeSubtitle(n, {
    model: detailModel.value,
    ordinals: ordinals.value,
    dimensions: dims,
  })
}

async function ensureRuleDimension(ruleId: string): Promise<void> {
  if (dimensionMap[ruleId] || auditLoading[ruleId]) return
  auditLoading[ruleId] = true
  try {
    const audit = await canvasApi.ruleAudit(props.caseId, ruleId)
    if (audit.dimension) dimensionMap[ruleId] = audit.dimension
  } catch {
    /* 维度缺省不阻断画布 */
  } finally {
    auditLoading[ruleId] = false
  }
}

const EMPTY_MODEL: DetailModel = {
  groups: [],
  groupByFact: new Map(),
  producers: new Map(),
  adjacency: new Map(),
}
const EMPTY_ORDINALS: ReadonlyMap<string, { source: string; ordinal: number }> =
  new Map()

const drawerNode = computed<CanvasNode | null>(() => {
  const id = drawerNodeId.value
  if (!id || !doc.value) return null
  return doc.value.nodes.find((n) => n.id === id) ?? null
})

/** 「下一步可核查」建议（按当前抽屉节点 + 已采纳去重）。仅 fact/object/source_row 有值。 */
const verifySuggestions = computed<VerifySuggestion[]>(() => {
  if (!drawerNode.value || !doc.value) return []
  return buildVerifySuggestions(
    drawerNode.value,
    makeSuggestionContext(doc.value, collectAdoptedVerifyTexts(doc.value)),
  )
})

const drawerDetail = computed<NodeDetail | null>(() =>
  drawerNodeId.value ? detailsMap[drawerNodeId.value] ?? null : null,
)

const drawerState = computed<ExpandUiState>(() =>
  drawerNodeId.value
    ? uiStates[drawerNodeId.value] ?? 'collapsed'
    : 'collapsed',
)

const drawerHasExpanded = computed(() => {
  const id = drawerNodeId.value
  return !!id && (expandedRoots.value.has(id) || uiStates[id] === 'expanded')
})

function findNode(id: string): CanvasNode | undefined {
  if (isLaneId(id)) return undefined
  return doc.value?.nodes.find((n) => n.id === id)
}

/** 单击延迟 250ms：让双击优先被识别为展开/折叠，避免误开抽屉 */
const CLICK_DELAY_MS = 250
let clickTimer: ReturnType<typeof setTimeout> | null = null

function clearClickTimer(): void {
  if (clickTimer !== null) {
    clearTimeout(clickTimer)
    clickTimer = null
  }
}

/** 徽标委托：根据子图形 className badge-N 反查 action */
function badgeActionOf(
  ev: { originalTarget?: { className?: unknown }; target?: unknown },
  data: Record<string, unknown>,
): BadgeSpec['action'] {
  const raw = (ev.originalTarget as { className?: unknown } | undefined)?.className
  const name = typeof raw === 'string' ? raw : ''
  const m = /^badge-(\d+)$/.exec(name)
  if (!m) return undefined
  const badges = cardBadges(data)
  return badges[Number(m[1])]?.action
}

/** node:click（延迟开抽屉；徽标点击立即处理） */
function onNodeClick(ev: unknown, rawId: unknown): void {
  const id = String(rawId ?? '')
  if (isLaneId(id)) return
  // P2：聚合视图里的列胶囊不是 doc 节点，点它就是「下钻到这一层」
  if (isOverviewNodeId(id)) {
    clearClickTimer()
    onOverviewColumnPick(overviewColumnKey(id))
    return
  }
  const node = findNode(id)
  if (!node) return
  const event = ev as { originalTarget?: { className?: unknown } }
  const g6Node = toG6NodeData(node)
  const action = badgeActionOf(event, g6Node)
  if (action === 'toggle') {
    clearClickTimer()
    void toggleNode(node)
    return
  }
  if (action === 'rows' || action === 'objects') {
    clearClickTimer()
    const oe = (event as { originalEvent?: MouseEvent }).originalEvent
    openFactPopover(node.id, oe?.clientX, oe?.clientY)
    return
  }
  // 连线模式下节点点击归 create-edge behavior，不开抽屉
  if (connectMode.value) return
  clearClickTimer()
    clickTimer = setTimeout(() => {
    clickTimer = null
    // 换焦点：上一轮手动展开的折叠段不继承
    unfoldedNodeIds.value = new Set()
    selectedNodeId.value = node.id
    pushTrail(node.id)
    applyFocus(node.id)
    openNode(node)
  }, CLICK_DELAY_MS)
}

/** node:dblclick：展开/折叠（清掉挂起的单击开抽屉） */
function onNodeDblClick(rawId: unknown): void {
  const id = String(rawId ?? '')
  if (isLaneId(id)) return
  const node = findNode(id)
  if (!node) return
  clearClickTimer()
  void toggleNode(node)
}

/** 卡片 data 重建（与 toG6Data 节点段同口径，供徽标反查） */
function toG6NodeData(n: CanvasNode): Record<string, unknown> {
  const group = detailModel.value.groupByFact.get(n.id)
  return {
    label: n.label,
    kind: n.kind,
    stale: n.stale === true,
    pinned: n.pinned === true,
    manual: n.system !== true,
    suggestion: isSuggestionNode(n),
    toggleable: viewMode.value === 'compact' && isToggleable(n),
    expanded: expandedRoots.value.has(n.id),
    rowCount: group?.rows.length ?? 0,
    objectCount: group?.objects.length ?? 0,
  }
}

/** +/− 切换：已展开→折叠投影；未展开→必要时懒加载并纳入展开集 */
async function toggleNode(node: CanvasNode): Promise<void> {
  if (viewMode.value !== 'compact') return
  if (expandedRoots.value.has(node.id)) {
    collapseRoot(node.id)
    return
  }
  const needsFetch = needsExpandFetch(node)
  if (needsFetch) {
    await doExpand(node.id, defaultDirection(node.kind))
  }
  // doExpand 已纳入展开集；失败或叶子节点也给入口态（折叠空组无副作用）
  addExpandedRoot(node.id)
}

function needsExpandFetch(node: CanvasNode): boolean {
  // 四态机：从未成功展开过则懒加载（种子文档可能已含明细，端点幂等
  // 返回空 added；已展开后重复双击只做前端投影切换，不再请求）
  return uiStates[node.id] !== 'expanded'
}

function addExpandedRoot(id: string): void {
  if (!expandedRoots.value.has(id)) {
    expandedRoots.value = new Set([...expandedRoots.value, id])
  }
}

function collapseRoot(id: string): void {
  const next = new Set(expandedRoots.value)
  next.delete(id)
  expandedRoots.value = next
  uiStates[id] = 'collapsed'
}

// ----------------------------------------------------------------------
// focus chain：hover 临时焦点；单击固化；空白/Esc 清除
// ----------------------------------------------------------------------
let hoverNodeId: string | null = null

function onNodePointerOver(rawId: unknown): void {
  const id = String(rawId ?? '')
  if (overviewMode.value || isLaneId(id) || !findNode(id)) return
  hoverNodeId = id
  if (!selectedNodeId.value) applyFocus(id)
}

function onNodePointerLeave(): void {
  hoverNodeId = null
  if (!selectedNodeId.value) applyFocus(null)
}

function onCanvasClick(): void {
  clearClickTimer()
  clearFocus()
}

/** 点合成边：把这一段被吃掉的中间节点放回来 */
function onEdgeClick(edgeId: string): void {
  if (!edgeId.startsWith(SYNTHETIC_EDGE_PREFIX)) return
  const seg = collapsedChain.value?.syntheticEdges.find((s) => s.id === edgeId)
  if (!seg) return
  const next = new Set(unfoldedNodeIds.value)
  for (const id of seg.via) next.add(id)
  unfoldedNodeIds.value = next
}



/**
 * 固化焦点的跳数游标（hover 恒 1 跳：鼠标移动时整图闪动更难受）。
 * 五列分层下一条链横向近 1200px，固定一跳既看不全、又因全画边而糊。
 */
const focusHops = ref(2)

function onToggleCollapse(): void {
  passThroughCollapsed.value = !passThroughCollapsed.value
  unfoldedNodeIds.value = new Set()
}

function setFocusHops(next: number): void {
  const clamped = Math.max(1, Math.min(FOCUS_HOPS_MAX, next))
  if (clamped === focusHops.value) return
  focusHops.value = clamped
  // 跳数变化只影响固化焦点；没有固化焦点时无需重绘状态
  if (selectedNodeId.value) applyFocus(selectedNodeId.value)
}

/** 跳数 → 状态名（与 focus 叠加，越远越细越淡） */
function hopState(depth: number): string[] {
  const hop = depth <= 1 ? 'hop1' : depth === 2 ? 'hop2' : 'hop3'
  return ['focus', hop]
}

/** 批量 state：链上按跳数分级高亮；链外边在固化焦点下直接隐藏（lane 不参与） */
function applyFocus(nodeId: string | null): void {
  const g = inst
  // 聚合视图里没有真实节点 id，下发 state 只会让 G6 报未知元素
  if (overviewMode.value) return
  // 与 collapsedChain 同口径：按当前投影计算（简洁视图会隐藏明细节点）
  const d = renderDoc.value ?? doc.value
  if (!g?.setElementState || !d) return
  // hover 临时焦点 1 跳；单击固化后按 focusHops 展开
  const solid = nodeId !== null && nodeId === selectedNodeId.value
  const chain = nodeId ? focusChain(d, nodeId, solid ? focusHops.value : 1) : null
  // 只对当前投影（简洁视图会隐藏明细节点/边）已渲染的元素下发 state，
  // 否则 G6 对未知 id 抛 Unknown element type，且可能中断整批状态应用
  const visibleNodes = projection.value?.visibleNodeIds
  const visibleEdges = projection.value?.visibleEdgeIds
  if (!visibleNodes || !visibleEdges) return
  const state: Record<string, string[]> = {}
  for (const id of visibleNodes) {
    if (isLaneId(id)) continue
    if (!chain) {
      state[id] = []
    } else if (chain.nodeIds.has(id)) {
      state[id] = id === nodeId
        ? ['selected', 'focus']
        : hopState(chain.depthByNode.get(id) ?? 1)
    } else {
      state[id] = ['dim']
    }
  }
  for (const id of visibleEdges) {
    if (!chain) state[id] = []
    else if (chain.edgeIds.has(id)) {
      state[id] = hopState(chain.depthByEdge.get(id) ?? 1)
    } else {
      // 噪声主体是边：固化焦点下链外边不画（hover 态只压到近乎无形，保结构感）
      state[id] = solid ? ['hidden'] : ['dim']
    }
  }
  void g.setElementState(state, false)
}

// ----------------------------------------------------------------------
// 事实明细预览弹层（计数胶囊入口；字段明文只在抽屉遮蔽 DTO 内）
// ----------------------------------------------------------------------
const factPopoverNodeId = ref<string | null>(null)
const factPopoverPos = ref<{ x: number; y: number }>({ x: 0, y: 0 })

const factPopoverNode = computed<CanvasNode | null>(() =>
  factPopoverNodeId.value ? findNode(factPopoverNodeId.value) ?? null : null)

function openFactPopover(
  factId: string,
  clientX?: number,
  clientY?: number,
): void {
  if (!detailModel.value.groupByFact.has(factId)) return
  factPopoverNodeId.value = factId
  const rect = containerEl.value?.getBoundingClientRect()
  if (rect && clientX !== undefined && clientY !== undefined) {
    factPopoverPos.value = {
      x: Math.min(Math.max(clientX - rect.left + 14, 8), rect.width - 288),
      y: Math.min(Math.max(clientY - rect.top - 10, 8), rect.height - 120),
    }
  } else {
    factPopoverPos.value = { x: 240, y: 120 }
  }
}

function closeFactPopover(): void {
  factPopoverNodeId.value = null
}

function onPopoverOpenNode(id: string): void {
  const node = findNode(id)
  if (!node) return
  closeFactPopover()
  openNode(node)
}

// ----------------------------------------------------------------------
// 视图模式 / 三层开关（偏好持久化，按案件+线索隔离）
// ----------------------------------------------------------------------
/** 下一次重推数据需要重排视口（视图切换/快照回滚：节点集合整体变化） */
let pendingRefit = false

function onViewModeChange(mode: ViewMode): void {
  viewMode.value = mode
  persistViewPref()
  pendingRefit = true
}

function onLayerToggle(layer: DetailLayerKind): void {
  layerForced[layer] = !layerForced[layer]
  persistViewPref()
}

function onExpandAllDetails(): void {
  if (!doc.value) return
  const next = new Set(expandedRoots.value)
  for (const n of doc.value.nodes) {
    if (n.kind === 'fact' || n.kind === 'object' || n.kind === 'source_row') {
      next.add(n.id)
    }
  }
  expandedRoots.value = next
}

function onCollapseAllDetails(): void {
  expandedRoots.value = new Set()
  for (const id of Object.keys(uiStates)) uiStates[id] = 'collapsed'
}

function openNode(node: CanvasNode): void {
  drawerNodeId.value = node.id
  auditView.value = 'business'
  drawerShow.value = true
  // 选中节点可能在可视区外：打开抽屉时把它居中
  try {
    void inst?.focusElement?.(node.id, true)
  } catch {
    // 视口 API 不可用：不影响抽屉内容
  }
  // 数据行/文件：点击即懒加载字段与所属文件（其「展开」就是抽屉内容本身）
  if (node.kind === 'source_row' || node.kind === 'source_file') {
    if (!uiStates[node.id] || uiStates[node.id] === 'collapsed') {
      void doExpand(node.id, 'source')
    }
  }
  // M4 RC-204：结果节点懒装载「查询自」源节点与输入表快照
  if (node.kind === 'function_result') {
    if (!uiStates[node.id] || uiStates[node.id] === 'collapsed') {
      void doExpand(node.id, 'source')
    }
  }
}

async function doExpand(
  nodeId: string,
  direction: ExpandDirection,
  isRetry = false,
): Promise<void> {
  if (uiStates[nodeId] === 'expanding') return
  uiStates[nodeId] = 'expanding'
  lastDirection[nodeId] = direction
  try {
    const env = await canvasApi.expand(
      props.caseId, props.clueId, nodeId, direction, version.value)
    absorbExpand(nodeId, env)
  } catch (e) {
    // 409：他人已更新画布——静默拉最新文档后以新版本自动重试一次
    if (!isRetry && e instanceof ApiError && e.code === 'CONFLICT') {
      try {
        const latest = await canvasApi.get(props.caseId, props.clueId)
        absorbReload(latest)
        const env = await canvasApi.expand(
          props.caseId, props.clueId, nodeId, direction, version.value)
        absorbExpand(nodeId, env)
        return
      } catch (e2) {
        failExpand(nodeId, e2)
        return
      }
    }
    failExpand(nodeId, e)
  }
}

function absorbExpand(nodeId: string, env: ExpandEnvelope): void {
  absorbReload(env)
  Object.assign(detailsMap, env.details ?? {})
  nodeNotices[nodeId] = env.notices ?? []
  nodeTruncated[nodeId] = env.truncated === true
  // 多方向重复展开：图层取并集；空新增（幂等再点/叶子）也进入 expanded
  const prev = layersMap[nodeId] ?? { nodes: [], edges: [] }
  layersMap[nodeId] = {
    nodes: [...new Set([...prev.nodes, ...env.added_nodes])],
    edges: [...new Set([...prev.edges, ...env.added_edges])],
  }
  addExpandedRoot(nodeId)
  uiStates[nodeId] = 'expanded'
}

function absorbReload(env: { doc: CanvasDoc; version: number;
                             semantic_ready?: boolean }): void {
  doc.value = env.doc
  version.value = env.version
  semanticReady.value = env.semantic_ready !== false
}

function failExpand(nodeId: string, e: unknown): void {
  uiStates[nodeId] = 'expand_failed'
  errorMsg.value = presentError(e).title
}

function retryExpand(): void {
  const id = drawerNodeId.value
  if (!id) return
  void doExpand(id, lastDirection[id] ?? defaultDirection(
    drawerNode.value?.kind ?? 'fact'), true)
}

function onDrawerExpand(direction: ExpandDirection): void {
  const id = drawerNodeId.value
  if (id) void doExpand(id, direction)
}

function collapseCurrent(): void {
  const id = drawerNodeId.value
  if (!id) return
  collapseRoot(id)
}

async function onAuditViewChange(v: 'business' | 'audit'): Promise<void> {
  auditView.value = v
  if (v !== 'audit' || !drawerNode.value) return
  const ruleId = drawerNode.value.ref
  if (auditsMap[ruleId] || auditLoading[ruleId]
      || auditMissing[ruleId]) return
  auditLoading[ruleId] = true
  try {
    auditsMap[ruleId] = await canvasApi.ruleAudit(props.caseId, ruleId)
    auditMissing[ruleId] = false
  } catch (e) {
    auditMissing[ruleId] = e instanceof ApiError && e.code === 'NOT_FOUND'
    if (!auditMissing[ruleId]) errorMsg.value = presentError(e).title
  } finally {
    auditLoading[ruleId] = false
  }
}

const drawerAudit = computed<RuleAudit | null>(() =>
  drawerNode.value ? auditsMap[drawerNode.value.ref] ?? null : null)

/** 抽屉节点的人工连线（支持删除；系统边不出现操作入口） */
const drawerManualEdges = computed<CanvasEdge[]>(() => {
  const node = drawerNode.value
  return node && doc.value ? incidentManualEdges(doc.value, node.id) : []
})

// ======================================================================
// RC-205：坐标/钉住防抖自动保存（仅 x/y/pinned 白名单，结构操作另走端点）
// ======================================================================
async function persistDoc(nextDoc: CanvasDoc): Promise<void> {
  const baseVersion = version.value
  try {
    const r = await canvasApi.save(
      props.caseId, props.clueId, nextDoc, baseVersion)
    absorbReload(r.envelope)
    return
  } catch (e) {
    if (!(e instanceof ApiError && e.code === 'CONFLICT')) throw e
  }
  // 409：拉最新画布，把本地坐标/钉住意图 rebase 到服务端版本上再写一次
  const latest = await canvasApi.get(props.caseId, props.clueId)
  const mineById = new Map(nextDoc.nodes.map((n) => [n.id, n]))
  const merged: CanvasDoc = {
    ...latest.doc,
    nodes: latest.doc.nodes.map((n) => {
      const mine = mineById.get(n.id)
      return mine
        ? { ...n, x: mine.x, y: mine.y, pinned: mine.pinned }
        : n
    }),
  }
  const r2 = await canvasApi.save(
    props.caseId, props.clueId, merged, latest.version)
  absorbReload(r2.envelope)
  message.warning('画布刚被他人更新，已按最新版本合并你的位置调整')
}

const autosaver = createAutosaver<CanvasDoc>({
  delay: 500,
  save: (d) => persistDoc(d),
  onError: () => {
    message.error('画布自动保存失败，将持续重试；请检查网络后继续操作')
  },
})

function scheduleCoordSave(): void {
  if (doc.value) autosaver.schedule(cloneDoc(doc.value))
}

/** 结构操作统一前置：先落盘坐标，再逐动作调专用端点；409 基于最新版本重试一次 */
async function mutate(
  action: () => Promise<{ doc: CanvasDoc; version: number;
                        semantic_ready?: boolean }>,
): Promise<void> {
  await autosaver.flush()
  busy.value = true
  try {
    try {
      absorbReload(await action())
    } catch (e) {
      if (!(e instanceof ApiError && e.code === 'CONFLICT')) throw e
      absorbReload(await canvasApi.get(props.caseId, props.clueId))
      absorbReload(await action())
      message.warning('画布刚被他人更新，已基于最新版本完成本次操作')
    }
  } catch (e) {
    message.error(presentError(e).title)
    throw e
  } finally {
    busy.value = false
  }
}

// ======================================================================
// RC-201：钉住 / 拖拽 / 分层重排 / 视口
// ======================================================================
const connectMode = ref(false)

function onDragEnd(ev: unknown): void {
  const e = ev as { id?: unknown; target?: { id?: unknown } } | undefined
  const id = String(e?.target?.id ?? e?.id ?? '')
  const node = findNode(id)
  if (!node || !doc.value || !inst?.getElementPosition) return
  const [x, y] = inst.getElementPosition(id)
  const nx = Math.round(x * 100) / 100
  const ny = Math.round(y * 100) / 100
  if (nx === node.x && ny === node.y && node.pinned) return
  node.x = nx
  node.y = ny
  node.pinned = true // RC-201：手动拖动即钉住
  // 触发数组身份变化，保证 watch 重渲染
  doc.value = { ...doc.value, nodes: [...doc.value.nodes] }
  scheduleCoordSave()
}

function unpinNode(node: CanvasNode): void {
  if (!doc.value || !node.pinned) return
  node.pinned = false
  doc.value = { ...doc.value, nodes: [...doc.value.nodes] }
  scheduleCoordSave()
  message.success('已取消钉住，下次「重新排版」时该节点会归位')
}

function onRelayout(): void {
  if (!doc.value) return
  const pinnedIds = new Set(
    doc.value.nodes.filter((n) => n.pinned).map((n) => n.id))
  doc.value = {
    ...doc.value,
    nodes: layoutNodes(doc.value.nodes, doc.value.edges, pinnedIds),
  }
  scheduleCoordSave()
  message.success('已重新排版，钉住节点保持不动')
}

/** 切换研判视角：process（流程） / tier（证据强度）。
 *  切换时清焦点 + 重排视口；偏好持久化。 */
function onTogglePerspective(): void {
  perspective.value = perspective.value === 'tier' ? 'process' : 'tier'
  persistViewPref()
  // 视角切换 = 整体布局变化，重排视口；附带清焦点（节点跨带位移，路径条不再连贯）
  if (perspective.value === 'tier') clearFocus()
  pendingRefit = true
}

/** 视口尺寸（G6 画布尺寸优先，拿不到回落容器尺寸，再兜默认） */
function viewportSize(): [number, number] {
  const s = inst?.getSize?.()
  if (s && s[0] > 0 && s[1] > 0) return [s[0], s[1]]
  const el = containerEl.value
  // happy-dom/未布局时 clientWidth 为 0（不是 null），必须显式兜底
  const w = el?.clientWidth ?? 0
  const h = el?.clientHeight ?? 0
  return [w > 0 ? w : 1000, h > 0 ? h : 580]
}

/**
 * 视口/缩略图统一的节点集合：明细视图按交付 G6 的文档，
 * 聚合视图按列胶囊（胶囊不进 doc，坐标即列 x）。
 */
const viewportNodes = computed<Array<{ x: number; y: number }>>(() => {
  if (overviewMode.value) {
    return (overviewModel.value?.columns ?? []).map((c) => ({ x: c.x, y: 0 }))
  }
  return (graphDoc.value ?? doc.value)?.nodes ?? []
})

/** 内容包围盒（视口适配与缩略图共用同一口径） */
const viewportBox = computed(() => contentBox(viewportNodes.value))

/** 状态栏计数：按交给 G6 的文档（投影 + 折叠后）+ doc 全量对照 */
const statusCounts = computed(() => {
  const d = graphDoc.value ?? doc.value
  return {
    nodes: overviewMode.value
      ? (overviewModel.value?.columns.length ?? 0)
      : (d?.nodes.length ?? 0),
    edges: overviewMode.value
      ? (overviewModel.value?.links.length ?? 0)
      : (d?.edges.length ?? 0),
    totalNodes: doc.value?.nodes.length ?? 0,
    collapsed: collapsedChain.value?.collapsedNodeIds.size ?? 0,
  }
})

/** 状态栏缩放百分比 */
const zoomPct = ref(100)
/** 内容超出视口（缩放被可读下限兜住，需要拖拽浏览） */
const viewportOverflow = ref(false)

// ----------------------------------------------------------------------
// P2 全局缩略图：放大/拖拽后「我在哪」+ 点击定位
// ----------------------------------------------------------------------
const minimapNodes = computed<MinimapNode[]>(() =>
  ((graphDoc.value ?? doc.value)?.nodes ?? []).map((n) => ({
    id: n.id,
    x: n.x,
    y: n.y,
    fill: canvasTokens.kind[n.kind].chip,
  })),
)

/** 当前可见区域（画布坐标） */
const minimapView = ref<ViewportRect | null>(null)

/** 视口左上角（取不到 getCanvasByViewport 时按画布原点反推） */
function viewportTopLeft(): [number, number] | null {
  const direct = inst?.getCanvasByViewport?.([0, 0])
  if (direct && Number.isFinite(direct[0]) && Number.isFinite(direct[1])) {
    return direct
  }
  const zoom = inst?.getZoom?.() ?? 1
  const origin = inst?.getViewportByCanvas?.([0, 0])
  if (!origin || !Number.isFinite(zoom) || zoom <= 0) return null
  return [-origin[0] / zoom, -origin[1] / zoom]
}

function syncMinimap(): void {
  if (!inst || overviewMode.value) {
    minimapView.value = null
    return
  }
  const [vw, vh] = viewportSize()
  minimapView.value = viewportRect(
    viewportTopLeft(),
    vw,
    vh,
    inst.getZoom?.() ?? 1,
  )
}

function syncZoomPct(): void {
  zoomPct.value = zoomPercent(inst?.getZoom?.() ?? 1)
}

/** 缩放/拖拽/重绘后统一同步（状态栏百分比 + 缩略图视口框） */
function syncViewport(): void {
  syncZoomPct()
  syncMinimap()
}

/** 点缩略图：把该画布点平移到视口中心 */
function onMinimapPan(point: [number, number]): void {
  if (!inst) return
  const [vw, vh] = viewportSize()
  const now = inst.getViewportByCanvas?.(point)
  if (!now) return
  void Promise.resolve(
    inst.translateBy?.([vw / 2 - now[0], vh / 2 - now[1]]),
  ).then(syncViewport)
}

/**
 * 初始/重置视口：只按真实节点 bbox 计算缩放并居中（泳道伪节点不参与）。
 * 先绝对缩放，再用「内容中心的当前视口位置 → 视口中心」做相对平移纠偏，
 * 不依赖 G6 变换公式的具体口径。
 */
async function applyInitialViewport(): Promise<void> {
  if (!inst) return
  // 按实际交付 G6 的节点算（折叠后的链路更短，视口更好看；聚合视图是列胶囊）
  const box = viewportBox.value
  const [vw, vh] = viewportSize()
  const scale = fitScale(box, vw, vh)
  try {
    await inst.zoomTo?.(scale)
    const center = contentCenter(box)
    const now = inst.getViewportByCanvas?.(center)
    if (now) await inst.translateBy?.([vw / 2 - now[0], vh / 2 - now[1]])
  } catch {
    // 视口 API 不可用（mock/降级环境）：不影响成图
  }
  syncViewport()
  viewportOverflow.value =
    !!box &&
    ((box.maxX - box.minX) * scale > vw - 8 ||
      (box.maxY - box.minY) * scale > vh - 8)
}

function onFit(): void {
  void applyInitialViewport()
}
function onZoom(delta: number): void {
  const z = inst?.getZoom?.() ?? 1
  const next = Math.min(2.5, Math.max(0.2, z * delta))
  void Promise.resolve(inst?.zoomTo?.(next)).then(syncViewport)
}

function toggleConnectMode(): void {
  connectMode.value = !connectMode.value
  pendingEdge.value = null
  if (connectMode.value) {
    message.info('连线模式：依次点击两个节点建立人工关系（Esc 退出）')
  }
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key !== 'Escape') return
  if (connectMode.value) {
    connectMode.value = false
    pendingEdge.value = null
  }
  clearClickTimer()
  clearFocus()
}

// ======================================================================
// RC-202：人工节点（假设/备注）增删改
// ======================================================================
const nodeModal = reactive<{
  show: boolean
  mode: 'create' | 'edit'
  kind: ManualNodeKind
}>({ show: false, mode: 'create', kind: 'hypothesis' })

function openCreateNode(kind: ManualNodeKind): void {
  nodeModal.mode = 'create'
  nodeModal.kind = kind
  nodeModal.show = true
}

function openEditNode(): void {
  const node = drawerNode.value
  if (!node || !isManualNode(node)) return
  nodeModal.mode = 'edit'
  nodeModal.kind = node.kind as ManualNodeKind
  nodeModal.show = true
}

/** 新人工节点落点：所属列最末槽位下方 */
function nextSlot(kind: ManualNodeKind): { x: number; y: number } {
  const rankX = RANK_X[kind]
  const maxY = (doc.value?.nodes ?? [])
    .filter((n) => n.x === rankX || n.kind === kind)
    .reduce((m, n) => Math.max(m, n.y), -104)
  return { x: rankX, y: maxY + 104 }
}

async function submitNode(payload: {
  kind: ManualNodeKind
  title: string
  content: string
}): Promise<void> {
  try {
    if (nodeModal.mode === 'create') {
      const { x, y } = nextSlot(payload.kind)
      const nodeProps = payload.kind === 'hypothesis'
        ? { title: payload.title, content: payload.content }
        : { content: payload.content }
      await mutate(() =>
        canvasApi.createNode(props.caseId, props.clueId, {
          kind: payload.kind,
          props: nodeProps,
          x,
          y,
          version: version.value,
        }))
      message.success(payload.kind === 'hypothesis' ? '侦查假设已添加' : '备注已添加')
    } else {
      const node = drawerNode.value
      if (!node) return
      const nodeProps = payload.kind === 'hypothesis'
        ? { title: payload.title, content: payload.content }
        : { content: payload.content }
      await mutate(() =>
        canvasApi.updateNode(props.caseId, props.clueId, node.id, {
          props: nodeProps,
          version: version.value,
        }))
      message.success('节点已保存')
    }
    nodeModal.show = false
  } catch {
    // mutate 已统一提示；保持弹窗打开供修改重试
  }
}

function requestDeleteNode(): void {
  const node = drawerNode.value
  if (!node || !doc.value) return
  const edgeCount = incidentManualEdges(doc.value, node.id).length
  dialog.warning({
    title: '删除该节点？',
    content: edgeCount > 0
      ? `该节点关联 ${edgeCount} 条人工连线，将一并移除；系统溯源连线不受影响。删除后可通过快照回滚恢复。`
      : '删除后可通过快照回滚恢复；系统溯源内容不受影响。',
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => doDeleteNode(node),
  })
}

async function doDeleteNode(node: CanvasNode): Promise<void> {
  try {
    await mutate(() =>
      canvasApi.deleteNode(
        props.caseId, props.clueId, node.id, version.value))
    if (drawerNodeId.value === node.id) drawerShow.value = false
    message.success('节点已删除')
  } catch {
    // mutate 已提示
  }
}

// ======================================================================
// RC-203：人工连线（G6 create-edge 拦截 → 矩阵校验 → 关系选择气泡）
// ======================================================================
const pendingEdge = ref<{
  source: CanvasNode
  target: CanvasNode
  rels: ManualRel[]
} | null>(null)

function rejectReason(src: CanvasNode, tgt: CanvasNode): string {
  if (src.id === tgt.id) return '不能连接节点自身'
  if (tgt.kind === 'note') return '备注节点只能作为连线起点'
  if (tgt.kind === 'source_row' || tgt.kind === 'source_file') {
    return '数据行/数据源节点仅可由系统溯源连线关联'
  }
  if (src.kind === 'note') return '备注只能「补充说明」其他节点'
  return '该两类节点之间不能建立人工关系'
}

/** G6 onCreate 钩子：永远返回 undefined（边由服务端签发后随整文档回流） */
function handleOnCreate(draft: unknown): undefined {
  const d = draft as { source?: unknown; target?: unknown } | undefined
  const source = findNode(String(d?.source ?? ''))
  const target = findNode(String(d?.target ?? ''))
  if (!source || !target || !doc.value) return undefined
  const rels = allowedRels(source.kind, target.kind)
  if (rels.length === 0) {
    message.warning(rejectReason(source, target))
    return undefined
  }
  // 唯一合法关系：直接建立（无备注）；多关系：弹气泡让分析员选
  if (rels.length === 1) {
    if (findEdge(doc.value, source.id, target.id, rels[0])) {
      message.warning('该连线已存在')
      return undefined
    }
    void submitEdge(source, target, rels[0], '')
    return undefined
  }
  pendingEdge.value = { source, target, rels }
  return undefined
}

async function submitEdge(
  source: CanvasNode,
  target: CanvasNode,
  rel: ManualRel,
  note: string,
): Promise<void> {
  pendingEdge.value = null
  if (doc.value && findEdge(doc.value, source.id, target.id, rel)) {
    message.warning('该关系连线已存在')
    return
  }
  try {
    await mutate(() =>
      canvasApi.createEdge(props.caseId, props.clueId, {
        source: source.id,
        target: target.id,
        rel,
        note: note || null,
        version: version.value,
      }))
    message.success(`已建立「${rel}」关系`)
  } catch {
    // mutate 已提示
  }
}

function onPickPendingEdge(p: { rel: ManualRel; note: string }): void {
  const pe = pendingEdge.value
  if (pe) void submitEdge(pe.source, pe.target, p.rel, p.note)
}

function requestDeleteEdge(edge: CanvasEdge): void {
  dialog.warning({
    title: '删除该人工连线？',
    content: `关系「${edge.rel}」将从画布移除，可通过快照回滚恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => doDeleteEdge(edge),
  })
}

async function doDeleteEdge(edge: CanvasEdge): Promise<void> {
  try {
    await mutate(() =>
      canvasApi.deleteEdge(
        props.caseId, props.clueId, edge.id, version.value))
    message.success('连线已删除')
  } catch {
    // mutate 已提示
  }
}

// ======================================================================
// RC-206：快照与回滚
// ======================================================================
const snapshotShow = ref(false)
const snapshots = ref<CanvasSnapshot[]>([])
const snapshotLoading = ref(false)
const snapshotCreating = ref(false)
const rollingBackId = ref<string | null>(null)

async function openSnapshots(): Promise<void> {
  snapshotShow.value = true
  await loadSnapshots()
}

// M5 RC-301：画布问答侧栏
const chatShow = ref(false)
function openChat(): void {
  chatShow.value = true
}
/** RC-301：引用定位触发展开重绘后，render 完成再居中的目标节点 id */
const pendingCiteFocus = ref<string | null>(null)

/**
 * 问答引用角标 → 画布定位（RC-301）。
 * 骨干节点（规则/事实/假设/核查项等）直接居中高亮；
 * 折叠中的明细节点（数据行/实体/文件）先展开其生产者事实（必要时懒加载），
 * 待投影重绘后由 pushGraphData 补一次居中。
 * 不打开节点抽屉：右侧问答面板（380px）会被同侧重抽屉（560px）遮挡。
 */
async function onCiteClick(ref: string): Promise<void> {
  if (!doc.value) return
  const node = doc.value.nodes.find((n) => n.ref === ref)
  if (!node) {
    message.warning('引用对应的节点不在当前画布（可能已删除或尚未展开）')
    return
  }
  const hidden =
    viewMode.value === 'compact'
    && (DETAIL_LAYERS as readonly string[]).includes(node.kind)
    && !projection.value?.visibleNodeIds.has(node.id)
  if (hidden) {
    const revealed = await revealCitedNode(node)
    if (!revealed) {
      message.warning('该引用的明细节点当前无法展开定位')
      return
    }
    // 展开 → renderDoc 重算 → pushGraphData 重绘后再聚焦（节点此刻尚未进 G6）
    pendingCiteFocus.value = node.id
  }
  selectedNodeId.value = node.id
  pushTrail(node.id)
  applyFocus(node.id)
  try {
    await inst?.focusElement?.(node.id, true)
  } catch {
    // 节点尚在重绘：pendingCiteFocus 于 render 完成后补聚焦
  }
}

/**
 * 让折叠的明细节点进入当前投影：
 * 优先懒加载并展开其生产者事实（与卡片 +/− 同一路径）；
 * 无事实生产者的明细（孤立文件/实体）则强制打开对应明细层开关。
 * 返回节点在最新投影中是否可见。
 */
async function revealCitedNode(node: CanvasNode): Promise<boolean> {
  const producers = detailModel.value.producers.get(node.id)
  const factId = producers
    ? [...producers].find((id) => findNode(id)?.kind === 'fact')
    : undefined
  const fact = factId ? findNode(factId) : null
  if (fact) {
    if (needsExpandFetch(fact)) {
      await doExpand(fact.id, defaultDirection(fact.kind))
    }
    addExpandedRoot(fact.id)
  } else if ((DETAIL_LAYERS as readonly string[]).includes(node.kind)) {
    layerForced[node.kind as DetailLayerKind] = true
  } else {
    return false
  }
  await nextTick()
  return !!projection.value?.visibleNodeIds.has(node.id)
}

async function loadSnapshots(): Promise<void> {
  snapshotLoading.value = true
  try {
    const env = await canvasApi.listSnapshots(props.caseId, props.clueId)
    snapshots.value = env.snapshots
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    snapshotLoading.value = false
  }
}

async function createSnapshot(label: string): Promise<void> {
  snapshotCreating.value = true
  try {
    await autosaver.flush()
    const env = await canvasApi.createSnapshot(
      props.caseId, props.clueId, label, version.value)
    absorbReload(env)
    snapshots.value = [env.snapshot, ...snapshots.value]
    message.success('快照已创建（画布版本未变动）')
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    snapshotCreating.value = false
  }
}

async function rollbackTo(snapshotId: string): Promise<void> {
  rollingBackId.value = snapshotId
  try {
    await autosaver.flush()
    const env = await canvasApi.rollbackSnapshot(
      props.caseId, props.clueId, snapshotId, version.value)
    absorbReload(env)
    // 节点集合可能整体变化：清空展开临时态（投影回到默认骨干层）
    Object.keys(layersMap).forEach((k) => delete layersMap[k])
    Object.keys(uiStates).forEach((k) => delete uiStates[k])
    expandedRoots.value = new Set()
    // 节点集合整体变化：回滚后重排视口
    pendingRefit = true
    focusTrail.value = []
    selectedNodeId.value = null
    drawerShow.value = false
    pendingEdge.value = null
    snapshots.value = [env.recovery_snapshot, ...snapshots.value]
    if (env.stale_node_ids.length > 0) {
      message.warning(
        `已回滚；${env.stale_node_ids.length} 个节点引用的核查项/书证已失效，已置灰标记`,
      )
    } else {
      message.success('画布已回滚到所选快照（回滚前状态已自动存为恢复点）')
    }
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    rollingBackId.value = null
  }
}

// ======================================================================
// M4 RC-105：手册建议生成 / 采纳（202→终态→sync）/ 假设转待核实
// 纪律：202 只代表入队；必须 waitForTerminal 拿到终态，sync 非 adopted/created
//       一律保持建议态并可见失败，禁止假成功。
// ======================================================================
/** 结构变更类信封（含 doc/version）的 409 单次重试，口径同 mutate() */
async function editWithRetry<E extends CanvasEnvelope>(
  fn: () => Promise<E>,
): Promise<E> {
  try {
    const env = await fn()
    absorbReload(env)
    return env
  } catch (e) {
    if (!(e instanceof ApiError && e.code === 'CONFLICT')) throw e
    absorbReload(await canvasApi.get(props.caseId, props.clueId))
    message.warning('画布刚被他人更新，已基于最新版本完成本次操作')
    const env = await fn()
    absorbReload(env)
    return env
  }
}

/** 入队类端点（202 信封无 doc）的 409 单次重试：拉最新版本后重发 */
async function enqueueWithRetry<E>(fn: () => Promise<E>): Promise<E> {
  try {
    return await fn()
  } catch (e) {
    if (!(e instanceof ApiError && e.code === 'CONFLICT')) throw e
    absorbReload(await canvasApi.get(props.caseId, props.clueId))
    message.warning('画布刚被他人更新，已基于最新版本重试本次操作')
    return await fn()
  }
}

/** 规则节点「生成手册核实建议」的进行中节点 */
const suggestingFor = ref<string | null>(null)
/** 规则节点最近一次建议生成结果（idle/empty/added） */
const suggestionUi = reactive<Record<string, 'idle' | 'empty' | 'added'>>({})
/** pb 建议节点采纳编排态（失败标红 + 可改写文本重试） */
const adoptUi = reactive<Record<string, AdoptUiState>>({})
/** 假设转待核实任务进行中/失败信息（抽屉确认弹窗持有） */
const toVerifying = ref(false)
const toVerifyError = ref<string | null>(null)

const drawerSuggesting = computed(() =>
  drawerNodeId.value ? suggestingFor.value === drawerNodeId.value : false)
const drawerSuggestionState = computed(
  () => (drawerNodeId.value
    ? suggestionUi[drawerNodeId.value] ?? 'idle'
    : 'idle'),
)
const drawerAdoptState = computed<AdoptUiState>(
  () => (drawerNodeId.value
    ? adoptUi[drawerNodeId.value] ?? { status: 'idle' }
    : { status: 'idle' }),
)

async function onGenSuggestions(): Promise<void> {
  const node = drawerNode.value
  if (!node) return
  await autosaver.flush()
  suggestingFor.value = node.id
  try {
    const env = await editWithRetry(() =>
      canvasApi.generateSuggestions(
        props.caseId, props.clueId, node.id, version.value))
    if (env.added_nodes.length > 0) {
      suggestionUi[node.id] = 'added'
      message.success(
        `已生成 ${env.added_nodes.length} 条手册核实建议（虚线节点），采纳后才进入核查工作台`,
      )
    } else {
      // 幂等去重（画布已有同 playbook/同文本节点）≠ 手册无匹配：
      // 需区分提示，避免把「建议已全部生成」误报成「手册暂无」
      const already = env.skipped.filter(
        (s) => s.reason.startsWith('画布已存在')).length
      if (already > 0) {
        suggestionUi[node.id] = 'added'
        message.info(
          `手册建议已在画布上（${already} 条虚线节点），可直接采纳，无需重复生成`)
      } else {
        suggestionUi[node.id] = 'empty'
        message.info('手册暂无该规则的核实建议，可手工添加假设')
      }
    }
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    suggestingFor.value = null
  }
}

async function adoptSuggestion(
  node: CanvasNode,
  text: string | undefined,
): Promise<void> {
  // 注意：必须整体替换 adoptUi[id]（新对象触发 set）。
  // 持有容器内对象的 raw 引用再原地改属性不会触发响应式更新。
  const setAdopt = (next: AdoptUiState): void => {
    adoptUi[node.id] = next
  }
  setAdopt({ status: 'running' })
  await autosaver.flush()
  try {
    const env = await enqueueWithRetry(() =>
      canvasApi.adoptSuggestion(props.caseId, props.clueId, node.id, {
        text,
        version: version.value,
      }))
    // 202 ≠ 成功：等待任务终态
    if (env.task) {
      const t = await waitForTerminal(env.task.id, {
        intervalMs: 500,
        timeoutMs: 30_000,
      })
      if (t.status !== 'SUCCEEDED') {
        setAdopt({
          status: 'failed',
          message: t.error_message || t.error_code || '任务终态未成功',
        })
        message.error('核查项生成失败，画布保持建议态；可改写文本后重试')
        return
      }
    }
    // 终态成功 / mode=adopted：sync 协调 pb→vi 迁移
    const syncEnv = await editWithRetry(() =>
      canvasApi.syncSuggestions(
        props.caseId, props.clueId,
        [{ node_id: node.id, text: env.effective_text }],
        version.value,
      ))
    const r = syncEnv.results[0]
    if (!r || r.status === 'pending' || r.status === 'missing') {
      // 无假成功：state 未就绪就保持虚节点
      const msg = r?.status === 'pending'
        ? '任务尚未完成或已失败，画布保持建议态'
        : '画布上未找到该建议节点'
      setAdopt({ status: 'failed', message: msg })
      message.warning(msg)
      // doc 引用未变、watcher 不触发：显式重推，画布保持建议虚节点态
      pushGraphData()
      return
    }
    setAdopt({ status: 'idle' })
    if (r.new_node_id && r.new_node_id !== node.id) {
      const nn = findNode(r.new_node_id)
      if (nn) openNode(nn) // 抽屉跟随到已采纳核查项
    }
    message.success('已采纳为核查项，画布与核查工作台已一致')
  } catch (e) {
    setAdopt({ status: 'failed', message: presentError(e).title })
  }
}

/** 抽屉采纳/重试事件（模板内联箭头参数无法被 vue-tsc 推断，具名化） */
function onDrawerAdopt(payload: { node: CanvasNode; text?: string }): void {
  void adoptSuggestion(payload.node, payload.text)
}

async function confirmToVerify(text: string): Promise<void> {
  const node = drawerNode.value
  if (!node) return
  // M4 P3：通用化——fact / object / source_row / hypothesis 都走这条入队路径
  toVerifying.value = true
  toVerifyError.value = null
  await autosaver.flush()
  try {
    const env = await enqueueWithRetry(() =>
      canvasApi.addManualVerify(
        props.caseId, props.clueId, node.id, text, version.value))
    const t = await waitForTerminal(env.task.id, {
      intervalMs: 500,
      timeoutMs: 30_000,
    })
    if (t.status !== 'SUCCEEDED') {
      toVerifyError.value = t.error_message || t.error_code || '任务终态未成功'
      return
    }
    const syncEnv = await editWithRetry(() =>
      canvasApi.syncSuggestions(
        props.caseId, props.clueId,
        [{ node_id: node.id, text }],
        version.value,
      ))
    const r = syncEnv.results[0]
    if (!r || r.status === 'pending' || r.status === 'missing') {
      toVerifyError.value = r?.status === 'pending'
        ? '任务尚未完成或已失败，请稍后重试'
        : '画布上未找到该节点'
      // doc 引用未变、watcher 不触发：显式重推，画布保持原态
      pushGraphData()
      return
    }
    message.success('已生成待核实核查项，可在核查工作台继续办理')
    if (r.new_node_id) {
      const nn = findNode(r.new_node_id)
      if (nn) drawerNodeId.value = nn.id // 抽屉跟随到新核查项（弹窗自动关闭）
    }
  } catch (e) {
    toVerifyError.value = presentError(e).title
  } finally {
    toVerifying.value = false
  }
}

/** RC-204 结果节点「查询自」源节点定位（继续 RC-103 溯源） */
function onFocusNode(nodeId: string): void {
  const n = findNode(nodeId)
  if (n) openNode(n)
}

// ======================================================================
// M4 RC-204：扩展查询（白名单只读 Function → function_result 节点）
// ======================================================================
const fqShow = ref(false)
const fqFunctions = ref<FunctionForm[]>([])
const fqAvailable = ref(true)
const fqBusy = ref(false)
/** 「查询自」来源：抽屉打开的规则节点；工具栏入口可能为 null */
const fqSourceId = ref<string | null>(null)
const fqModalRef = ref<InstanceType<typeof FunctionQueryModal> | null>(null)

const fqSourceLabel = computed(() => {
  const n = fqSourceId.value ? findNode(fqSourceId.value) : undefined
  return n ? n.label : null
})

async function openFunctionQuery(): Promise<void> {
  fqSourceId.value = drawerNode.value?.kind === 'rule'
    ? drawerNode.value.id
    : null
  fqBusy.value = false
  try {
    const cat = await canvasApi.listFunctions(props.caseId, props.clueId)
    fqFunctions.value = cat.functions
    fqAvailable.value = cat.available
  } catch (e) {
    message.error(presentError(e).title)
    fqFunctions.value = []
    fqAvailable.value = false
  }
  fqShow.value = true
}

async function submitFunctionQuery(payload: {
  name: string
  params: Record<string, unknown>
}): Promise<void> {
  await autosaver.flush()
  fqBusy.value = true
  const call = (): ReturnType<typeof canvasApi.functionQuery> =>
    canvasApi.functionQuery(props.caseId, props.clueId, {
      function: payload.name,
      params: payload.params,
      source_node_id: fqSourceId.value,
      version: version.value,
    })
  try {
    let env: Awaited<ReturnType<typeof call>>
    try {
      env = await call()
    } catch (e) {
      if (!(e instanceof ApiError && e.code === 'CONFLICT')) throw e
      absorbReload(await canvasApi.get(props.caseId, props.clueId))
      message.warning('画布刚被他人更新，已基于最新版本完成本次查询')
      env = await call()
    }
    if (env.executed) {
      absorbReload(env)
      fqShow.value = false
      message.success('查询完成，结果已挂到画布；可继续沿结果节点溯源')
      const n = findNode(env.node.id)
      if (n) openNode(n)
    } else {
      // DATASOURCE_UNAVAILABLE / DEGRADED：200 但不落节点，弹窗内告警
      fqModalRef.value?.showSkip(env as FunctionQuerySkippedEnvelope)
    }
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    fqBusy.value = false
  }
}

async function load(): Promise<void> {
  state.value = 'loading'
  errorMsg.value = ''
  try {
    const env = await canvasApi.get(props.caseId, props.clueId)
    doc.value = env.doc
    version.value = env.version
    semanticReady.value = env.semantic_ready !== false
    // 规则五维懒预取（卡片顶部色点/事实继承色；失败静默）
    for (const n of env.doc.nodes) {
      if (n.kind === 'rule') void ensureRuleDimension(n.ref || n.id)
    }
    state.value = 'ready'
    emit('loaded', {
      version: env.version,
      nodeCount: env.doc.nodes.length,
      edgeCount: env.doc.edges.length,
    })
  } catch (e) {
    state.value = 'error'
    errorMsg.value = presentError(e).title
  }
}

function failGraph(): void {
  try {
    inst?.destroy?.()
  } catch {
    // ignore
  }
  inst = null
  graphFailed.value = true
}

async function mountGraph(): Promise<void> {
  if (graphFailed.value || !containerEl.value || !doc.value) return
  let GraphCtor: new (cfg: unknown) => G6Instance
  try {
    const mod = await import('@antv/g6')
    GraphCtor = mod.Graph as unknown as new (cfg: unknown) => G6Instance
  } catch {
    failGraph()
    return
  }
  try {
    ensureResearchCardNode()
    inst = new GraphCtor({
      container: containerEl.value,
      autoResize: true,
      // 不用 autoFit：它按「全部元素 bbox」缩放，会把 10 个泳道伪节点算进去，
      // 节点纵向一多就把 186×50 的卡片压到文字不可读。初始视口改由
      // applyInitialViewport() 按真实节点 bbox 计算（domain/canvas-viewport）
      // RC-201：按数据 x/y preset 渲染（分列口径与种子/后端一致），不挂 dagre
      data: currentG6Data(),
      node: {
        type: RESEARCH_CARD_NODE,
        style: {
          size: [186, 50],
          radius: 8,
          lineWidth: (d: G6Datum) => (d.data?.pinned ? 2.5 : 1.25),
          // M4 RC-105：未采纳手册建议节点虚线描边
          lineDash: (d: G6Datum) => (d.data?.suggestion ? [4, 3] : []),
          stroke: (d: G6Datum) =>
            d.data?.manual ? canvasTokens.strokeManual : canvasTokens.stroke,
          cursor: 'pointer',
          pointerEvents: 'auto',
          fill: canvasTokens.surface,
          // 卡片自定义属性（research-card 消费）
          chipText: (d: G6Datum) => d.data?.glyph ?? '',
          chipFill: (d: G6Datum) => d.data?.chip ?? 'transparent',
          chipInk: (d: G6Datum) => d.data?.chipInk ?? canvasTokens.title,
          dimDotColor: (d: G6Datum) =>
            (d.data?.dimColor as string | undefined) ?? '',
          titleFill: canvasTokens.title,
          subtitleText: (d: G6Datum) => d.data?.subtitle ?? '',
          subtitleFill: canvasTokens.subtitle,
          labelText: (d: G6Datum) => d.data?.label ?? '',
          labelPlacement: 'left',
          labelFill: canvasTokens.title,
          labelFontSize: 12,
          labelFontWeight: 600,
          labelMaxWidth: 148,
          labelWordWrap: true,
          // 证据强度视角下 Tier 3 节点（推测 / 失效）半透明后退；
          // 流程视角下仅 stale 节点半透；两套规则都靠 perspective + tier 派生
          opacity: (d: G6Datum) => {
            if (d.data?.stale) return canvasTokens.staleOpacity
            if (perspective.value === 'tier' && d.data?.tier === 3) {
              return TIER_3_OPACITY
            }
            return 1
          },
          badges: (d: G6Datum) => cardBadges(d.data ?? {}),
        },
        state: {
          // 焦点链成员：细描边（低缩放下也要能看出链路）
          focus: {
            stroke: canvasTokens.strokeSelected,
            lineWidth: 2,
          },
          // 固化选中：粗描边 + 外发光，与 focus 形态可区分
          selected: {
            stroke: canvasTokens.strokeSelected,
            lineWidth: 3,
            halo: true,
            haloStroke: canvasTokens.strokeSelected,
            haloLineWidth: 10,
            haloStrokeOpacity: 0.35,
            shadowColor: canvasTokens.strokeSelected,
            shadowBlur: 12,
          },
          // 跳数分级：越远越细越淡（与 focus 叠加，命中键以后者为准）
          hop1: { lineWidth: 2.2, opacity: 1 },
          hop2: { lineWidth: 1.6, opacity: 0.92 },
          hop3: { lineWidth: 1.2, opacity: 0.82 },
          dim: { opacity: canvasTokens.dimOpacity },
        },
      },
      edge: {
        type: 'cubic',
        style: {
          stroke: (d: G6Datum) => {
            if (d.data?.system === false) return canvasTokens.edgeManual
            // 折叠段用高亮青：它代表「被吃掉的 N 个中间节点」，不是普通系统边
            return d.data?.synthetic === true
              ? canvasTokens.edgeSystemFocus
              : canvasTokens.edgeSystem
          },
          lineWidth: (d: G6Datum) => {
            // P2 全局概览：主干边线宽 ∝ 边数（由 overview 派生 weight）
            if (d.data?.overview === true) {
              const w = typeof d.data?.weight === 'number' ? d.data.weight : 0
              return overviewLinkWidth(w)
            }
            if (d.data?.synthetic === true) return 1.8
            return d.data?.system === false ? 1.6 : 1.4
          },
          lineDash: (d: G6Datum) => {
            // 主干边用淡虚线，避免概览里粗黑一团
            if (d.data?.overview === true) return [6, 5]
            if (d.data?.synthetic === true) return [8, 4]
            return d.data?.system === false ? [6, 4] : []
          },
          endArrow: true,
          // 边标签默认隐藏：仅焦点链（含固化选中）显示；折叠段常显（它是可点入口）；
          // 概览主干边常显（「N 条」是结构信息）
          labelText: (d: G6Datum) => {
            if (d.data?.overview === true) return d.data?.label ?? ''
            if (d.data?.synthetic === true) return d.data?.label ?? ''
            return d.states?.includes('focus') ? d.data?.label ?? '' : ''
          },
          labelFill: canvasTokens.edgeLabelFill,
          labelFontSize: 10,
          labelBackground: true,
          labelBackgroundFill: canvasTokens.edgeLabelBg,
          labelBackgroundRadius: 4,
          labelPadding: [2, 5],
          opacity: (d: G6Datum) => (d.data?.system === false ? 0.95 : 0.85),
        },
        state: {
          focus: {
            stroke: (d: G6Datum) =>
              d.data?.system === false
                ? canvasTokens.edgeManual
                : canvasTokens.edgeSystemFocus,
            lineWidth: 2.4,
            opacity: 1,
          },
          // 跳数分级：第 3 跳起加虚线，越远越细
          hop1: { lineWidth: 2.4, opacity: 1 },
          hop2: { lineWidth: 1.6, opacity: 0.9 },
          hop3: { lineWidth: 1.1, opacity: 0.75, lineDash: [6, 4] },
          // hover 临时焦点：压到近乎无形但保留结构感
          dim: { opacity: 0.06 },
          // 固化焦点：链外边直接不画（聚焦态的视觉噪声主要来自边）
          hidden: { opacity: 0 },
        },
      },
      // M3：drag-element 移动节点（连线模式关闭）；create-edge 点击两节点
      // 建人工边（连线模式开启）——enable 在事件触发时动态求值
      behaviors: [
        'drag-canvas',
        'zoom-canvas',
        { type: 'drag-element', enable: () => !connectMode.value },
        {
          type: 'create-edge',
          trigger: 'click',
          enable: () => connectMode.value,
          style: {
            stroke: canvasTokens.edgeManual,
            lineWidth: 1.8,
            lineDash: [6, 4],
            endArrow: true,
          },
          onCreate: (draft: unknown) => handleOnCreate(draft),
        },
      ],
    })
    // RC-103：节点点击 → 溯源抽屉（G6 v5 事件对象 target.id / id 两兼容）
    inst.on?.('node:click', (ev: unknown) => {
      const e = ev as G6Event
      onNodeClick(ev, e?.target?.id ?? e?.id)
    })
    // UX P0：双击节点 = 展开/折叠；+/− 与计数徽标在 node:click 内委托
    inst.on?.('node:dblclick', (ev: unknown) => {
      const e = ev as G6Event
      onNodeDblClick(e?.target?.id ?? e?.id)
    })
    // focus chain：hover 高亮一跳
    inst.on?.('node:pointerover', (ev: unknown) => {
      const e = ev as G6Event
      onNodePointerOver(e?.target?.id ?? e?.id)
    })
    inst.on?.('node:pointerleave', () => onNodePointerLeave())
    // P1：点合成边 = 展开被折叠的那一段（段内中间节点回到画布）
    inst.on?.('edge:click', (ev: unknown) => {
      const e = ev as G6Event
      onEdgeClick(String(e?.target?.id ?? e?.id ?? ''))
    })
    // 空白点击：清除固化焦点
    inst.on?.('canvas:click', () => onCanvasClick())
    // RC-201：拖拽结束 → 钉住 + 坐标防抖保存
    inst.on?.('node:dragend', (ev: unknown) => onDragEnd(ev))
    // P2：任何视口变换（拖拽 / 缩放）都要同步缩略图与缩放百分比
    inst.on?.('aftertransform', () => syncViewport())
    await inst.render?.()
    await applyInitialViewport()
    syncViewport()
    applyFocus(selectedNodeId.value)
  } catch {
    failGraph()
  }
}

async function retryGraph(): Promise<void> {
  graphFailed.value = false
  // v-if 从降级列表切回图形容器：等 DOM 更新后再挂 G6
  await nextTick()
  await mountGraph()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  void load()
})

// post-flush：ready 时图形容器已挂载，containerEl 才非空
watch(
  state,
  (s) => {
    if (s === 'ready' && !graphFailed.value) void mountGraph()
  },
  { flush: 'post' },
)

/**
 * 显式向 G6 重推一次图数据。sync 协调返回未变更信封（pending/missing）时，
 * 服务端 doc 与当前为同一引用，doc watcher 不会触发——此时必须把画布权威态
 * 再断言一次（建议虚节点原样保留、不发生迁移），避免“看似没动=无法验证”。
 */
function pushGraphData(opts: { refit?: boolean } = {}): void {
  if (!doc.value || graphFailed.value || !inst) return
  try {
    inst.setData?.(currentG6Data())
    void Promise.resolve(inst.render?.()).then(async () => {
      // 视图整体切换/回滚后重排视口；展开折叠等结构微调保持用户当前视口
      if (opts.refit) await applyInitialViewport()
      // 重渲染清空 state：恢复固化焦点（或 hover 临时焦点）
      syncViewport()
      applyFocus(selectedNodeId.value ?? hoverNodeId)
      // RC-301：引用定位触发的展开重绘完成后，把目标节点平移居中
      const citeId = pendingCiteFocus.value
      if (citeId) {
        pendingCiteFocus.value = null
        try {
          await inst?.focusElement?.(citeId, true)
        } catch {
          // 目标仍未进入渲染（懒加载缺失）：保留选中高亮，不打断
        }
      }
    })
  } catch {
    failGraph()
  }
}

// 折叠结果变化（固化焦点/跳数/手动展开段）→ 重推 G6 数据，保持当前视口
watch(collapsedChain, () => pushGraphData())

// P2：进出全局概览整体替换 G6 数据，需要重排视口
watch(overviewMode, () => pushGraphData({ refit: true }))

// P3：研判视角（process/tier）切换 — 节点坐标重排且 Tier 3 半透规则激活；
// 流程视角之间不会真的改变坐标，但要确保 setData 把新的 opacity 规则带入 G6
watch(perspective, () => pushGraphData({ refit: true }))

// 投影变化（展开集/层开关/模式）或维度懒取完成后重推
watch(
  [renderDoc, () => JSON.stringify(dimensionMap)],
  () => {
    const refit = pendingRefit
    pendingRefit = false
    pushGraphData({ refit })
  },
)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  // 离开页面不补发坐标保存（服务端版本可能已前进，避免离屏 409 弹窗）
  autosaver.cancel()
  try {
    inst?.destroy?.()
  } catch {
    // ignore
  }
  inst = null
})

function nodeLabel(id: string): string {
  return doc.value?.nodes.find((n) => n.id === id)?.label ?? id
}
</script>

<template>
  <div class="rcanvas">
    <!-- 加载骨架 -->
    <div v-if="state === 'loading'" data-testid="canvas-skeleton" class="canvas-skeleton">
      <NSpin size="small" />
      <span class="skel-text">正在根据线索事实初始化画布…</span>
    </div>

    <!-- 错误态 -->
    <div v-else-if="state === 'error'" data-testid="canvas-error" class="canvas-state-box">
      <NAlert type="error" :show-icon="false" :bordered="false">
        <div class="err-line">画布加载失败，请重试</div>
        <div class="err-detail dim">{{ errorMsg }}</div>
        <NButton size="small" type="primary" class="retry-btn" @click="load">
          <NIcon :component="RefreshOutline" />
          重试
        </NButton>
      </NAlert>
    </div>

    <template v-else>
      <!-- 语义层未构建横幅（事实/数据行仍可用，对象层 M2） -->
      <NAlert
        v-if="!semanticReady"
        type="warning"
        :show-icon="false"
        :bordered="false"
        class="banner"
        data-testid="canvas-semantic-banner"
      >
        语义层未构建，实体关联暂不可用；数据行溯源仍可使用
      </NAlert>

      <!-- M3 工具栏（RC-201/202/206）+ M4 RC-204 扩展查询 -->
      <CanvasToolbar
        v-if="!isEmpty"
        :connect-mode="connectMode"
        :chain-collapsed="passThroughCollapsed"
        :overview="overviewMode"
        :perspective="perspective"
        :busy="busy"
        @add-hypothesis="openCreateNode('hypothesis')"
        @add-note="openCreateNode('note')"
        @relayout="onRelayout"
        @toggle-collapse="onToggleCollapse"
        @toggle-overview="setOverviewMode(!overviewMode)"
        @toggle-perspective="onTogglePerspective"
        @fit="onFit"
        @zoom-in="onZoom(1.2)"
        @zoom-out="onZoom(1 / 1.2)"
        @toggle-connect="toggleConnectMode"
        @open-snapshots="openSnapshots"
        @open-function-query="openFunctionQuery"
        @open-chat="openChat"
        @expand-all-details="onExpandAllDetails"
        @collapse-all-details="onCollapseAllDetails"
      />

      <!-- 空状态 -->
      <div v-if="isEmpty" data-testid="canvas-empty" class="canvas-state-box">
        <NAlert type="info" :show-icon="false" :bordered="false">
          暂无可溯源事实，请先在「核查工作区」补充或运行检测
        </NAlert>
      </div>

      <!-- 降级：节点-关系列表（RC-207） -->
      <div v-else-if="graphFailed" data-testid="canvas-fallback" class="fb">
        <div class="fb-head">
          <span class="dim">图形视图加载失败，已降级为节点-关系列表</span>
          <NButton size="small" @click="retryGraph">
            <NIcon :component="RefreshOutline" />
            重试图形视图
          </NButton>
        </div>
        <div class="fb-grid">
          <div class="fb-col">
            <h4>节点（{{ doc?.nodes.length ?? 0 }}）</h4>
            <div v-for="g in groupedNodes" :key="g.kind" class="fb-group">
              <div class="fb-group-title">{{ g.label }}</div>
              <div
                v-for="n in g.nodes"
                :key="n.id"
                class="fb-node fb-node--btn"
                :data-node-id="n.id"
                role="button"
                tabindex="0"
                @click="onNodeClick(undefined, n.id)"
                @keyup.enter="onNodeClick(undefined, n.id)"
              >
                <span class="fb-node-label">{{ n.label }}</span>
                <span v-if="n.stale" class="fb-stale">引用已失效</span>
              </div>
            </div>
          </div>
          <div class="fb-col">
            <h4>关系（{{ edges.length }}）</h4>
            <table class="fb-table">
              <thead>
                <tr><th>起点</th><th>关系</th><th>终点</th></tr>
              </thead>
              <tbody>
                <tr v-for="e in edges" :key="e.id" class="fb-edge" :data-edge-id="e.id">
                  <td>{{ nodeLabel(e.source) }}</td>
                  <td class="rel">{{ e.rel }}</td>
                  <td>{{ nodeLabel(e.target) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- G6 图形视图 -->
      <div v-else class="canvas-stage">
        <div
          ref="containerEl"
          data-testid="canvas-graph"
          class="canvas-graph"
          :class="{ 'canvas-graph--connect': connectMode }"
        />
        <!-- P1 焦点路径条：来路可见、每层可回退 -->
        <CanvasPathBar
          v-if="trailItems.length"
          class="canvas-pathbar"
          :trail="trailItems"
          :current-id="selectedNodeId"
          @jump="onTrailJump"
          @reset="onTrailReset"
        />

        <!-- 状态栏：节点/关系/视图/缩放/版本（泳道伪节点不计入计数） -->
        <div class="canvas-status" data-testid="canvas-status">
          <span>
            节点 {{ statusCounts.nodes }}<template v-if="statusCounts.totalNodes !== statusCounts.nodes"> / {{ statusCounts.totalNodes }}</template>
          </span>
          <span>关系 {{ statusCounts.edges }}</span>
          <span v-if="statusCounts.collapsed > 0">
            折叠 {{ statusCounts.collapsed }} 个过路节点
          </span>
          <span>{{ overviewMode ? '全局概览' : (viewMode === 'compact' ? '简洁' : '完整') }}</span>
          <span
            :class="{ 'status-active': perspective === 'tier' && !overviewMode }"
          >视角：{{ perspective === 'tier' ? '证据强度' : '流程' }}</span>
          <span v-if="perspective === 'tier' && !overviewMode" class="tier-breakdown">
            <b class="mono">{{ tierCounts[1] }}</b>
            <span class="dim">已锁死 ·</span>
            <b class="mono">{{ tierCounts[2] }}</b>
            <span class="dim">待核实 ·</span>
            <b class="mono">{{ tierCounts[3] }}</b>
            <span class="dim">{{ TIER_LABELS[3] }}</span>
          </span>
          <span>缩放 {{ zoomPct }}%</span>
          <span v-if="selectedNodeId && !overviewMode" class="hop-ctl">
            焦点跳数
            <button
              type="button"
              class="hop-btn"
              data-testid="hop-minus"
              :disabled="focusHops <= 1"
              @click="setFocusHops(focusHops - 1)"
            >−</button>
            <b class="mono">{{ focusHops }}</b>
            <button
              type="button"
              class="hop-btn"
              data-testid="hop-plus"
              :disabled="focusHops >= FOCUS_HOPS_MAX"
              @click="setFocusHops(focusHops + 1)"
            >+</button>
          </span>
          <span>版本 v{{ version }}</span>
          <span v-if="viewportOverflow" class="status-warn">
            内容超出视口，拖拽浏览或点「适应屏幕」
          </span>
        </div>

        <!-- P2 全局缩略图：放大/拖拽后告诉用户「我在哪」+ 点击定位 -->
        <CanvasMinimap
          v-if="!overviewMode && doc"
          class="canvas-minimap"
          :nodes="minimapNodes"
          :box="viewportBox"
          :view="minimapView"
          @pan="onMinimapPan"
        />

        <!-- UX P0：图例 + 视图开关（悬浮，不挡节点列头） -->
        <CanvasLegend
          class="canvas-legend"
          :view-mode="viewMode"
          :layers="{ ...layerForced }"
          :collapsed="legendCollapsed"
          data-testid="canvas-legend"
          @update:view-mode="onViewModeChange"
          @toggle-layer="onLayerToggle"
          @update:collapsed="onLegendCollapse"
        />
        <!-- 事实计数胶囊明细预览（字段明文只在节点抽屉遮蔽 DTO 内 RC-104） -->
        <FactDetailPopover
          v-if="factPopoverNode"
          :fact="factPopoverNode"
          :group="detailModel.groupByFact.get(factPopoverNode.id) ?? null"
          :node-by-id="doc?.nodes ?? []"
          :ordinals="ordinals"
          :position="factPopoverPos"
          @close="closeFactPopover"
          @open-node="onPopoverOpenNode"
        />
        <div v-if="connectMode" class="connect-hint" data-testid="connect-hint">
          连线模式：依次点击起点、终点节点建立人工关系（Esc 退出）
        </div>
        <EdgeCreatePopover
          :show="pendingEdge !== null"
          :source="pendingEdge?.source ?? null"
          :target="pendingEdge?.target ?? null"
          :rels="pendingEdge?.rels ?? []"
          @pick="onPickPendingEdge"
          @cancel="pendingEdge = null"
        />
      </div>
    </template>

    <!-- RC-102/103/104 + M3 RC-201/202/203 节点抽屉 -->
    <CanvasNodeDrawer
      v-model:show="drawerShow"
      :node="drawerNode"
      :ui-state="drawerState"
      :detail="drawerDetail"
      :notices="drawerNodeId ? nodeNotices[drawerNodeId] ?? [] : []"
      :truncated="drawerNodeId ? nodeTruncated[drawerNodeId] === true : false"
      :audit="drawerAudit"
      :audit-loading="drawerNode?.ref ? auditLoading[drawerNode.ref] === true : false"
      :audit-missing="drawerNode?.ref ? auditMissing[drawerNode.ref] === true : false"
      v-model:audit-view="auditView"
      :has-expanded="Boolean(drawerHasExpanded)"
      :manual-edges="drawerManualEdges"
      :nodes="doc?.nodes ?? []"
      :busy="busy"
      :suggesting="drawerSuggesting"
      :suggestion-state="drawerSuggestionState"
      :adopt-state="drawerAdoptState"
      :to-verifying="toVerifying"
      :to-verify-error="toVerifyError"
      :verify-suggestions="verifySuggestions"
      @expand="onDrawerExpand"
      @retry="retryExpand"
      @collapse="collapseCurrent"
      @update:audit-view="onAuditViewChange"
      @edit-node="openEditNode"
      @delete-node="requestDeleteNode"
      @unpin-node="unpinNode"
      @delete-edge="requestDeleteEdge"
      @gen-suggestions="onGenSuggestions"
      @adopt-suggestion="onDrawerAdopt"
      @retry-adopt="onDrawerAdopt"
      @confirm-to-verify="confirmToVerify"
      @focus-node="onFocusNode"
    />

    <!-- M4 RC-204 扩展查询（白名单只读 Function） -->
    <FunctionQueryModal
      ref="fqModalRef"
      v-model:show="fqShow"
      :functions="fqFunctions"
      :available="fqAvailable"
      :source-node-label="fqSourceLabel"
      :busy="fqBusy"
      @submit="submitFunctionQuery"
    />

    <!-- RC-202 人工节点表单 -->
    <ManualNodeModal
      v-model:show="nodeModal.show"
      :mode="nodeModal.mode"
      :kind="nodeModal.kind"
      :node="drawerNode"
      @submit="submitNode"
    />

    <!-- RC-206 快照 -->
    <SnapshotDrawer
      v-model:show="snapshotShow"
      :snapshots="snapshots"
      :loading="snapshotLoading"
      :creating="snapshotCreating"
      :rolling-back-id="rollingBackId"
      @create="createSnapshot"
      @rollback="rollbackTo"
    />

    <!-- M5 RC-301 画布问答侧栏 -->
    <CanvasChatPanel
      v-if="chatShow"
      :case-id="caseId"
      :clue-id="clueId"
      class="canvas-chat-drawer"
      @cite-click="onCiteClick"
    />
  </div>
</template>

<style scoped>
.rcanvas {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.canvas-skeleton {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 40px 0;
  color: var(--sun-text-secondary);
  font-size: 13px;
}
.canvas-state-box {
  padding: 24px 0;
}
.err-line {
  font-size: 13px;
  margin-bottom: 4px;
}
.err-detail {
  font-size: 12px;
  margin-bottom: 10px;
}
.banner {
  border-radius: 4px;
}
.canvas-stage {
  position: relative;
}
.canvas-chat-drawer {
  position: absolute;
  top: 0;
  right: 0;
  width: 380px;
  height: 100%;
  z-index: 30;
  border-left: 1px solid var(--sun-border);
  border-radius: 0 6px 6px 0;
  box-shadow: -4px 0 16px rgba(0, 0, 0, 0.15);
}
.canvas-graph {
  width: 100%;
  /* 跟随视口高度，避免大屏下画布下方留大片空白 */
  height: clamp(480px, calc(100vh - 300px), 900px);
  min-height: 380px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background:
    radial-gradient(circle at 50% 40%, rgba(109, 200, 236, 0.05), transparent 70%),
    var(--sun-bg-card);
  overflow: hidden;
}
.canvas-graph--connect {
  outline: 2px dashed var(--sun-canvas-edge-manual);
  outline-offset: -2px;
}
.canvas-legend {
  position: absolute;
  top: 10px;
  right: 12px;
  z-index: 20;
  max-width: 248px;
}
.canvas-pathbar {
  position: absolute;
  top: 10px;
  left: 12px;
  z-index: 20;
}
.canvas-minimap {
  position: absolute;
  right: 12px;
  bottom: 8px;
  z-index: 20;
}
.canvas-status {
  position: absolute;
  left: 12px;
  bottom: 8px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  max-width: calc(100% - 280px);
  padding: 4px 10px;
  border: 1px solid var(--sun-border);
  border-radius: 999px;
  background: rgba(5, 21, 34, 0.92);
  font-size: 12px;
  color: var(--sun-text-secondary);
  pointer-events: none;
}
.canvas-status .status-warn {
  color: var(--sun-warn-text);
}
/* 研判视角激活时高亮标识 */
.canvas-status .status-active {
  color: var(--sun-border-active);
}
/* tier 计数胶囊：三级用 dim 色 + 数字 mono，便于一眼比较 */
.canvas-status .tier-breakdown {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 8px;
  border: 1px solid var(--sun-border);
  border-radius: 999px;
  background: rgba(110, 222, 233, 0.06);
}
.canvas-status .tier-breakdown b {
  color: var(--sun-text-primary);
  font-weight: 600;
}
/* 状态栏整体不吃鼠标事件，只有控件例外 */
.canvas-status .hop-ctl {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  pointer-events: auto;
}
.hop-btn {
  appearance: none;
  width: 18px;
  height: 18px;
  line-height: 1;
  border: 1px solid var(--sun-border);
  border-radius: 3px;
  background: transparent;
  color: var(--sun-text-secondary);
  font-size: 12px;
  cursor: pointer;
  padding: 0;
}
.hop-btn:hover:not(:disabled) {
  border-color: var(--sun-border-active);
  color: var(--sun-border-active);
}
.hop-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.connect-hint {
  position: absolute;
  /* 让位给左上角的焦点路径条 */
  top: 46px;
  left: 12px;
  z-index: 20;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  color: var(--sun-canvas-hint-ink);
  background: rgba(255, 157, 77, 0.18);
  border: 1px solid rgba(255, 157, 77, 0.55);
  pointer-events: none;
}
.dim {
  color: var(--sun-text-tertiary);
  font-size: 12px;
}
.fb-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.fb-grid {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(320px, 1.4fr);
  gap: 16px;
}
.fb-col h4 {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--sun-text-secondary);
}
.fb-group {
  margin-bottom: 10px;
}
.fb-group-title {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  margin-bottom: 4px;
}
.fb-node {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 5px 8px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  margin-bottom: 4px;
  font-size: 12px;
  background: var(--sun-bg-card);
}
.fb-node--btn {
  cursor: pointer;
}
.fb-node--btn:hover {
  border-color: var(--sun-border-active);
}
.fb-stale {
  font-size: 10px;
  color: var(--sun-text-tertiary);
}
.fb-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.fb-table th {
  text-align: left;
  font-weight: 400;
  color: var(--sun-text-tertiary);
  padding: 4px 8px;
  border-bottom: 1px solid var(--sun-border);
}
.fb-table td {
  padding: 5px 8px;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.5);
}
.fb-table .rel {
  color: var(--sun-border-active);
  white-space: nowrap;
}
</style>
