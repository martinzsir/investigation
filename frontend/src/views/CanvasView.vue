<script setup lang="ts">
// 案件级研判画布（case# 域，P2 窗口框架；PRD V1.0.0 功能 2/3）。
// P2 交付：G6 成图渲染（research-card 复合节点）+ 卡内迷你符号
// （精度点/时间条/待裁决——规格经 domain/canvas-window 单一来源）+
// 复合节点「窗口」按钮 + 贴附浮窗宿主（未钉住 1 / 钉住 3、Esc/空白关闭、
// 视口跟随、放大入口）+ 添加节点/连线编辑通路 + 底部状态条。
// 纪律：G6 动态 import；结构写操作走案件级专用端点（逐动作审计）；
// 连线矩阵前端 caseCanConnect 先拒、后端 can_connect 单一真相兜底。
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
  type Component,
} from 'vue'
import { NButton, NDropdown, NIcon, NSpin, useMessage } from 'naive-ui'
import { AddOutline, GitNetworkOutline, RefreshOutline } from '@vicons/ionicons5'
import { useRouter } from 'vue-router'
import { useCaseStore } from '../stores/case'
import { caseCanvasApi } from '../api/endpoints/canvas'
import { tasksApi } from '../api/endpoints/tasks'
import { failureSummary, isTerminal, type TaskRow } from '../domain/task'
import { presentError } from '../api/errors'
import {
  caseAllowedRels,
  caseCanConnect,
  KIND_LABELS,
  type CanvasNode,
  type CaseCanvasEnvelope,
  type CaseManualNodeKind,
  type NodeKind,
} from '../domain/canvas'
import {
  hasWindow,
  precisionSymbol,
  timeAxisRangeOf,
  timeBarSpec,
  windowFor,
  windowEnlargeRoute,
  type WindowKind,
  AMBIGUOUS_BORDER,
} from '../domain/canvas-window'
import { canvasTokens } from '../design/tokens'
import { contentBox, contentCenter, fitScale } from '../domain/canvas-viewport'
import type { ToolboxNode } from '../domain/canvas-toolbox'
import EmptyState from '../components/common/EmptyState.vue'
import CanvasNodeWindow from '../components/research/CanvasNodeWindow.vue'
import CanvasToolbox from '../components/research/CanvasToolbox.vue'
import CaseNodeModal from '../components/research/CaseNodeModal.vue'
import RelationWindow from '../components/research/win/RelationWindow.vue'
import MapWindow from '../components/research/win/MapWindow.vue'
import TimeWindow from '../components/research/win/TimeWindow.vue'
import EvidenceWindow from '../components/research/win/EvidenceWindow.vue'
import HypothesisWindow from '../components/research/win/HypothesisWindow.vue'
import { RESEARCH_CARD_NODE, ensureResearchCardNode } from '../components/research/g6-card-node'

const router = useRouter()
const message = useMessage()

const cs = useCaseStore()
const caseId = computed(() => cs.currentCaseId ?? '')
const caseName = computed(() => cs.currentCase?.name ?? cs.currentCaseId)

// ----------------------------------------------------------------------
// 读面
// ----------------------------------------------------------------------
const loading = ref(false)
const error = ref('')
const envelope = ref<CaseCanvasEnvelope | null>(null)
const doc = computed(() => envelope.value?.doc ?? null)
const version = computed(() => envelope.value?.version ?? 0)

async function load(): Promise<void> {
  if (!caseId.value) return
  loading.value = true
  error.value = ''
  try {
    envelope.value = await caseCanvasApi.get(caseId.value)
  } catch (e) {
    error.value = presentError(e).title
  } finally {
    loading.value = false
    await nextTick()
    void renderGraph()
  }
}

onMounted(() => void load())
watch(caseId, () => void load())

/** 待裁决主体数（状态条；红线 R2 可视化） */
const ambiguousCount = computed(
  () =>
    doc.value?.nodes.filter((n) => n.props?.person_ambiguous === true).length ?? 0,
)

// ----------------------------------------------------------------------
// G6 成图（动态 import；失败降级节点列表）
// ----------------------------------------------------------------------
interface G6Instance {
  render?: () => Promise<unknown>
  setData?: (d: unknown) => void
  destroy?: () => void
  resize?: () => Promise<unknown> | void
  on?: (event: string, handler: (ev: unknown) => void) => void
  off?: (event: string, handler: (ev: unknown) => void) => void
  zoomTo?: (zoom: number) => Promise<unknown>
  getZoom?: () => number
  getSize?: () => [number, number]
  translateBy?: (offset: [number, number]) => Promise<unknown>
  getViewportByCanvas?: (point: [number, number]) => [number, number]
  getElementPosition?: (id: string) => [number, number]
}
interface G6Datum {
  data?: Record<string, unknown>
}
interface G6Event {
  id?: unknown
  target?: { id?: unknown }
  originalTarget?: { className?: unknown }
}

