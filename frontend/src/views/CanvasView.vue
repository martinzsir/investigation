<script setup lang="ts">
/**
 * 研判层画布（案件级独立页面，CAN-01）
 *
 * 为什么是独立页面而不是嵌在线索里
 * --------------------------------
 * `ResearchCanvas` 只挂在 ClueDetailView 内，是**线索级**溯源画布：
 * 节点键 sha1("{clue_id}|fact|{i}") 锁死在单条线索内，长不成案件总图。
 * 研判层画布是**案件总图**——多线索并入同一张图，所以它必须独立成页。
 *
 * 与线索级画布的关系
 * ------------------
 * 两者共用 domain 层（canvas-symbol / canvas-window / subject-picker），
 * 但持久化端点分开：这里走 GET/PATCH /cases/{cid}/case-canvas。
 * 页面独立 ≠ 数据模型独立——共用 domain 才不会演化成分叉的真相。
 *
 * 保存语义（阶段 3）
 * ------------------
 * 镜头层由定向观察档案每次 GET 重建，**不落库**；保存时服务端剥掉
 * `generated_by = 'lens_layer'` 的节点只存人工层。前端只管整文档入，
 * 且**绝不用 PATCH 返回覆盖本地 doc**（返回不含 doc，覆盖会抹成空图）。
 *
 * 两条红线
 * --------
 * 1. 符号口径统一：卡片/连线/窗口三处都取 nodeSymbol/edgeSymbol（SYM-01）。
 *    日期级画成实线粗边，会让 12 条模糊记录压过 4 条精确记录。
 * 2. 空态必须写原因：名录读不出、镜头层为空，都要显示 why，
 *    静默空列表会被读成「确实没有」。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useCaseStore } from '../stores/case'
import { NAlert, NButton, NCheckbox, NEmpty, NSpin, NTag, useMessage } from 'naive-ui'
import { canvasApi } from '../api/endpoints/canvas'
import { lensesApi, type LensSpecItem } from '../api/endpoints/lenses'
import { TaskWaitTimeoutError, waitForTerminal } from '../api/endpoints/tasks'
import {
  PRECISION_LABEL,
  asPrecision,
  edgeSymbol,
  nodeSymbol,
} from '../domain/canvas-symbol'
import {
  KIND_LABELS,
  type CanvasDoc,
  type CanvasEdge,
  type CanvasNode,
  type CaseCanvasLensGroup,
  type CaseCanvasLensLayer,
} from '../domain/canvas'
import {
  allRevealedKeys,
  groupRowsByTarget,
  readRevealedGroups,
  revealKey,
  toggleRevealedGroup,
  unrevealedCountByTarget,
  withRevealedGroups,
} from '../domain/case-growth'
import NodeWindow from '../components/research/NodeWindow.vue'
import SubjectPicker from '../components/research/SubjectPicker.vue'
import ItemPicker from '../components/research/ItemPicker.vue'
import LensRunModal from '../components/research/LensRunModal.vue'

// 案件 id 取自案件 store（与 ConvergenceView / GeoMapView 同范式），
// 不走路由 props——研判画布是案件级页面，案件切换由顶部选择器统一驱动。
const cs = useCaseStore()
const caseId = computed(() => cs.currentCaseId)

const router = useRouter()
const message = useMessage()

const loading = ref(false)
const loadErr = ref('')
const saving = ref(false)
const doc = ref<CanvasDoc & { meta?: Record<string, unknown> | null }>(
  { nodes: [], edges: [] })
const version = ref<number | null>(null)
const dirty = ref(false)

// ---- 渐进式揭示（v3 §6）---------------------------------------------
// 打开画布只见人工层；lens_layer.groups 始终全量枚举（清单），但只有
// doc.meta.revealed_groups 里的组才会被后端重建进 doc。揭示/隐藏 =
// 改这份 meta → PATCH 持久 → GET 重取，图上只长/消对应那一组。
const lensLayer = ref<CaseCanvasLensLayer | null>(null)
const lensGroups = ref<CaseCanvasLensGroup[]>([])
const groupSections = computed(() => groupRowsByTarget(lensGroups.value))
const revealedKeys = computed(() =>
  readRevealedGroups(doc.value.meta ?? null))
const revealedTotal = computed(() => lensGroups.value
  .filter((g) => g.revealed).length)
const unrevealedMap = computed(() =>
  unrevealedCountByTarget(lensGroups.value))
const revealBusy = ref(false)

const pickerShow = ref(false)

// ---- 节点窗口（WIN-01/02）------------------------------------------
const winNodeId = ref<string | null>(null)
/** 画布选中节点：镜头靶心的来源（CAN-15 选中即靶心） */
const selectedId = ref<string | null>(null)
const winAnchor = ref<{ x: number; y: number }>({ x: 0, y: 0 })
const winViewport = ref({ width: 960, height: 600 })
const winSupports = ref<Array<{ obs_id?: string; label?: string; precision?: string; dim?: string }>>([])
const winLoading = ref(false)
const winErr = ref('')

