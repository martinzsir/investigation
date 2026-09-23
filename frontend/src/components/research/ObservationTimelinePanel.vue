<script setup lang="ts">
/**
 * 观察图层时间轴面板（无遮罩全宽浮层）。
 *
 * 把后端 observation_layer 的节点转成 vue-timeline-chart 的 items/groups，
 * 按 obs_of（观察源）分泳道，事件点→point、区间→range、无时间字段不发 item。
 *
 * 交互模型：
 * - 不用 NDrawer：NDrawer 的全屏 mask 会拦截鼠标，导致画布其余区域不可操作。
 *   本面板只是一个 position:fixed 的普通元素，**只有面板自身矩形吃事件**，
 *   鼠标在面板外（未被遮挡的画布区）直接操作 G6 画布，无需关闭时间轴。
 * - 面板始终**全宽贴底**（left/right/bottom 均为 0），无锁定/拖动概念。
 * - 视口导航（set-viewport 范式）：标题栏 左移/缩小/全览/放大/右移；
 *   单击 item 钻取聚焦（point±7天、range±3天），双击打开观察档案。
 * - stacking 开启：数据精度只到天，同日事件经 collisionWidth 像素占位
 *   自动分到不同泳道；「×N」徽标按业务语义自算（同组同一时刻的真实项数，
 *   挂代表项），不用库 slot 的 stackSize（那是随缩放变化的泳道占用数）。
 * - 单日区间（start==end 的 burst）在显示层补宽 1 天，避免零宽不可见。
 * - process 口径同观察节点共享 created_at，按「观察+时刻」聚合成单点。
 *
 * 与主画布完全分离：不并入 graphDoc、不算 G6 坐标，开关仅控制本浮层显隐，
 * 时间口径也是面板自有，不跟随主画布 perspective/timeMode。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NEmpty, NTag, NIcon } from 'naive-ui'
import {
  TimeOutline,
  CloseOutline,
  ChevronBackOutline,
  ChevronForwardOutline,
  ExpandOutline,
  AddCircleOutline,
  RemoveCircleOutline,
} from '@vicons/ionicons5'
import { Timeline } from 'vue-timeline-chart'
import 'vue-timeline-chart/style.css'
import {
  buildTimeAxis,
  intervalRange,
  nodeTimestamp,
  type IntervalBandKind,
  type TimeMode,
} from '../../domain/canvas-layout-time'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'
import type { CanvasNode } from '../../domain/canvas'
import { presetOfSkill } from '../../domain/lensPresets'

const props = withDefaults(
  defineProps<{
    show: boolean
    nodes: CanvasNode[]
    observationCount?: number
  }>(),
  { observationCount: 0 },
)

const PANEL_HEIGHT = 340
const HEADER_HEIGHT = 44
const DAY_MS = 86_400_000

/**
 * 堆叠配置：观察数据时间精度只到「天」，同日事件时间戳完全相同，
 * collisionWidth 给每个点一个像素级最小占位（换算成 ms 后），
 * 使同日事件落入不同泳道；maxLanes 4 封顶（全览时隔日近邻也会堆叠，
 * 放大后自然恢复精细）；dataset 策略泳道按全量数据计算，高度稳定。
 */
const stackingOptions = {
  enabled: true,
  strategy: 'dataset' as const,
  maxLanes: 4,
  collisionWidth: 16,
}

/**
 * 面板自有的时间口径，**不跟随主画布** perspective/timeMode。
 * 打开观察图层是唤出独立时间轴，不应把主画布拖进时间视角，
 * 也不应依赖主画布是否处于时间视角才能切口径。
 * 默认 process（研判过程时间，所有 obs 节点都有 created_at，必有数据）。
 */
const mode = ref<TimeMode>('process')
const { hasEventTime } = useCaseOntologyConfig()
/** 业务时间口径需本体声明 semantic:event_time，否则禁用切换 */
const canUseEventTime = computed(() => hasEventTime.value)
const modeLabel = computed(() => (mode.value === 'event' ? '业务时间' : '过程时间'))