const containerEl = ref<HTMLDivElement | null>(null)
const graphFailed = ref(false)
let inst: G6Instance | null = null
let renderQueued = false

const KIND_GLYPH: Record<NodeKind, string> = {
  rule: '规', fact: '实', object: '体', source_row: '行', source_file: '档',
  verify_item: '核', evidence: '证', hypothesis: '假', note: '备',
  function_result: '查', subject: '人', place: '地', event: '事',
  analysis_result: '结',
}

/** 案件时间轴范围（卡内时间条几何的唯一轴口径） */
const timeAxis = computed(() => timeAxisRangeOf(doc.value?.nodes ?? []))

/** 节点 → G6 data（迷你符号经 canvas-window 纯函数算好传入，R1 单一来源） */
function toG6Data(): unknown {
  const d = doc.value
  if (!d) return { nodes: [], edges: [] }
  const axis = timeAxis.value
  const nodes = d.nodes.map((n) => {
    const p = (n.props ?? {}) as Record<string, unknown>
    const ambiguous = p.person_ambiguous === true
    const bar = timeBarSpec(p, axis)
    const kind = n.kind
    return {
      id: n.id,
      style: { x: n.x, y: n.y },
      data: {
        label: n.label,
        kind,
        glyph: KIND_GLYPH[kind],
        chip: canvasTokens.kind[kind].chip,
        chipInk: canvasTokens.kind[kind].ink,
        subtitle: subOf(n),
        pinned: n.pinned === true,
        manual: n.system !== true,
        ambiguous,
        // P2 迷你符号（R1：规格只来自 canvas-window 纯函数）
        miniDot: precisionSymbol(p.time_precision),
        miniBar: bar
          ? { start: bar.start, span: bar.span, symbol: bar.symbol }
          : null,
        miniAmbiguous: ambiguous,
        winnable: hasWindow(kind),
        deletable: n.system !== true,
        // P3 回写上图脉冲：新产出的结论节点 3s 内描边高亮
        fresh: freshIds.value.has(n.id),
      },
    }
  })
  const edges = d.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    data: {
      label: e.rel,
      system: e.system === true,
      note: e.note ?? '',
    },
  }))
  return { nodes, edges }
}

function subOf(n: CanvasNode): string {
  const p = (n.props ?? {}) as Record<string, unknown>
  switch (n.kind) {
    case 'subject':
      return p.person_ambiguous === true ? `${n.label} · 待裁决` : String(p.person_pk ?? '')
    case 'place':
      return String(p.std_address ?? p.coord_precision ?? '')
    case 'event':
      return String(p.time_start ?? '')
    case 'analysis_result':
      return String(p.lens_title ?? p.lens_id ?? '')
    case 'hypothesis':
    case 'note':
      return String(p.content ?? '').slice(0, 40)
    default:
      return ''
  }
}

/** 案件画布徽标：右上「窗口」（研判 5 类）+ 人工节点「×」删除 */
function cardBadges(data: Record<string, unknown>): Array<Record<string, unknown>> {
  const badges: Array<Record<string, unknown>> = []
  if (data.winnable === true) {
    badges.push({
      text: '窗',
      className: 'badge-window',
      placement: 'right-top',
      backgroundFill: canvasTokens.kind.subject.chip,
      fill: '#FFFFFF',
      fontSize: 9,
      padding: [1, 4],
    })
  }
  if (data.deletable === true) {
    badges.push({
      text: '×',
      className: 'badge-delete',
      placement: 'right-bottom',
      backgroundFill: '#8A97AC',
      fill: '#FFFFFF',
      fontSize: 9,
      padding: [1, 4],
    })
  }
  return badges
}