const winNode = computed<CanvasNode | null>(
  () => doc.value.nodes.find((n) => n.id === winNodeId.value) ?? null,
)
const nodeById = computed(() => new Map(doc.value.nodes.map((n) => [n.id, n])))

/** 节点显示名：顶层 label 与 props 取名链都要认，最后才回退类型中文名。
 *  缺顶层 label 这一级时，growth 重建层结论节点（名字只在顶层）会被
 *  显示成裸 kind "analysis_result"。 */
function nodeLabelOf(n: CanvasNode | undefined): string {
  if (!n) return ''
  const p = (n.props ?? {}) as Record<string, unknown>
  const raw = String(n.label ?? p.label ?? p.name ?? p.title ?? '').trim()
  if (raw) return raw
  return KIND_LABELS[n.kind] ?? String(n.kind ?? '')
}

// ---- 定向镜头调度（CAN-15/16：选中即靶心）--------------------------
const lrShow = ref(false)
const lrBusy = ref(false)
const lrLenses = ref<LensSpecItem[]>([])
const lrRecommendations = ref<any[]>([])

/**
 * 靶心不可用必须**先说清楚**（CAN-20）：静默留空会被读成"可以手填"，
 * 于是正兵填个名字跑一遍得到一个空结果，还以为是数据里没有。
 */
const lrPrefillBlocked = computed<string>(() => {
  const id = selectedId.value
  if (!id) return ''
  const p = (nodeById.value.get(id)?.props ?? {}) as Record<string, unknown>
  const nm = nodeLabelOf(nodeById.value.get(id))
  if (p.person_pk_ambiguous === true) {
    const c = Array.isArray(p.pk_candidates) ? p.pk_candidates.length : 0
    return `「${nm}」同名异人（${c} 个候选主键），须先裁决主体再研判；系统不代为选择`
  }
  if (!p.person_pk) {
    return (String(p.pk_resolution ?? '').trim()
      || `「${nm}」未锚定语义层主键，无可用研判`)
  }
  return ''
})

const lrPrefill = computed<Record<string, unknown>>(() => {
  if (lrPrefillBlocked.value) return {}
  const id = selectedId.value
  if (!id) return {}
  const p = (nodeById.value.get(id)?.props ?? {}) as Record<string, unknown>
  const pk = typeof p.person_pk === 'string' ? p.person_pk : ''
  if (!pk) return {}
  // 只填主靶心：同时填 subject_a 会让双主体镜头变成"自己与自己同框"
  return { target_subject: pk }
})

/** 画布可见主体名（候选规模主力）；object/subject 才算主体 */
const lrCanvasNodes = computed<string[]>(() => {
  const out: string[] = []
  for (const n of doc.value.nodes) {
    if (n.kind !== 'object' && n.kind !== 'subject') continue
    const nm = nodeLabelOf(n)
    if (nm && !out.includes(nm)) out.push(nm)
    if (out.length >= 100) break
  }
  return out
})

const lrSelectedNode = computed<string | null>(() => {
  const id = selectedId.value
  return id ? nodeLabelOf(nodeById.value.get(id)) || null : null
})

async function openLensRun(): Promise<void> {
  try {
    const r = await lensesApi.list(caseId.value)
    // 看的是 canvas_enabled（不是 enabled）：批量自动跑与正兵手动带参跑
    // 是两个开关——自动跑关了仍可在画布手动跑。
    lrLenses.value = (r.lenses ?? []).filter(
      (l) => l.requires_params && l.canvas_enabled
        && l.pack_enabled && l.mode === 'deterministic')
  } catch (e) {
    message.error(String((e as Error)?.message ?? e))
    lrLenses.value = []
  }
  if (!lrLenses.value.length) {
    message.warning('当前案件没有可在画布上调度的定向镜头（需 requires_params 且画布化已启用）')
  }
  if (lrPrefillBlocked.value) {
    message.warning(lrPrefillBlocked.value, { duration: 6000 })
  }
  lrShow.value = true
}