function toggleMode(): void {
  if (!canUseEventTime.value) return
  mode.value = mode.value === 'event' ? 'process' : 'event'
  // Timeline 以 :key=mode 重建并回到全览，旧视口坐标必须作废，
  // 否则随后按按钮平移/缩放会用另一口径的时间戳算目标区间
  currentViewport.value = null
  // 两口径 item id 命名空间不同（proc: 聚合点 vs 节点 id），锚点一律作废
  focusedId.value = null
}

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  /** 点击某条观察 item：observation_id（= item.group） */
  (e: 'pick-observation', observationId: string): void
}>()

interface TItem {
  id: string
  group: string
  type: 'point' | 'range'
  start: number
  end?: number
  cssVariables?: Record<string, string>
  title?: string
  /** burst 链式簇：相邻间隔均 ≤ 天窗但首末跨度超天窗 */
  chain?: boolean
  /** range 细分种类（徽标计数键防 burst/collision 同日跨类合并） */
  intervalKind?: IntervalBandKind
}
interface TGroup {
  id: string
  label: string
  /** 完整观察标题（中文），泳道头悬浮展示；缺省回退 label */
  fullTitle?: string
  /** 组级节奏摘要，如「中位间隔 23 天 · 4 簇」 */
  metric?: string
}

/** 一条观察的命名元信息（后端 obs_* props 透传；旧产物可能整组缺失） */
interface ObsMeta {
  skillId: string
  lensName: string
  target: string
  obsTitle: string
}

function obsMetaOf(p: Record<string, unknown>): ObsMeta {
  return {
    skillId: String(p.obs_skill_id ?? ''),
    lensName: String(p.obs_lens_name ?? ''),
    target: String(p.obs_target ?? ''),
    obsTitle: String(p.obs_title ?? ''),
  }
}

/** 镜头显示名：本体中文名（去「镜头」后缀）→ 前端预设业务话术 → skill_id → gid */
function lensDisplayName(m: ObsMeta, gid: string): string {
  const short = m.lensName.replace(/镜头$/, '').trim()
  return short || presetOfSkill(m.skillId)?.title || m.skillId || gid
}

/** 泳道名 = 镜头名 · 靶心（同镜头对不同主体的多条观察靠靶心区分） */
function groupLabelOf(m: ObsMeta, gid: string): { label: string; fullTitle: string } {
  const lens = lensDisplayName(m, gid)
  return {
    label: m.target ? `${lens} · ${m.target}` : lens,
    fullTitle: m.obsTitle,
  }
}

/** 间隔天数格式化：整数天不带小数，中位数可能为 .5 */
function fmtDays(v: unknown): string | null {
  const n = Number(v)
  if (!Number.isFinite(n)) return null
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}
/** 库 marker：无 group 时在 .markers 层横贯全部泳道画竖线 */
interface TMarker {
  id: string
  type: 'marker'
  start: number
  className?: string
}