async function mountGraph(): Promise<void> {
  if (graphFailed.value || !containerEl.value || !doc.value) return
  let GraphCtor: new (cfg: unknown) => G6Instance
  try {
    const mod = await import('@antv/g6')
    GraphCtor = mod.Graph as unknown as new (cfg: unknown) => G6Instance
  } catch {
    graphFailed.value = true
    return
  }
  try {
    ensureResearchCardNode()
    inst = new GraphCtor({
      container: containerEl.value,
      autoResize: true,
      data: toG6Data(),
      node: {
        type: RESEARCH_CARD_NODE,
        style: {
          size: [186, 50],
          radius: 8,
          lineWidth: (d: G6Datum) => (d.data?.fresh ? 2.2 : 1.25),
          // 红线 R2：重名待裁决虚线边框（口径取 AMBIGUOUS_BORDER）；
          // fresh 脉冲优先（回写上图 3s 高亮，暖橙 = minute 档符号色系）
          lineDash: (d: G6Datum) =>
            d.data?.fresh ? [] :
            d.data?.ambiguous ? AMBIGUOUS_BORDER.lineDash : [],
          stroke: (d: G6Datum) =>
            d.data?.fresh
              ? '#FF7043'
              : d.data?.ambiguous
                ? AMBIGUOUS_BORDER.color
                : d.data?.manual
                  ? canvasTokens.strokeManual
                  : canvasTokens.stroke,
          fill: canvasTokens.surface,
          cursor: 'pointer',
          chipText: (d: G6Datum) => d.data?.glyph ?? '',
          chipFill: (d: G6Datum) => d.data?.chip ?? 'transparent',
          chipInk: (d: G6Datum) => d.data?.chipInk ?? canvasTokens.title,
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
          labelMaxLines: 1,
          labelTextOverflow: 'ellipsis',
          miniDot: (d: G6Datum) => d.data?.miniDot ?? null,
          miniBar: (d: G6Datum) => d.data?.miniBar ?? null,
          miniAmbiguous: (d: G6Datum) => d.data?.ambiguous === true,
          badges: (d: G6Datum) => cardBadges(d.data ?? {}),
        },
      },
      edge: {
        type: 'cubic',
        style: {
          stroke: (d: G6Datum) =>
            d.data?.system === false ? canvasTokens.edgeManual : canvasTokens.edgeSystem,
          lineWidth: (d: G6Datum) => (d.data?.system === false ? 1.6 : 1.4),
          lineDash: (d: G6Datum) => (d.data?.system === false ? [6, 4] : []),
          endArrow: true,
          labelText: (d: G6Datum) => d.data?.label ?? '',
          labelFill: canvasTokens.edgeLabelFill,
          labelFontSize: 10,
          labelBackground: true,
          labelBackgroundFill: canvasTokens.edgeLabelBg,
          labelBackgroundRadius: 4,
          labelPadding: [2, 5],
          opacity: 0.9,
        },
      },
      layout: { type: 'preset' },
      animation: false,
    })
    inst.on?.('node:click', onNodeClick)
    inst.on?.('canvas:click', onCanvasBlankClick)
    inst.on?.('viewportchange', syncWindowAnchors)
    // 必须先首帧 render 再调视口 API：G6 v5 构造期 data 不自动出图，
    // 且 render 前 zoomTo/translateBy 会让拾取矩阵与渲染相机错位（点击不命中）。
    await inst.render?.()
    await applyInitialViewport()
  } catch {
    failGraph()
  }
}

function failGraph(): void {
  try {
    inst?.destroy?.()
  } catch {
    /* ignore */
  }
  inst = null
  graphFailed.value = true
}

async function renderGraph(): Promise<void> {
  if (graphFailed.value || !doc.value) return
  if (!inst) {
    await mountGraph()
    return
  }
  try {
    inst.setData?.(toG6Data())
    await inst.render?.()
  } catch {
    failGraph()
  }
}

async function applyInitialViewport(): Promise<void> {
  if (!inst || !doc.value) return
  const box = contentBox(doc.value.nodes)
  const [vw, vh] = inst.getSize?.() ?? [0, 0]
  const scale = fitScale(box, vw, vh)
  try {
    await inst.zoomTo?.(scale)
    const center = contentCenter(box)
    const now = inst.getViewportByCanvas?.(center)
    if (now) await inst.translateBy?.([vw / 2 - now[0], vh / 2 - now[1]])
  } catch {
    /* 视口 API 不可用（降级环境）：不影响成图 */
  }
}

onBeforeUnmount(() => {
  stopLensPoll()
  try {
    inst?.destroy?.()
  } catch {
    /* ignore */
  }
  inst = null
})