async function submitLensRun(payload: {
  skill_id: string
  params: Record<string, unknown>
}): Promise<void> {
  lrBusy.value = true
  // 结果要挂在**发起它的靶心**下（CAN-19），先记下当前选中节点
  const targetId = selectedId.value || undefined
  try {
    // 案件级画布**没有发起线索**，所以 origin 只带 node_id（不带 clue_id）。
    // 后端据此把观察记上发起节点，重建层才能把结论挂回这个节点下；
    // 若带空 clue_id，后端会把来源整个丢弃 → 跑完图上什么都不出现。
    const r = await lensesApi.run(caseId.value, payload.skill_id, {
      params: payload.params,
      auto: false,
      origin: {
        node_id: targetId,
        subject: lrSelectedNode.value || undefined,
        surface: 'case-canvas',
      },
    })
    lrShow.value = false
    // 202 只代表入队；必须等终态再读，否则读的是旧档案、图上挂不出新结论
    try {
      const t = await waitForTerminal(r.task.id, {
        intervalMs: 500, timeoutMs: 30_000,
      })
      if (t.status !== 'SUCCEEDED') {
        message.error(`定向镜头未成功（${t.status}）：`
          + `${t.error_message || t.error_code || '无错误详情'}`, { duration: 6000 })
        return
      }
    } catch (err) {
      if (err instanceof TaskWaitTimeoutError) {
        // 超时不是失败：任务仍在跑，给正兵一条可自行完成的路径
        message.info('镜头仍在运行，完成后在「研判结果」面板揭示该组', { duration: 6000 })
        return
      }
      message.error(String((err as Error)?.message ?? err))
      return
    }
    // 渐进式生成：跑成功只揭示这一组，别把档案里其他组一起长出来。
    // 揭示集持久后 GET 才会重建对应结论节点（target 取自选中靶心）。
    if (targetId) {
      await enqueueReveal(
        toggleRevealedGroup(revealedKeys.value, targetId, payload.skill_id, true),
      )
    } else {
      await load()
    }
    message.success('研判完成，结果已挂到发起节点下')
  } catch (e) {
    message.error(String((e as Error)?.message ?? e))
  } finally {
    lrBusy.value = false
  }
}

// ---- G6 实例 --------------------------------------------------------
const containerEl = ref<HTMLDivElement | null>(null)
const g6Failed = ref(false)
let inst: any = null
type G6Instance = {
  render?: () => Promise<void> | void
  destroy?: () => void
  setData?: (d: unknown) => void
  getViewportByCanvas?: (p: [number, number]) => [number, number]
  getElementPosition?: (id: string) => [number, number] | undefined
  on?: (evt: string, cb: (e: unknown) => void) => void
}
let GraphCtor: (new (cfg: unknown) => G6Instance) | null = null

/** 节点视觉：符号口径统一取 domain，页面不自带一套颜色/线型 */
function toNodeDatum(n: CanvasNode) {
  const p = (n.props ?? {}) as Record<string, unknown>
  const s = nodeSymbol({
    ambiguous: p.person_pk_ambiguous === true,
    precision: String(p.precision ?? ''),
    unanchored: p.person_pk == null && n.kind === 'subject',
  })
  const baseLabel = nodeLabelOf(n)
  // 靶心节点未揭示组数角标：G6 单标签，用紧凑文本后缀（面板有完整清单）
  const pending = unrevealedMap.value.get(n.id) ?? 0
  const badge = pending > 0 ? ` ·${pending}组` : ''
  const label = (p.person_pk_ambiguous === true ? '? ' : '') + baseLabel + badge
  return {
    id: n.id,
    data: {
      label,
      // nodeSymbol 只给精度色点/线型（dimDotColor/lineDash）；图节点底色
      // 维持统一蓝，描边透明——色点语义在卡片/窗口里表达，不在总图重描。
      color: '#6e9fc1',
      stroke: 'transparent',
      lineWidth: 0,
      lineDash: s.lineDash.length ? s.lineDash : undefined,
      size: n.kind === 'hypothesis' ? 34 : 26,
      raw: n,
    },
    style: { x: n.x ?? undefined, y: n.y ?? undefined },
  }
}