/** 聚焦锚点时间格式化：整日数据省略 00:00，过程时刻保留时分 */
function fmtFocusTime(ts: number): string {
  const d = new Date(ts)
  const p = (n: number): string => String(n).padStart(2, '0')
  const ymd = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`
  return hm === '00:00' ? ymd : `${ymd} ${hm}`
}

/** 区间种类 → 背景色（与 design/tokens 的 band.stroke 对齐） */
function bandColor(kind: 'burst' | 'collision_window'): string {
  return kind === 'burst' ? '#FF7043' : '#6E87B5'
}

/** 节点 → 显示标题（label 优先，回退 ref） */
function nodeTitle(n: CanvasNode): string {
  return String(n.label || n.ref || n.id)
}

/**
 * 转换：CanvasNode[] → timeline items + groups。
 *
 * 口径差异（重要）：
 * - event：每个有 event_time 的事件节点一个 point，区间节点（burst/collision_window）
 *   成 range；无业务时间的主体/项目节点不发 item，其所在观察组也不注册（不出空泳道）。
 * - process：同一次观察（obs_of）的全部节点共享同一个 created_at（镜头运行时刻），
 *   逐节点发 point 会在同一毫秒重叠成一堆。**按「观察+时刻」聚合成 1 个 point**，
 *   徽标显示节点数——过程口径回答的本来就是「何时做过哪次深挖」。
 *
 * 徽标计数（badgeById）：库 slot 给的 stackSize 是「重叠簇占用泳道数」，且随
 * collisionWidth 在不同缩放下变化（全览时 ±8 天占位会把整段密集期连成一条
 * 长链，每个点都挂 ×4），不等于「同一时刻事件数」。这里按业务语义自算：
 * 同组同 start 的 point 数 / 同组同 start 的 range 数，>1 才挂到代表项。
 */
const model = computed(() => {
  const items: TItem[] = []
  const groupMap = new Map<string, TGroup>()
  /** key → 计数；key 与代表 item id */
  const counts = new Map<string, number>()
  const badgeKeys = new Map<string, string>()

  function ensureGroup(gid: string, m: ObsMeta): void {
    if (!groupMap.has(gid)) {
      const { label, fullTitle } = groupLabelOf(m, gid)
      groupMap.set(gid, { id: gid, label, fullTitle: fullTitle || undefined })
    }
  }

  /** 登记一个 item 的同刻计数；返回该项是否为代表项（挂徽标）。
   *  range 键必须带 interval_kind：burst 与 collision_window 同日起算时
   *  不是同一语义的重叠，跨类合并会虚增徽标。 */
  function registerBadge(item: TItem): void {
    const key = item.type === 'point'
      ? `p:${item.group}:${item.start}`
      : `r:${item.intervalKind ?? 'burst'}:${item.group}:${item.start}`
    const c = (counts.get(key) ?? 0) + 1
    counts.set(key, c)
    if (!badgeKeys.has(key)) badgeKeys.set(key, item.id)
  }

  if (mode.value === 'process') {
    // 观察+时刻 → 聚合点
    const agg = new Map<string, {
      id: string
      gid: string
      meta: ObsMeta
      t: number
      count: number
    }>()
    for (const n of props.nodes) {
      const t = nodeTimestamp(n, 'process')
      if (t === null) continue
      const p = (n.props ?? {}) as Record<string, unknown>
      const gid = String(p.obs_of ?? '_default')
      const key = `${gid}|${t}`
      const cur = agg.get(key)
      if (cur) cur.count += 1
      else agg.set(key, {
        id: n.id, gid, meta: obsMetaOf(p), t, count: 1,
      })
    }
    for (const a of agg.values()) {
      ensureGroup(a.gid, a.meta)
      const item: TItem = {
        id: `proc:${a.gid}:${a.t}`,
        group: a.gid,
        type: 'point',
        start: a.t,
        title: `观察运行时刻（${groupLabelOf(a.meta, a.gid).label}，含 ${a.count} 项）`,
      }
      items.push(item)
      // 聚合点的徽标直接显示被合并的节点总数
      counts.set(`p:${a.gid}:${a.t}`, a.count)
      badgeKeys.set(`p:${a.gid}:${a.t}`, item.id)
    }
  } else {
    for (const n of props.nodes) {
      const p = (n.props ?? {}) as Record<string, unknown>
      const gid = String(p.obs_of ?? '_default')
      const meta = obsMetaOf(p)
      // event 口径下区间节点成 range（burst/collision_window）
      const range = intervalRange(n)
      if (range) {
        // 显示层补宽：真实数据里大量 burst start==end（单日聚集簇），
        // 零宽 range 在轴上不可见。补满 1 天仅影响渲染/聚焦，不改后端语义。
        const end = range.end > range.start ? range.end : range.start + DAY_MS
        ensureGroup(gid, meta)
        const isChain = p.chain === true
        const spanDays = fmtDays(p.span_days)
        const maxGapDays = fmtDays(p.max_gap_days)
        const eventTypes = Array.isArray(p.types)
          ? p.types.map((x) => String(x)).filter(Boolean)
          : []
        // 簇带标题（label 已含「聚集簇 N」）：链式语义 + 跨度/最大间隔 + 成员类型
        const detailParts: string[] = []
        if (range.kind === 'burst') {
          if (isChain) detailParts.push('链式：相邻接力成簇，带宽≠天窗宽')
          if (spanDays !== null) detailParts.push(`跨度 ${spanDays} 天`)
          if (maxGapDays !== null) detailParts.push(`最大相邻间隔 ${maxGapDays} 天`)
          if (eventTypes.length > 0) detailParts.push(`类型 ${eventTypes.join('/')}`)
        }
        const title = detailParts.length > 0
          ? `${nodeTitle(n)}｜${detailParts.join(' · ')}`
          : nodeTitle(n)
        const item: TItem = {
          id: n.id,
          group: gid,
          type: 'range',
          start: range.start,
          end,
          cssVariables: { '--item-background': bandColor(range.kind) },
          title,
          chain: isChain,
          intervalKind: range.kind,
        }
        items.push(item)
        registerBadge(item)
        continue
      }
      // 时间戳节点成 point
      const t = nodeTimestamp(n, 'event')
      if (t !== null) {
        ensureGroup(gid, meta)
        const item: TItem = {
          id: n.id,
          group: gid,
          type: 'point',
          start: t,
          title: nodeTitle(n),
        }
        items.push(item)
        registerBadge(item)
      }
      // 无时间字段：不发 item、不注册组（subject/project 普通节点及 0 事件观察）
    }
  }

  // 代表项 id → 徽标数字（仅 >1）
  const badgeById = new Map<string, number>()
  for (const [key, repId] of badgeKeys) {
    const c = counts.get(key) ?? 1
    if (c > 1) badgeById.set(repId, c)
  }
  // 组级节奏摘要：直接从 props.nodes 的 burst 区间节点扫描（与口径无关：
  // process 口径下没有 range item，但摘要仍需显示）。同组各簇指标一致，
  // 取首个非空即可。旧产物/非节奏镜头缺字段 → 不显示摘要。
  const metricByGroup = new Map<string, { median: string | null; count: number | null }>()
  for (const n of props.nodes) {
    const p = (n.props ?? {}) as Record<string, unknown>
    if (p.interval_kind !== 'burst') continue
    const gid = String(p.obs_of ?? '_default')
    if (metricByGroup.has(gid)) continue
    const median = fmtDays(p.median_gap_days)
    const count = Number.isFinite(Number(p.burst_count))
      ? Number(p.burst_count)
      : null
    if (median === null && count === null) continue
    metricByGroup.set(gid, { median, count })
  }
  for (const g of groupMap.values()) {
    const m = metricByGroup.get(g.id)
    if (!m) continue
    const segs: string[] = []
    if (m.median !== null) segs.push(`中位间隔 ${m.median} 天`)
    if (m.count !== null) segs.push(`${m.count} 簇`)
    if (segs.length > 0) g.metric = segs.join(' · ')
  }
  return {
    items,
    groups: Array.from(groupMap.values()),
    hasTimeData: items.length > 0,
    badgeById,
  }
})

const hasTimeData = computed(() => model.value.hasTimeData)

/** 时间轴范围（视口用）。无时间数据时回落「近一天」避免 viewportMin/Max 为 null */
const viewport = computed(() => {
  const axis = buildTimeAxis(
    { nodes: props.nodes, edges: [] },
    mode.value,
  )
  if (axis.from !== null && axis.to !== null) {
    // process 口径同观察所有节点同一毫秒 → from===to，零宽视口 Timeline 无法渲染，
    // 两侧各补 12 小时
    if (axis.to === axis.from) {
      return { min: axis.from - 12 * 3_600_000, max: axis.to + 12 * 3_600_000 }
    }
    return { min: axis.from, max: axis.to }
  }
  const now = Date.now()
  return { min: now - 86_400_000, max: now }
})

/** 时间 item 数（状态栏摘要用，与 observationCount 维度不同） */
const timedCount = computed(() => model.value.items.length)

/** 面板标题摘要 */
const titleSuffix = computed(() => {
  const obs = props.observationCount
  const t = timedCount.value
  return `${obs} 条观察 · ${t} 个时间项 · ${mode.value === 'event' ? '业务时间' : '过程时间'}口径`
})

/* ------------------------------------------------------------------ */
/* 聚焦锚点：单击只平移视口时，视口内各节点外观完全一致，用户无法辨认    */
/* 自己点的是哪一个。这里把被点 item 固化为锚点：                        */
/*  - activeItems 让库给该 item 外壳挂 .active（强高亮描边/放大）；      */
/*  - 无组 marker 在该时刻横贯全部泳道画琥珀色竖线（平移出视口即消失）；  */
/*  - 头部芯片文字说明锚点是谁、可一键 × 清除回全览。                    */
/* ------------------------------------------------------------------ */
const focusedId = ref<string | null>(null)
const focusedItem = computed(
  () => model.value.items.find((i) => i.id === focusedId.value) ?? null,
)
/** 锚点已不在当前数据中（刷新/切口径竞争）时不发 active id */
const activeItemIds = computed<string[]>(() =>
  focusedItem.value ? [focusedItem.value.id] : [],
)
const focusMarkers = computed<TMarker[]>(() => {
  const f = focusedItem.value
  if (!f) return []
  return [{ id: '__otp-focus__', type: 'marker', start: f.start, className: 'otp-focus-marker' }]
})
const focusLabel = computed(() => {
  const f = focusedItem.value
  if (!f) return ''
  return `${fmtFocusTime(f.start)}｜${f.title || '时间项'}`
})
/** 收起面板：Timeline 随 v-if 销毁、重开回到全览，锚点状态同步作废 */
watch(
  () => props.show,
  (v) => {
    if (!v) focusedId.value = null
  },
)

/* ------------------------------------------------------------------ */
/* 视口导航（set-viewport 范式）：全览 → 钻取                           */
/* ------------------------------------------------------------------ */

const timelineRef = ref<InstanceType<typeof Timeline> | null>(null)
/** 当前视口（由 @changeViewport 回传）；null 时按钮以初始全览为准 */
const currentViewport = ref<{ start: number; end: number } | null>(null)

/** 把目标区间夹到 [viewportMin, viewportMax]，并保证最短 1 天、时长不变 */
function clampRange(start: number, end: number): { start: number; end: number } {
  const min = viewport.value.min
  const max = viewport.value.max
  const duration = Math.max(end - start, DAY_MS)
  let s = start
  let e = s + duration
  if (s < min) {
    s = min
    e = s + duration
  }
  if (e > max) {
    e = max
    s = e - duration
  }
  s = Math.max(min, s)
  e = Math.min(max, e)
  if (e <= s) return { start: min, end: max }
  return { start: s, end: e }
}

function applyView(start: number, end: number): void {
  const r = clampRange(start, end)
  timelineRef.value?.setViewport(r.start, r.end)
}

/** 全览：跳回数据完整范围（同时解除聚焦锚点） */
function fitAll(): void {
  focusedId.value = null
  timelineRef.value?.setViewport(viewport.value.min, viewport.value.max)
}

/** 头部芯片 ×：清除锚点并回全览 */
function clearFocus(): void {
  fitAll()
}

/** 缩放：factor>1 放大（视口变短），factor<1 缩小；围绕视口中心 */
function zoomView(factor: number): void {
  const cur = currentViewport.value
    ?? { start: viewport.value.min, end: viewport.value.max }
  const center = (cur.start + cur.end) / 2
  const newDuration = Math.max((cur.end - cur.start) / factor, DAY_MS)
  applyView(center - newDuration / 2, center + newDuration / 2)
}

/** 平移：dir=-1 向左（看更早），+1 向右（看更晚），步长 20% 视口 */
function panView(dir: -1 | 1): void {
  const cur = currentViewport.value
    ?? { start: viewport.value.min, end: viewport.value.max }
  const step = (cur.end - cur.start) * 0.2
  applyView(cur.start + dir * step, cur.end + dir * step)
}

/** 单击 item 聚焦：point → 该日前后各 7 天；range → 区间两侧各留 3 天 */
function onItemFocus(item: TItem): void {
  // 固化锚点：视口跳过去之后靠 .active 高亮 + 竖线 + 头部芯片辨认
  focusedId.value = item.id
  if (item.type === 'point') {
    applyView(item.start - 7 * DAY_MS, item.start + 7 * DAY_MS)
  } else {
    applyView(item.start - 3 * DAY_MS, (item.end ?? item.start) + 3 * DAY_MS)
  }
}

/** 双击 item 打开观察档案（item.group = observation_id / obs_of） */
function onItemOpen(item: TItem): void {
  emit('pick-observation', String(item.group))
}

/**
 * 单击/双击统一在 click 内判别：
 * 单击聚焦会 setViewport，库视口变化时 item DOM 会被重建，浏览器原生
 * dblclick 要求两次 click 落在同一元素，导致真实双击时 dblclick 丢失。
 * 250ms 内同 item 的第二次 click → 打开档案；超时 → 聚焦。
 */
const DBLCLICK_MS = 250
let clickTimer: ReturnType<typeof setTimeout> | null = null
let lastClickId = ''
function onItemClick(item: TItem): void {
  if (clickTimer !== null && lastClickId === item.id) {
    clearTimeout(clickTimer)
    clickTimer = null
    lastClickId = ''
    onItemOpen(item)
    return
  }
  lastClickId = item.id
  clickTimer = setTimeout(() => {
    clickTimer = null
    onItemFocus(item)
  }, DBLCLICK_MS)
}

onBeforeUnmount(() => {
  if (clickTimer !== null) clearTimeout(clickTimer)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="otp-slide">
      <div
        v-if="show"
        class="otp-panel"
        data-testid="observation-timeline-panel"
      >
        <header class="otp-header">
          <NIcon :component="TimeOutline" size="18" class="otp-header-icon" />
          <span class="otp-title">观察图层时间轴</span>
          <NTag size="tiny" :bordered="false" type="info">{{ titleSuffix }}</NTag>

          <span
            v-if="focusedItem"
            class="otp-focus-chip"
            :title="`当前聚焦锚点：${focusLabel}（竖线与高亮标记的位置；点 × 回全览）`"
          >
            <span class="otp-focus-chip-dot" aria-hidden="true" />
            <span class="otp-focus-chip-text">已聚焦：{{ focusLabel }}</span>
            <button
              type="button"
              class="otp-focus-chip-x"
              title="清除聚焦并回到全览"
              aria-label="清除聚焦"
              data-testid="otp-clear-focus"
              @click="clearFocus"
            >×</button>
          </span>

          <div class="otp-header-spacer" />

          <span v-if="hasTimeData" class="otp-wheel-hint" aria-hidden="true">
            Ctrl/⌘ + 滚轮：以鼠标为锚点缩放 · Shift + 滚轮：横向平移
          </span>

          <div class="otp-nav" role="group" aria-label="时间轴视口导航">
            <button
              type="button"
              class="otp-icon-btn"
              title="向左平移（更早）"
              aria-label="向左平移"
              data-testid="otp-pan-left"
              :disabled="!hasTimeData"
              @click="panView(-1)"
            >
              <NIcon :component="ChevronBackOutline" size="16" />
            </button>
            <button
              type="button"
              class="otp-icon-btn"
              title="缩小（看更长时间范围）；快捷键：Ctrl/⌘ + 滚轮向下"
              aria-label="缩小"
              data-testid="otp-zoom-out"
              :disabled="!hasTimeData"
              @click="zoomView(0.8)"
            >
              <NIcon :component="RemoveCircleOutline" size="16" />
            </button>
            <button
              type="button"
              class="otp-icon-btn"
              title="全览（完整时间范围）"
              aria-label="全览"
              data-testid="otp-fit-all"
              :disabled="!hasTimeData"
              @click="fitAll"
            >
              <NIcon :component="ExpandOutline" size="16" />
            </button>
            <button
              type="button"
              class="otp-icon-btn"
              title="放大（聚焦更短时间范围）；快捷键：Ctrl/⌘ + 滚轮向上（以鼠标位置为锚点）"
              aria-label="放大"
              data-testid="otp-zoom-in"
              :disabled="!hasTimeData"
              @click="zoomView(1.25)"
            >
              <NIcon :component="AddCircleOutline" size="16" />
            </button>
            <button
              type="button"
              class="otp-icon-btn"
              title="向右平移（更晚）"
              aria-label="向右平移"
              data-testid="otp-pan-right"
              :disabled="!hasTimeData"
              @click="panView(1)"
            >
              <NIcon :component="ChevronForwardOutline" size="16" />
            </button>
          </div>

          <button
            type="button"
            class="otp-mode-btn"
            :class="{ 'otp-mode-active': mode === 'event' }"
            :disabled="!canUseEventTime"
            :title="canUseEventTime
              ? '在本时间轴内切换 过程时间/业务时间（不影响主画布视角）'
              : '本体未声明业务时间字段（semantic:event_time），仅能按过程时间排列'"
            data-testid="otp-toggle-time-mode"
            @click="toggleMode"
          >{{ modeLabel }}</button>

          <button
            type="button"
            class="otp-icon-btn"
            title="收起时间轴（画布操作不受影响时可保持展开）"
            aria-label="收起时间轴"
            data-testid="otp-close"
            @click="emit('update:show', false)"
          >
            <NIcon :component="CloseOutline" size="16" />
          </button>
        </header>

        <div class="otp-body" :style="{ height: `${PANEL_HEIGHT - HEADER_HEIGHT}px` }">
          <NEmpty
            v-if="!hasTimeData"
            description="当前口径下观察节点无时间字段（普通对象节点无 event_time）。尝试切换为「过程时间」口径，或运行「查行为时间线」等时间类镜头生成带时间字段的观察。"
          />
          <Timeline
            v-else
            :key="mode"
            ref="timelineRef"
            :items="model.items"
            :groups="model.groups"
            :viewport-min="viewport.min"
            :viewport-max="viewport.max"
            :initial-viewport-start="viewport.min"
            :initial-viewport-end="viewport.max"
            :min-viewport-duration="DAY_MS"
            :max-viewport-duration="viewport.max - viewport.min + DAY_MS"
            :stacking="stackingOptions"
            :active-items="activeItemIds"
            :markers="focusMarkers"
            @change-viewport="currentViewport = $event"
          >
            <template #group-label="{ group }">
              <span class="otp-group-col">
                <span class="otp-group" :title="(group as TGroup).fullTitle || group.label">{{ group.label }}</span>
                <span
                  v-if="(group as TGroup).metric"
                  class="otp-group-metric"
                  title="该镜头的常态事件间隔与簇总数，与轴上成簇段对照判读节奏异常"
                >{{ (group as TGroup).metric }}</span>
              </span>
            </template>
            <template #item="{ item }">
              <div
                class="otp-item"
                :class="[
                  `otp-item--${(item as TItem).type}`,
                  { 'otp-item--chain': (item as TItem).chain },
                ]"
                :title="`${(item as TItem).title || ''}（单击聚焦，双击打开观察档案）`"
                @click="onItemClick(item as TItem)"
              >
                <span
                  v-if="model.badgeById.get((item as TItem).id) !== undefined"
                  class="otp-badge"
                  :class="`otp-badge--${(item as TItem).type}`"
                >×{{ model.badgeById.get((item as TItem).id) }}</span>
              </div>
            </template>
          </Timeline>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.otp-panel {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 920;
  height: 340px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--sun-text-primary, #d7e6ee);
  background: rgba(5, 21, 34, 0.97);
  border-top: 1px solid var(--sun-border, rgba(63, 191, 168, 0.25));
  box-shadow: 0 -8px 28px rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(6px);
}

.otp-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 44px;
  padding: 0 12px;
  border-bottom: 1px solid rgba(63, 191, 168, 0.18);
  user-select: none;
}
.otp-header-icon {
  color: #3fbfa8;
}
.otp-title {
  font-weight: 600;
  font-size: 14px;
}
.otp-header-spacer {
  flex: 1 1 auto;
}
.otp-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-right: 8px;
}
.otp-wheel-hint {
  flex: 0 0 auto;
  margin-right: 10px;
  font-size: 11px;
  color: var(--sun-text-secondary, #9fb7c6);
  opacity: 0.75;
  white-space: nowrap;
  user-select: none;
}
@media (max-width: 1280px) {
  /* 窄屏头部空间紧张，快捷键说明只保留在按钮 tooltip 里 */
  .otp-wheel-hint {
    display: none;
  }
}

.otp-mode-btn {
  font-size: 12px;
  line-height: 1;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid rgba(63, 191, 168, 0.45);
  background: transparent;
  color: #3fbfa8;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s;
}
.otp-mode-btn:hover:not(:disabled) {
  background: rgba(63, 191, 168, 0.12);
}
.otp-mode-btn.otp-mode-active {
  background: #3fbfa8;
  color: #06231f;
  border-color: #3fbfa8;
}
.otp-mode-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.otp-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 4px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--sun-text-secondary, #9fb7c6);
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s, border-color 0.15s;
}
.otp-icon-btn:hover:not(:disabled) {
  background: rgba(63, 191, 168, 0.12);
  color: #3fbfa8;
  border-color: rgba(63, 191, 168, 0.35);
}
.otp-icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.otp-body {
  flex: 1 1 auto;
  /* 多泳道/多观察时整组纵向滚动；横向平移由组件内部接管，禁止外层横滚 */
  overflow-y: auto;
  overflow-x: hidden;
  position: relative;
  /* 紧凑泳道：默认 2em 行高 × 8 个观察组必超出面板高度 */
  --group-items-height: 22px;
  --item-stack-height: 20px;
  --item-stack-gap: 2px;
}
/* 库默认 color:#000，深色面板上刻度/组名不可读；currentColor 派生项一并修正 */
.otp-body :deep(.timeline-wrapper) {
  color: var(--sun-text-secondary, #9fb7c6);
}
/* stacking 泳道超限时后到项与先到项像素重叠：点抬到 range 带之上，保证点得中 */
.otp-body :deep(.group-items .item.point) {
  z-index: 2;
}
.otp-body :deep(.group-items .item.range) {
  z-index: 1;
}
.otp-group-col {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  max-width: 210px;
  overflow: hidden;
}
.otp-group {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  opacity: 0.85;
}
/* 镜头节奏摘要（中位间隔/簇数）：单行小字琥珀色，超宽省略，不抬升泳道行高 */
.otp-group-metric {
  flex: 0 0 auto;
  white-space: nowrap;
  font-size: 10px;
  color: #ffd54f;
  opacity: 0.85;
}
.otp-item {
  position: absolute;
  inset: 0;
  cursor: pointer;
  border-radius: 3px;
}
.otp-item--point {
  border-radius: 50%;
}
/* 链式簇：带宽是贪心链首末跨度而非天窗宽，虚线描边提示读图区别 */
.otp-item--chain {
  outline: 1.5px dashed rgba(255, 255, 255, 0.85);
  outline-offset: -1px;
}
.otp-item:hover {
  filter: brightness(1.2);
}

/* ---- 聚焦锚点视觉 ---- */
/* 琥珀色竖线：marker 无组，库在 .markers 层横贯全部泳道；默认 1px 红线被覆盖 */
.otp-body :deep(.marker.otp-focus-marker) {
  width: 2px;
  background: #ffd54f;
  box-shadow: 0 0 8px rgba(255, 213, 79, 0.85);
  z-index: 3;
  pointer-events: none;
}
/* 被点节点：库把 active 挂在外层 .item 壳上（非 slot 内层） */
.otp-body :deep(.item.active) {
  opacity: 1;
  z-index: 4;
}
/* point 外壳自带 translate(-50%,-50%) 居中，放大必须保留该平移 */
.otp-body :deep(.item.point.active) {
  transform: translate(-50%, -50%) scale(1.6);
  box-shadow:
    0 0 0 3px rgba(255, 213, 79, 0.95),
    0 0 10px 2px rgba(255, 193, 7, 0.8);
}
.otp-body :deep(.item.range.active) {
  outline: 2px solid #ffd54f;
  outline-offset: -1px;
  box-shadow: 0 0 12px rgba(255, 213, 79, 0.55);
}

/* 头部焦点芯片 */
.otp-focus-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 340px;
  height: 24px;
  padding: 0 6px 0 8px;
  margin-left: 4px;
  border-radius: 12px;
  font-size: 12px;
  color: #3a2c00;
  background: rgba(255, 213, 79, 0.92);
  box-shadow: 0 0 0 1px rgba(255, 213, 79, 0.4);
  user-select: none;
}
.otp-focus-chip-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #b8860b;
  box-shadow: 0 0 6px rgba(184, 134, 11, 0.9);
}
.otp-focus-chip-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.otp-focus-chip-x {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  font-size: 13px;
  line-height: 1;
  color: #3a2c00;
  background: transparent;
  cursor: pointer;
}
.otp-focus-chip-x:hover {
  background: rgba(0, 0, 0, 0.18);
  color: #000;
}

/* 同一时刻数量徽标（同组同 start，挂在代表项上，缩放无关） */
.otp-badge {
  position: absolute;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 700;
  line-height: 1;
  pointer-events: none;
}
.otp-badge--point {
  inset: 0;
  color: #fff;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.9), 0 0 2px rgba(0, 0, 0, 0.9);
}
.otp-badge--range {
  top: 1px;
  right: 3px;
  bottom: auto;
  left: auto;
  padding: 0 4px;
  height: 12px;
  border-radius: 6px;
  color: #fff;
  background: rgba(0, 0, 0, 0.45);
}

/* 显隐：自底部滑入 */
.otp-slide-enter-active,
.otp-slide-leave-active {
  transition: transform 0.22s ease, opacity 0.22s ease;
}
.otp-slide-enter-from,
.otp-slide-leave-to {
  transform: translateY(24px);
  opacity: 0;
}
</style>