// ----------------------------------------------------------------------
// P2 窗口框架：状态机 + 竞态 + 钉住 + 跟随 + 放大
// ----------------------------------------------------------------------
interface WinState {
  nodeId: string
  /** 案件级窗口分型（研判 5 类；source 溯源弹层不进案件画布） */
  kind: Exclude<WindowKind, 'source'>
  loading: boolean
  error: string
}
/** 未钉住窗口（同时最多 1 个） */
const activeWin = ref<WinState | null>(null)
/** 钉住窗口（最多 3 个，PRD 窗口协议） */
const pinnedWins = reactive<WinState[]>([])
const PINNED_MAX = 3
/** 打开中的窗口（渲染序：未钉住在前）；同一窗口只会出现在其中一侧 */
const openWins = computed<WinState[]>(() =>
  activeWin.value ? [activeWin.value, ...pinnedWins] : [...pinnedWins],
)
/** 节点画布坐标 → 容器像素（浮窗贴附 anchor），viewportchange 同步 */
const anchorPx = reactive<Record<string, { x: number; y: number }>>({})

function nodeOf(id: string): CanvasNode | null {
  return doc.value?.nodes.find((n) => n.id === id) ?? null
}

function syncWindowAnchors(): void {
  if (!inst) return
  for (const w of [activeWin.value, ...pinnedWins]) {
    if (!w) continue
    const n = nodeOf(w.nodeId)
    if (!n) continue
    try {
      const vp = inst.getViewportByCanvas?.([n.x, n.y])
      if (vp) anchorPx[w.nodeId] = { x: vp[0], y: vp[1] }
    } catch {
      /* G6 内部状态未就绪 */
    }
  }
}

function anchorOf(nodeId: string): { x: number; y: number } {
  return anchorPx[nodeId] ?? { x: 0, y: 0 }
}

function viewportSize(): { w: number; h: number } {
  const size = inst?.getSize?.()
  if (size && Number.isFinite(size[0]) && size[0] > 0) {
    return { w: size[0], h: size[1] }
  }
  const el = containerEl.value
  return { w: el?.clientWidth ?? 800, h: el?.clientHeight ?? 600 }
}

function openWindow(node: CanvasNode): void {
  const kind = windowFor(node.kind)
  if (!kind || kind === 'source') return
  // 未钉住窗口唯一：开新窗即关旧窗（PRD 窗口协议；钉住窗口不受影响）
  activeWin.value = {
    nodeId: node.id,
    kind,
    loading: false,
    error: '',
  }
  syncWindowAnchors()
}

function closeWin(w: WinState): void {
  if (activeWin.value === w) activeWin.value = null
  const idx = pinnedWins.indexOf(w)
  if (idx >= 0) pinnedWins.splice(idx, 1)
}

function togglePin(w: WinState): void {
  const idx = pinnedWins.indexOf(w)
  if (idx >= 0) {
    // 已钉住 → 取消钉住：并入未钉住位（挤掉原 active）
    pinnedWins.splice(idx, 1)
    activeWin.value = w
    return
  }
  if (pinnedWins.length >= PINNED_MAX) {
    message.warning('最多钉住 3 个窗口，请先取消一个')
    return
  }
  if (activeWin.value === w) activeWin.value = null
  pinnedWins.push(w)
}

/**
 * 窗口开/关切换（PRD 关闭协议：再点同一节点=关闭）。
 * 钉住窗口不受此影响——取消钉住是唯一关闭途径（Esc/空白/再点均不关钉住窗）。
 */
function toggleWindow(node: CanvasNode): void {
  if (activeWin.value?.nodeId === node.id) {
    activeWin.value = null
    return
  }
  if (pinnedWins.some((w) => w.nodeId === node.id)) return
  openWindow(node)
}

function onEnlarge(w: WinState): void {
  const to = windowEnlargeRoute(w.kind)
  if (!to) return
  const node = nodeOf(w.nodeId)
  const n = node
  // P1 范围仅跳转；筛选参数（主体/地点预设）按 PRD 属 P1 放大带筛选
  const query: Record<string, string> = {}
  if (n && n.kind === 'subject') {
    const pk = String((n.props ?? {}).person_pk ?? '')
    if (pk) query.subject = pk
  }
  if (n && n.kind === 'place') {
    const loc = String((n.props ?? {}).location_id ?? '')
    if (loc) query.location = loc
  }
  void router.push({ path: to, query })
}

function onWinRetry(w: WinState): void {
  w.error = ''
  w.loading = true
  // 分型窗口数据自取（RelationWindow 内建重试）；此处仅复位错误态
  setTimeout(() => {
    w.loading = false
  }, 0)
}