function toEdgeDatum(e: CanvasEdge) {
  // 重建边把 precision 放 props（类型层未声明，按结构读取）
  const p = ((e as CanvasEdge & { props?: Record<string, unknown> }).props ?? {})
  const s = edgeSymbol({ precision: asPrecision(String(p.precision ?? '')) })
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    data: {
      color: s.stroke,
      width: s.lineWidth,
      dashed: (s.lineDash?.length ?? 0) > 0,
      label: String(p.rel ?? e.rel ?? ''),
    },
  }
}

function toDatum() {
  const ids = new Set(doc.value.nodes.map((n) => n.id))
  return {
    nodes: doc.value.nodes.map(toNodeDatum),
    // 边引用的节点必须存在，否则 G6 渲染失败；镜头层节点被过滤时要连带滤边
    edges: doc.value.edges
      .filter((e) => ids.has(e.source) && ids.has(e.target))
      .map(toEdgeDatum),
  }
}

async function initGraph(): Promise<void> {
  if (!containerEl.value) return
  try {
    const mod: any = await import('@antv/g6')
    GraphCtor = mod.Graph as unknown as new (cfg: unknown) => G6Instance
  } catch {
    g6Failed.value = true
    return
  }
  try {
    inst = new GraphCtor!({
      container: containerEl.value,
      autoResize: true,
      autoFit: 'view',
      padding: 24,
      data: toDatum(),
      layout: { type: 'force', preventOverlap: true, nodeSize: 30 },
      node: {
        style: {
          size: (d: any) => d.data?.size ?? 26,
          fill: (d: any) => d.data?.color ?? '#6e9fc1',
          stroke: (d: any) => d.data?.stroke ?? 'transparent',
          lineWidth: (d: any) => d.data?.lineWidth ?? 0,
          lineDash: (d: any) => d.data?.lineDash,
          labelText: (d: any) => d.data?.label ?? '',
          labelFill: '#e8eef4',
          labelFontSize: 11,
          labelPlacement: 'bottom',
        },
      },
      edge: {
        style: {
          stroke: (d: any) => d.data?.color ?? '#3a5a72',
          lineWidth: (d: any) => d.data?.width ?? 1,
          lineDash: (d: any) => (d.data?.dashed ? [4, 3] : undefined),
          endArrow: true,
          labelText: (d: any) => d.data?.label ?? '',
          labelFill: '#8aa5b8',
          labelFontSize: 10,
        },
      },
      behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
    })
    inst.on?.('node:click', onNodeClick)
    inst.on?.('node:dragend', onNodeDragEnd)
    await inst.render?.()
  } catch {
    g6Failed.value = true
  }
}

/**
 * G6 render 串行守卫：load()/揭示链/拖拽摆位可能在一个 render() 的 Promise
 * 还没结束时再次 setData()+render()，旧渲染绘制已被移除的元素会抛
 * "Node not found for id …"。每次刷新捕获自己的数据并排队，前一次
 * render 落定后再画最新一版。
 */
let renderChain: Promise<void> = Promise.resolve()
function refreshGraph(): void {
  const datum = toDatum()
  renderChain = renderChain.then(async () => {
    try {
      inst?.setData?.(datum)
      await inst?.render?.()
    } catch {
      g6Failed.value = true
    }
  })
}

// ---- 加载与保存 -----------------------------------------------------
async function load(): Promise<void> {
  loading.value = true
  loadErr.value = ''
  try {
    const env = await canvasApi.getCaseCanvas(caseId.value)
    doc.value = env.doc ?? { nodes: [], edges: [] }
    version.value = env.version ?? null
    // lens_layer 是只读清单（全量枚举所有组 + revealed 标志），不进 doc
    lensLayer.value = env.lens_layer ?? null
    lensGroups.value = env.lens_layer?.groups ?? []
    dirty.value = false
    refreshGraph()
  } catch (e) {
    loadErr.value = `画布读取失败：${String((e as Error)?.message ?? e)}`
  } finally {
    loading.value = false
  }
}

async function save(opts: { quiet?: boolean } = {}): Promise<boolean> {
  saving.value = true
  try {
    const r = await canvasApi.saveCaseCanvas(caseId.value, doc.value, version.value, true)
    // 返回不含 doc——只前进 version，绝不用它覆盖本地（会把图抹成空）
    if (typeof r?.version === 'number') version.value = r.version
    dirty.value = false
    if (!opts.quiet) message.success('已保存')
    return true
  } catch (e) {
    message.error(`保存失败：${String((e as Error)?.message ?? e)}`)
    return false
  } finally {
    saving.value = false
  }
}