/** 节点点击：窗口徽标 → 开窗；删除徽标 → 删除；否则选中即开窗（研判 5 类） */
function onNodeClick(ev: unknown): void {
  const e = ev as G6Event
  const id = String(e?.id ?? e?.target?.id ?? '')
  const node = nodeOf(id)
  if (!node) return
  const className =
    typeof e?.originalTarget?.className === 'string' ? e.originalTarget.className : ''
  if (className === 'badge-window') {
    toggleWindow(node)
    return
  }
  if (className === 'badge-delete') {
    void removeNode(node)
    return
  }
  // P3：点选即选靶心（工具箱滑出跟随；连线模式优先消费点击）
  if (connectArmed.value) {
    if (!connectSrc.value) {
      connectSrc.value = node
      connectTarget.value = null
      connectRel.value = null
      message.info(`已选源节点「${node.label}」：点击目标节点`)
      return
    }
    void onConnectTarget(node)
    return
  }
  selectedNode.value = node
  toolboxShow.value = true
  // 点击节点体 = 开它自己的窗口（无窗口类型则提示）
  if (hasWindow(node.kind)) {
    toggleWindow(node)
  } else {
    message.info(`${KIND_LABELS[node.kind]}节点无分型窗口`)
  }
}

function onCanvasBlankClick(): void {
  if (activeWin.value) activeWin.value = null
  connectArmed.value = false
  connectSrc.value = null
}

function onEsc(ev: KeyboardEvent): void {
  if (ev.key !== 'Escape') return
  if (activeWin.value) activeWin.value = null
  else {
    connectArmed.value = false
    connectSrc.value = null
  }
}

// ----------------------------------------------------------------------
// 编辑通路：添加节点 + 连线（矩阵先拒）+ 删除
// ----------------------------------------------------------------------
const busy = ref(false)
const nodeModalShow = ref(false)
const nodeModalRef = ref<InstanceType<typeof CaseNodeModal> | null>(null)

function spawnXY(): { x: number; y: number } {
  const n = doc.value?.nodes.length ?? 0
  return { x: 120 + (n % 4) * 240, y: 120 + Math.floor(n / 4) * 110 }
}

async function onModalSubmit(
  kind: CaseManualNodeKind,
  props: Record<string, unknown>,
): Promise<void> {
  if (!caseId.value) return
  const { x, y } = spawnXY()
  try {
    const env = await caseCanvasApi.createNode(caseId.value, {
      kind,
      props,
      x,
      y,
      version: version.value,
    })
    envelope.value = env
    nodeModalRef.value?.finish(true)
    await nextTick()
    void renderGraph()
  } catch (e) {
    nodeModalRef.value?.finish(false, presentError(e).title)
  }
}

/** 连线模式：true=已点「连线」等待选源；connectSrc 非空=已选源等待目标 */
const connectArmed = ref(false)
/** 连线模式：已点源节点，等待目标 */
const connectSrc = ref<CanvasNode | null>(null)
const connectTarget = ref<CanvasNode | null>(null)
const connectRel = ref<string | null>(null)

function startConnect(): void {
  connectArmed.value = true
  connectSrc.value = null
  connectTarget.value = null
  connectRel.value = null
  message.info('连线模式：点击源节点')
}

const connectRels = computed(() => {
  const s = connectSrc.value
  const t = connectTarget.value
  if (!s || !t) return []
  return caseAllowedRels(s.kind, t.kind)
})

const connectReject = computed(() => {
  const s = connectSrc.value
  const t = connectTarget.value
  if (!s || !t || s.id === t.id) return ''
  const rels = connectRels.value
  if (rels.length === 0) {
    const probe = caseCanConnect(s.kind, t.kind, '同现')
    return probe.reason ?? '该两类节点不能建立该关系'
  }
  return ''
})

async function onConnectTarget(node: CanvasNode): Promise<void> {
  const src = connectSrc.value
  if (!src) return
  if (src.id === node.id) {
    message.warning('不能连接节点自身')
    return
  }
  connectTarget.value = node
  connectRel.value = null
}

async function confirmConnect(): Promise<void> {
  const s = connectSrc.value
  const t = connectTarget.value
  const rel = connectRel.value
  if (!s || !t || !rel || !caseId.value) return
  // 前端先拒（矩阵），后端 can_connect 兜底
  const check = caseCanConnect(s.kind, t.kind, rel)
  if (!check.ok) {
    message.error(check.reason ?? '该连线不合法')
    return
  }
  busy.value = true
  try {
    const env = await caseCanvasApi.createEdge(caseId.value, {
      source: s.id,
      target: t.id,
      rel,
      version: version.value,
    })
    envelope.value = env
    message.success('连线已保存', { duration: 1500 })
    connectArmed.value = false
    connectSrc.value = null
    connectTarget.value = null
    connectRel.value = null
    await nextTick()
    void renderGraph()
  } catch (e) {
    message.error(`保存失败：${presentError(e).title}`)
  } finally {
    busy.value = false
  }
}

async function removeNode(node: CanvasNode): Promise<void> {
  if (!caseId.value) return
  busy.value = true
  try {
    const env = await caseCanvasApi.deleteNode(caseId.value, node.id, version.value)
    envelope.value = env
    message.success('节点已删除', { duration: 1500 })
    // 节点删了，窗口跟着关（悬挂窗口无意义）
    if (activeWin.value?.nodeId === node.id) activeWin.value = null
    const pi = pinnedWins.findIndex((w) => w.nodeId === node.id)
    if (pi >= 0) pinnedWins.splice(pi, 1)
    await nextTick()
    void renderGraph()
  } catch (e) {
    message.error(`删除失败：${presentError(e).title}`)
  } finally {
    busy.value = false
  }
}

// ----------------------------------------------------------------------
// P3 镜头工具箱：靶心选中 + 202 轮询 + 回写上图脉冲
// ----------------------------------------------------------------------
const selectedNode = ref<CanvasNode | null>(null)
const toolboxShow = ref(false)
/** 工具箱轻量节点视图（结构与 CanvasNode 兼容，裁剪为纯函数入参） */
const toolboxNode = computed<ToolboxNode | null>(() => {
  const n = selectedNode.value
  if (!n) return null
  return {
    id: n.id,
    kind: n.kind,
    label: n.label,
    props: (n.props ?? {}) as Record<string, unknown>,
  }
})

/** 回写上图脉冲：新产出 analysis_result 节点 id 集（3s 后清除重渲） */
const freshIds = ref<Set<string>>(new Set())

/** 状态条镜头任务项（轮询中） */
interface LensTaskState {
  id: string
  skillId: string
  name: string
  status: string
}
const lensTask = ref<LensTaskState | null>(null)
let lensPollTimer: number | null = null

function stopLensPoll(): void {
  if (lensPollTimer !== null) {
    window.clearTimeout(lensPollTimer)
    lensPollTimer = null
  }
}

function handleLensSubmitted(taskId: string, skillId: string, name: string): void {
  stopLensPoll()
  lensTask.value = { id: taskId, skillId, name, status: 'PENDING' }
  const started = Date.now()
  const tick = async (): Promise<void> => {
    if (!lensTask.value || lensTask.value.id !== taskId) return
    let t: TaskRow | null = null
    try {
      t = await tasksApi.get(taskId)
      if (lensTask.value) lensTask.value.status = t.status
      if (isTerminal(t.status)) {
        stopLensPoll()
        await onLensTaskDone(t)
        return
      }
    } catch {
      /* 瞬时轮询失败忽略；超时兜底在下方 */
    }
    if (Date.now() - started > 120_000) {
      stopLensPoll()
      lensTask.value = null
      message.warning('镜头轮询超时：任务仍在后台执行，可稍后刷新画布查看产出')
      return
    }
    lensPollTimer = window.setTimeout(() => void tick(), 2000)
  }
  void tick()
}

/** 镜头任务终态：重拉画布 → diff 新结论节点 → 脉冲高亮 3s */
async function onLensTaskDone(t: TaskRow): Promise<void> {
  const before = new Set((doc.value?.nodes ?? []).map((n) => n.id))
  await load()
  const fresh = (doc.value?.nodes ?? []).filter(
    (n) => n.kind === 'analysis_result' && !before.has(n.id),
  )
  if (t.status === 'SUCCEEDED' && fresh.length > 0) {
    freshIds.value = new Set(fresh.map((n) => n.id))
    await nextTick()
    void renderGraph()
    message.success(t.progress_detail || `镜头产出已上图（新增 ${fresh.length} 条结论）`, {
      duration: 4000,
    })
    window.setTimeout(() => {
      freshIds.value = new Set()
      void renderGraph()
    }, 3000)
  } else if (t.status === 'SUCCEEDED') {
    // 幂等重跑（同观察已上图）或无观察（progress_detail 带降级原因）
    message.info(t.progress_detail || '镜头完成：无新增画布产出', { duration: 4000 })
  } else {
    message.error(`镜头任务失败：${failureSummary(t)}`, { duration: 5000 })
  }
  lensTask.value = null
}