/**
 * 揭示集写操作：串行 + 飞行中合并 + 最新意图胜出。
 * - 连点不逐个发 PATCH：保存排空循环（drainRevealQueue）每轮都取
 *   desiredReveal 最新集合；只有保存期间又来了新意图才追加一轮。
 * - 意图基线取 desiredReveal（本地最新意图），不取服务端回显——load()
 *   完成前到达的点击若基于回显集合计算，会丢掉飞行中的修改。
 * - 行点击一律按本地最新意图"取反"（见 onFlipGroup），不采信受控
 *   checkbox 在回显翻转瞬间给出的事件布尔。
 * 保存失败先 GET 刷新版本，再带最新集合重试一次。
 */
let revealChain: Promise<void> = Promise.resolve()
let revealDraining = false
const desiredReveal = ref<Set<string> | null>(null)

function enqueueReveal(keys: Set<string>): Promise<void> {
  desiredReveal.value = keys
  // 当帧同步反映到文档：连点的视觉状态与后续意图基线都立即更新
  doc.value = withRevealedGroups(doc.value, keys)
  if (!revealDraining) {
    revealDraining = true
    revealChain = revealChain.then(drainRevealQueue)
  }
  return revealChain
}

async function drainRevealQueue(): Promise<void> {
  revealBusy.value = true
  try {
    for (;;) {
      const latest = desiredReveal.value
      if (!latest) break
      doc.value = withRevealedGroups(doc.value, latest)
      let ok = await save({ quiet: true })
      if (!ok) {
        await load()
        doc.value = withRevealedGroups(doc.value, latest)
        ok = await save({ quiet: true })
      }
      if (!ok) break
      await load()
      // 保存期间没有更新的意图：回落到服务端回显作为后续基线，
      // 避免他人并发修改被本地陈旧意图长期盖住
      if (desiredReveal.value === latest) {
        desiredReveal.value = null
        break
      }
    }
  } finally {
    revealBusy.value = false
    revealDraining = false
  }
}

async function onFlipGroup(g: CaseCanvasLensGroup): Promise<void> {
  // 不采信 update:checked 的布尔：保存链中的 load() 回显可能在点击瞬间把
  // 受控 checkbox 的视觉状态翻回旧值，事件布尔会与用户真实意图相反。
  // 一次行点击的语义就是"相对本地最新意图取反"。
  const base = desiredReveal.value ?? revealedKeys.value
  const on = !base.has(revealKey(g.target_node_id, g.lens_id))
  await enqueueReveal(
    toggleRevealedGroup(base, g.target_node_id, g.lens_id, on))
}

async function onRevealAll(): Promise<void> {
  await enqueueReveal(allRevealedKeys(lensGroups.value))
}

function isGroupRevealed(g: CaseCanvasLensGroup): boolean {
  return revealedKeys.value.has(revealKey(g.target_node_id, g.lens_id))
}

function precisionLabel(p?: string | null): string {
  if (!p) return ''
  return PRECISION_LABEL[asPrecision(String(p))] ?? String(p)
}

// ---- 拖拽落位：坐标回写 doc（重建层坐标由服务端收割进 meta.lens_layout）----
let dragSaveTimer: ReturnType<typeof setTimeout> | null = null

function onNodeDragEnd(evt: unknown): void {
  const id = String((evt as any)?.target?.id ?? (evt as any)?.data?.id ?? '')
  const node = doc.value.nodes.find((n) => n.id === id)
  if (!node) return
  const pos = inst?.getElementPosition?.(id)
  if (!pos || (!Number.isFinite(pos[0]) && !Number.isFinite(pos[1]))) return
  node.x = pos[0]
  node.y = pos[1]
  dirty.value = true
  // 防抖静默自动保存：拖动摆位是高频动作，不逐条弹「已保存」。
  // 排在揭示写链之后——与逐组揭示的 PATCH 并发会同基准版本撞 409。
  if (dragSaveTimer) clearTimeout(dragSaveTimer)
  dragSaveTimer = setTimeout(() => {
    dragSaveTimer = null
    void revealChain.then(() => save({ quiet: true }))
  }, 1200)
}