// ----------------------------------------------------------------------
// G6 事件绑定前的窗口锚点初始化
// ----------------------------------------------------------------------
watch(
  () => doc.value?.nodes.map((n) => n.id).join(','),
  () => {
    void nextTick().then(syncWindowAnchors)
  },
)

/** 分型窗口组件注册（研判 5 类；key 与 WinState.kind 同域） */
const windowComponents: Record<Exclude<WindowKind, 'source'>, Component> = {
  relation: RelationWindow,
  map: MapWindow,
  time: TimeWindow,
  evidence: EvidenceWindow,
  hypothesis: HypothesisWindow,
}
</script>

<template>
  <div class="canvas-page">
    <EmptyState v-if="!caseId" type="empty" title="研判画布" desc="请先在顶部选择案件" />
    <template v-else>
      <div class="head">
        <h2 class="head-title">案件研判画布 · {{ caseName }}</h2>
        <span v-if="envelope" class="head-meta" data-testid="case-canvas-meta">
          {{ envelope.canvas_domain }} 域 · v{{ envelope.version }} ·
          {{ envelope.doc.nodes.length }} 节点 ·
          {{ envelope.doc.edges.length }} 连线
        </span>
        <span class="head-spacer" />
        <NButton size="tiny" :disabled="busy" @click="startConnect">
          <template #icon><NIcon><GitNetworkOutline /></NIcon></template>
          连线
        </NButton>
        <NButton size="tiny" type="primary" :disabled="busy" @click="nodeModalShow = true">
          <template #icon><NIcon><AddOutline /></NIcon></template>
          添加节点
        </NButton>
        <NButton size="tiny" quaternary @click="() => load()">
          <template #icon><NIcon><RefreshOutline /></NIcon></template>
          刷新
        </NButton>
      </div>

      <div v-if="loading" class="loading"><NSpin size="small" /></div>
      <EmptyState
        v-else-if="error"
        type="error"
        title="画布读取失败"
        :desc="error"
      />
      <div
        v-else-if="envelope"
        class="board"
        data-testid="case-canvas-board"
      >
        <EmptyState
          v-if="envelope.doc.nodes.length === 0"
          class="board-empty"
          type="empty"
          title="这是一张空白的作战地图"
          desc="放置第一个主体节点，开始研判。点右上「添加节点」，或从主体候选摆人、地、事。"
        >
          <template #action>
            <NButton size="small" type="primary" @click="nodeModalShow = true">
              添加节点
            </NButton>
          </template>
        </EmptyState>
        <div ref="containerEl" class="g6-container" data-testid="case-g6" />
        <div v-if="graphFailed && envelope.doc.nodes.length > 0" class="fallback">
          <div
            v-for="n in envelope.doc.nodes"
            :key="n.id"
            class="fallback-node"
          >
            <strong>{{ n.label }}</strong>
            <span>{{ KIND_LABELS[n.kind] }}</span>
          </div>
        </div>

        <!-- 贴附浮窗宿主：未钉住 1 个 + 钉住至多 3 个 -->
        <CanvasNodeWindow
          v-for="w in openWins"
          :key="`${w.nodeId}-${pinnedWins.includes(w) ? 'pin' : 'act'}`"
          :window-kind="w.kind"
          :title="nodeOf(w.nodeId)?.label ?? ''"
          :anchor-x="anchorOf(w.nodeId).x"
          :anchor-y="anchorOf(w.nodeId).y"
          :viewport-w="viewportSize().w"
          :viewport-h="viewportSize().h"
          :pinned="pinnedWins.includes(w)"
          :loading="w.loading"
          :error="w.error"
          :enlarge-to="windowEnlargeRoute(w.kind)"
          @close="closeWin(w)"
          @toggle-pin="togglePin(w)"
          @enlarge="onEnlarge(w)"
          @retry="onWinRetry(w)"
          @cancel="closeWin(w)"
        >
          <component
            :is="windowComponents[w.kind]"
            v-if="nodeOf(w.nodeId)"
            :node="nodeOf(w.nodeId)!"
            :case-id="caseId"
            :doc="doc!"
          />
        </CanvasNodeWindow>

        <!-- 连线确认条（连线模式：源→目标 → 选关系） -->
        <div
          v-if="connectSrc && connectTarget"
          class="connect-bar"
          data-testid="connect-bar"
        >
          <span class="connect-endpoints">
            {{ connectSrc.label }} → {{ connectTarget.label }}
          </span>
          <NDropdown
            v-if="connectRels.length"
            :options="connectRels.map((r) => ({ label: r, key: r }))"
            trigger="click"
            @select="(k: string) => { connectRel = k; void confirmConnect() }"
          >
            <NButton size="tiny" type="primary" :loading="busy" data-testid="connect-confirm">
              选择关系（{{ connectRels.length }}）
            </NButton>
          </NDropdown>
          <span v-else class="connect-reject" data-testid="connect-reject">
            {{ connectReject || '该两类节点不能建立该关系' }}
          </span>
          <NButton
            size="tiny"
            quaternary
            @click="connectArmed = false; connectSrc = null; connectTarget = null"
          >
            取消
          </NButton>
        </div>
        <div v-else-if="connectSrc" class="connect-bar" data-testid="connect-src-bar">
          <span class="connect-endpoints">源：{{ connectSrc.label }}</span>
          <span class="connect-hint">点击目标节点完成连线</span>
          <NButton
            size="tiny"
            quaternary
            @click="connectArmed = false; connectSrc = null"
          >
            取消
          </NButton>
        </div>

        <!-- P3 镜头工具箱：选中节点滑出（提交 202 后宿主轮询回写） -->
        <CanvasToolbox
          :show="toolboxShow && !!toolboxNode"
          :case-id="caseId"
          :node="toolboxNode"
          @submitted="handleLensSubmitted"
          @update:show="toolboxShow = $event"
        />
      </div>

      <!-- 底部状态条 -->
      <footer v-if="envelope" class="statusbar" data-testid="case-canvas-statusbar">
        <span>节点 {{ envelope.doc.nodes.length }}</span>
        <span data-testid="statusbar-ambiguous">待裁决 {{ ambiguousCount }}</span>
        <span>连线 {{ envelope.doc.edges.length }}</span>
        <span v-if="activeWin || pinnedWins.length" class="statusbar-win">
          窗口 {{ (activeWin ? 1 : 0) + pinnedWins.length }}（钉住 {{ pinnedWins.length }}/3）
        </span>
        <span v-if="lensTask" class="statusbar-lens" data-testid="statusbar-lens">
          镜头 {{ lensTask.name }} · 运行中
        </span>
        <span v-if="graphFailed" class="statusbar-degraded">成图降级：节点列表模式</span>
      </footer>

      <CaseNodeModal
        ref="nodeModalRef"
        v-model:show="nodeModalShow"
        @submit="onModalSubmit"
      />
    </template>
  </div>