// ---- 节点点击：开专属窗口，其余走抽屉 -------------------------------
async function onNodeClick(evt: unknown): Promise<void> {
  const id = String((evt as any)?.target?.id ?? (evt as any)?.data?.id ?? '')
  if (!id) return
  const n = nodeById.value.get(id)
  if (!n) return
  // 选中即靶心（CAN-15）：先记下选中节点，开镜头面板时据此预填
  selectedId.value = id
  const pt = inst?.getViewportByCanvas?.([n.x ?? 0, n.y ?? 0]) ?? [0, 0]
  const box = containerEl.value?.getBoundingClientRect()
  winViewport.value = {
    width: box?.width ?? 960,
    height: box?.height ?? 600,
  }
  winAnchor.value = { x: Number(pt[0]) || 0, y: Number(pt[1]) || 0 }
  winNodeId.value = id
  winSupports.value = []
  winErr.value = ''
  // 只有研判结论需要回查观察档案；关系/地图/时间三窗口读画布内存数据。
  // 此前案件画布没接这个请求，证据窗口三维与支撑观察永远为空。
  if (n.kind === 'analysis_result') {
    winLoading.value = true
    try {
      const res = await canvasApi.nodeWindow(
        caseId.value, n as unknown as Record<string, unknown>)
      // 切换/关窗后返回的旧响应不得覆盖当前窗口
      if (winNodeId.value !== id) return
      winSupports.value = res.supports ?? []
      // 定位不到 ≠ 没有支撑，必须把原因说出来，不能静默空列表。
      if (!res.server_sourced) winErr.value = res.note ?? ''
      else if (res.match?.mode === 'none') winErr.value = res.match.reason
    } catch (e) {
      if (winNodeId.value !== id) return
      winErr.value = `支撑观察加载失败：${String((e as Error)?.message ?? e)}`
    } finally {
      if (winNodeId.value === id) winLoading.value = false
    }
  }
}

async function closeWindow(): Promise<void> {
  winNodeId.value = null
  winSupports.value = []
  winErr.value = ''
  winLoading.value = false
}

/** 跳原始档案（WIN-10）：先关浮层，否则窗口会悬空盖在详情页上 */
async function jumpToObservation(obsId: string): Promise<void> {
  await closeWindow()
  void router.push(`/c/observations/${encodeURIComponent(obsId)}`)
}

// ---- 加主体（CAN-13/14）---------------------------------------------
function onSubjectSubmit(payload: { props: Record<string, unknown>; sel: unknown }): void {
  const p = payload.props ?? {}
  const name = String(p.name ?? p.label ?? '')
  // id 必须走 case# 命名空间（与后端 case_node_id / 镜头层 target_node_id 对齐）：
  // person_pk 是裸语义键（person_xxx），直接拿它当画布 id 会让"靶心已在画布"
  // 永远判否、揭示组连向靶心的边全被悬空边防御剔除。
  const pk = String(p.person_pk ?? p.ref ?? '')
  const id = pk
    ? `case#${caseId.value}:subject:${pk}`
    : `case#${caseId.value}:subject:manual:${name || Date.now()}`
  if (doc.value.nodes.some((n) => n.id === id)) {
    pickerShow.value = false
    message.warning('该主体已在图上')
    return
  }
  doc.value.nodes.push({
    id,
    kind: 'subject',
    props: p,
    x: undefined,
    y: undefined,
  } as unknown as CanvasNode)
  pickerShow.value = false
  dirty.value = true
  refreshGraph()
  message.success('已加入画布')
}

// ---- 加物品（ITM 画布层）-------------------------------------------
const itemShow = ref(false)

/**
 * 节点由服务端构造后回传（id 含摘要，前端算就会有两份规则）。
 * 判重按 id：同一凭证重复登记时提示"已在图上"，不静默建第二个节点——
 * 两个同 id 节点会让持有链出现分叉，而界面上看不出是同一件物品。
 */
function onItemSubmit(node: Record<string, unknown>): void {
  const id = String((node as { id?: unknown }).id ?? '')
  if (!id) {
    message.error('服务端未返回节点 id，未加入画布')
    return
  }
  if (doc.value.nodes.some((n) => n.id === id)) {
    message.warning('该物品已在图上')
    return
  }
  doc.value.nodes.push(node as unknown as CanvasNode)
  dirty.value = true
  refreshGraph()
  message.success('已加入画布')
}

onMounted(async () => {
  await load()
  await initGraph()
})
onBeforeUnmount(() => {
  if (dragSaveTimer) clearTimeout(dragSaveTimer)
  try {
    inst?.destroy?.()
  } catch {
    /* 组件卸载时实例可能已失效 */
  }
  inst = null
})

const hypothesisCount = computed(
  () => doc.value.nodes.filter((n) => n.kind === 'hypothesis').length,
)
</script>

<template>
  <div class="canvas-view">
    <div class="bar">
      <div class="bar-left">
        <span class="title">研判层画布</span>
        <NTag size="small" :bordered="false">节点 {{ doc.nodes.length }}</NTag>
        <NTag size="small" :bordered="false">连线 {{ doc.edges.length }}</NTag>
        <NTag size="small" :bordered="false">假设 {{ hypothesisCount }}</NTag>
        <NTag size="small" :bordered="false">
          已揭示 {{ revealedTotal }}/{{ lensGroups.length }} 组
        </NTag>
        <NTag v-if="lensLayer?.edges_dropped" size="small" type="warning" :bordered="false">
          {{ lensLayer.edges_dropped }} 条挂边悬空
        </NTag>
        <NTag v-if="dirty" size="small" type="warning" :bordered="false">未保存</NTag>
      </div>
      <div class="bar-right">
        <NButton size="small" @click="pickerShow = true">加主体</NButton>
        <NButton size="small" @click="itemShow = true">加物品</NButton>
        <NButton size="small" @click="openLensRun">研判</NButton>
        <NButton size="small" :loading="loading" @click="load">重建</NButton>
        <NButton size="small" type="primary" :loading="saving" @click="save()">保存</NButton>
      </div>
    </div>

    <NAlert v-if="loadErr" type="error" :bordered="false" class="alert">{{ loadErr }}</NAlert>

    <div class="stage">
      <div v-if="loading" class="center"><NSpin /></div>
      <div v-else-if="g6Failed" class="center">
        <NAlert type="warning" :bordered="false">
          图渲染不可用，已降级为结构化列表（不影响数据）。
        </NAlert>
        <ul class="fallback">
          <li v-for="n in doc.nodes" :key="n.id">
            {{ nodeLabelOf(n) }}
            <span v-if="(n.props as any)?.precision" class="dim">
              · {{ PRECISION_LABEL[asPrecision(String((n.props as any).precision))] }}
            </span>
          </li>
        </ul>
      </div>
      <div v-else-if="!doc.nodes.length" class="center empty-guide">
        <template v-if="lensGroups.length">
          <p class="empty-title">研判画布还是空的</p>
          <p class="empty-sub">
            已有 {{ lensGroups.length }} 组镜头结果尚未揭示——全部显示会把结论与假设长到图上；
            靶心主体也可先「加主体」提升。
          </p>
          <div class="empty-actions">
            <NButton size="small" type="primary" :loading="revealBusy"
              :disabled="revealBusy" @click="onRevealAll">
              全部显示（{{ lensGroups.length }} 组）
            </NButton>
            <NButton size="small" @click="pickerShow = true">加主体</NButton>
          </div>
        </template>
        <NEmpty
          v-else
          description="画布为空：先「加主体」，或对主体跑定向镜头"
        />
      </div>
      <div v-show="!loading" ref="containerEl" class="g6" />

      <!-- 研判结果清单：全量枚举可生长组，逐组揭示（v3 渐进式生成） -->
      <div v-if="lensGroups.length" class="growth-panel">
        <div class="growth-head">
          <span>研判结果</span>
          <span class="growth-count">{{ revealedTotal }}/{{ lensGroups.length }}</span>
        </div>
        <div class="growth-body">
          <div v-for="sec in groupSections" :key="sec.target" class="growth-sec">
            <div class="growth-target" :title="sec.target">
              <span class="growth-target-name">{{ sec.targetLabel }}</span>
              <NTag v-if="!sec.rows.some(isGroupRevealed) && sec.rows.every(r => !r.on_canvas)"
                size="tiny" type="warning" :bordered="false">靶心未在画布</NTag>
            </div>
            <!-- 用 div 不用 label：label 包 NCheckbox 时点击会被原生转发与组件
                 各触发一次，update:checked 成对(true,false)抵消，快速连点会丢组 -->
            <div v-for="g in sec.rows" :key="`${g.target_node_id}|${g.lens_id}`"
              class="growth-row">
              <!-- 保存中不禁用：disabled 会直接吞掉连点意图；写入已由
                   revealChain 串行 + desiredReveal 最新意图合并保证一致 -->
              <NCheckbox
                :checked="isGroupRevealed(g)"
                @update:checked="() => onFlipGroup(g)"
              />
              <span class="growth-row-main">
                <span class="growth-row-name">{{ g.lens_name || g.lens_id }}</span>
                <span class="growth-row-meta">
                  {{ g.observation_count }} 条观察
                  <template v-if="precisionLabel(g.precision)">
                    · {{ precisionLabel(g.precision) }}
                  </template>
                </span>
                <span v-if="g.assumption" class="growth-row-hyp">{{ g.assumption }}</span>
                <span v-if="!g.on_canvas" class="growth-row-warn">挂边待靶心提升</span>
              </span>
            </div>
          </div>
        </div>
        <div class="growth-foot">
          <NButton size="tiny" block
            :disabled="revealedTotal === lensGroups.length || revealBusy"
            :loading="revealBusy" @click="onRevealAll">
            全部显示
          </NButton>
        </div>
      </div>

      <NodeWindow
        v-if="winNode"
        :node="winNode as any"
        :anchor="winAnchor"
        :viewport="winViewport"
        :edges="doc.edges as any"
        :node-by-id="nodeById as any"
        :supports="winSupports"
        :loading="winLoading"
        :error="winErr"
        @close="closeWindow"
        @open-node="(id: string) => onNodeClick({ target: { id } })"
        @jump-observation="jumpToObservation"
      />
    </div>

    <SubjectPicker
      v-model:show="pickerShow"
      :case-id="caseId"
      @submit="onSubjectSubmit"
    />

    <ItemPicker v-model:show="itemShow" :case-id="caseId" @submit="onItemSubmit" />

    <!-- 定向镜头带参调度（选中主体预填；结果挂回发起节点下） -->
    <LensRunModal
      v-model:show="lrShow"
      :lenses="lrLenses"
      :busy="lrBusy"
      :prefill="lrPrefill"
      :case-id="caseId"
      :canvas-nodes="lrCanvasNodes"
      :selected-node="lrSelectedNode"
      :recommendations="lrRecommendations"
      @submit="submitLensRun"
    />
  </div>