</template>

<style scoped>
.canvas-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  height: calc(100vh - 56px);
  box-sizing: border-box;
}
.head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: none;
}
.head-title {
  margin: 0;
  font-size: 15px;
  color: var(--sun-text-primary);
}
.head-meta {
  font-size: 12px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.head-spacer { flex: 1; }
.loading {
  display: flex;
  justify-content: center;
  padding: 40px 0;
}
.board {
  position: relative;
  flex: 1;
  min-height: 320px;
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 10px;
  overflow: hidden;
  background: var(--sun-surface, #fff);
}
.board-empty {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
}
.g6-container {
  position: absolute;
  inset: 0;
}
.fallback {
  position: absolute;
  inset: 0;
  overflow: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--sun-surface, #fff);
}
.fallback-node {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 6px 10px;
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 6px;
  font-size: 12px;
}
.fallback-node span {
  color: var(--sun-text-tertiary);
  font-size: 11px;
}
.connect-bar {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  background: var(--sun-surface, #fff);
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 8px;
  box-shadow: 0 4px 14px rgba(10, 27, 54, 0.12);
  font-size: 12px;
}
.connect-endpoints { font-weight: 600; color: var(--sun-text-primary); }
.connect-hint, .connect-reject { color: var(--sun-text-tertiary); }
.connect-reject { color: var(--sun-danger, #d03050); }
.statusbar {
  flex: none;
  display: flex;
  gap: 16px;
  align-items: center;
  height: 28px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  border-top: 1px solid var(--sun-border, #e2e8f2);
  padding-top: 4px;
}
.statusbar-win { color: var(--sun-primary, #2f6fed); }
.statusbar-lens { color: var(--sun-primary, #2f6fed); font-weight: 600; }
.statusbar-degraded { color: #8a5a00; }
</style>