</template>

<style scoped>
.canvas-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 480px;
}
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.bar-left,
.bar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title {
  font-size: 14px;
  font-weight: 600;
  color: #e8eef4;
}
.alert {
  margin: 8px 14px;
}
.stage {
  position: relative;
  flex: 1;
  min-height: 420px;
}
.g6 {
  width: 100%;
  height: 100%;
}
.center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 10px;
}
.fallback {
  max-height: 320px;
  overflow: auto;
  color: #c9d6e0;
  font-size: 12px;
}
.dim {
  color: #8aa5b8;
}

/* ---- 渐进式揭示：空态引导 ---- */
.empty-guide {
  gap: 8px;
  padding: 0 24px;
  text-align: center;
}
.empty-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: #e8eef4;
}
.empty-sub {
  margin: 0;
  max-width: 420px;
  font-size: 12px;
  line-height: 1.7;
  color: #8aa5b8;
}
.empty-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}

/* ---- 研判结果清单（右侧浮层） ---- */
.growth-panel {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 5;
  display: flex;
  flex-direction: column;
  width: 264px;
  max-height: calc(100% - 24px);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  background: rgba(16, 24, 32, 0.92);
  box-shadow: 0 6px 22px rgba(0, 0, 0, 0.35);
  font-size: 12px;
}
.growth-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  font-weight: 600;
  color: #e8eef4;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.growth-count {
  font-weight: 400;
  color: #8aa5b8;
}
.growth-body {
  overflow: auto;
  padding: 4px 0;
}
.growth-sec + .growth-sec {
  border-top: 1px dashed rgba(255, 255, 255, 0.07);
}
.growth-target {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px 2px;
  color: #b8c8d4;
}
.growth-target-name {
  overflow: hidden;
  max-width: 180px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.growth-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 5px 10px;
  cursor: pointer;
}
.growth-row:hover {
  background: rgba(255, 255, 255, 0.04);
}
.growth-row-main {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 8px;
  align-items: center;
  min-width: 0;
}
.growth-row-name {
  width: 100%;
  color: #dce6ee;
}
.growth-row-meta {
  color: #8aa5b8;
}
.growth-row-hyp {
  padding: 0 6px;
  border-radius: 4px;
  background: rgba(110, 159, 193, 0.22);
  color: #9fc7e0;
}
.growth-row-warn {
  color: #e0b36b;
}
.growth-foot {
  padding: 8px 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
</style>
