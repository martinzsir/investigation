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
    /** 观察层三元组边 [source, target, rel]：簇/窗 ──涉及──▶ 事件（确定性归属） */
    edges?: Array<[string, string, string]>
    observationCount?: number
  }>(),
  { observationCount: 0, edges: () => [] },
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
  // 两口径 item id 命名空间不同（proc: 聚合点 vs 节点 id），锚点/悬停一律作废
  focusedId.value = null
  hoveredId.value = null
}

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  /** 点击某条观察 item：observation_id（= item.group） */
  (e: 'pick-observation', observationId: string): void
}>()

interface TItemBase {
  id: string
  group: string
  start: number
  end?: number
  cssVariables?: Record<string, string>
  title?: string
  /** 库外壳 class（background 无 slot，状态样式只能走 className） */
  className?: string
}
/** 事件点（交易/通话/轨迹等，按 event_time 定位） */
interface TPointItem extends TItemBase {
  type: 'point'
}
/** 簇/窗铺底带：库原生 background 容器层，不参与分泳道、横贯泳道全高 */
interface TBandItem extends TItemBase {
  type: 'background'
  end: number
  /** burst 链式簇：相邻间隔均 ≤ 天窗但首末跨度超天窗 */
  chain?: boolean
  /** 区间细分种类（徽标键防 burst/collision 同日跨类合并） */
  intervalKind?: IntervalBandKind
}
type TItem = TPointItem | TBandItem
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

/** 间隔天数格式化：整数天不带小数，中位数可能为 .5。
 *  null/undefined/空串（后端缺字段，如旧产物无 span_days）必须返回 null——
 *  Number(null)===0，会把「缺数据」谎报成「跨度 0 天」 */
function fmtDays(v: unknown): string | null {
  if (v === null || v === undefined || v === '') return null
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

/**
 * 区间双色：主胶囊实色（视线焦点）+ 全高淡影（罩住多泳道成员点）。
 * burst 橙、碰撞窗蓝灰；色族同时用于簇内点的描边，建立「带—点」色彩对应。
 */
const BAND_SHADOW_SUFFIX = '__bandshadow'
function bandFill(kind: IntervalBandKind): string {
  return kind === 'burst' ? 'rgba(255,112,67,0.42)' : 'rgba(110,135,181,0.42)'
}
function bandShadowFill(kind: IntervalBandKind): string {
  return kind === 'burst' ? 'rgba(255,112,67,0.10)' : 'rgba(110,135,181,0.12)'
}
/* ------------------------------------------------------------------ */
/* 簇归属：后端「簇/窗 ──涉及──▶ 事件」边是确定性成员关系（旧产物无边时  */
/* 全部退化为散点，不做几何时间推断，链式簇跨度内也不会误判）。          */
/* ------------------------------------------------------------------ */
const bandMembers = computed(() => {
  const byBand = new Map<string, Set<string>>()
  const byEvent = new Map<string, Set<string>>()
  for (const [src, tgt, rel] of props.edges) {
    if (rel !== '涉及') continue
    if (!byBand.has(src)) byBand.set(src, new Set())
    byBand.get(src)!.add(tgt)
    if (!byEvent.has(tgt)) byEvent.set(tgt, new Set())
    byEvent.get(tgt)!.add(src)
  }
  return { byBand, byEvent }
})

/** 区间节点 id → 种类（成员点描边色按所属簇色族） */
const bandKindById = computed(() => {
  const m = new Map<string, IntervalBandKind>()
  for (const n of props.nodes) {
    const k = (n.props ?? {}) as Record<string, unknown>
    if (k.interval_kind === 'burst' || k.interval_kind === 'collision_window') {
      m.set(n.id, k.interval_kind)
    }
  }
  return m
})

/** 节点 → 所属观察泳道 gid（簇带与事件点同用 obs_of 分泳道） */
function nodeGroupOf(id: string): string | null {
  const n = props.nodes.find((x) => x.id === id)
  return n ? String(((n.props ?? {}) as Record<string, unknown>).obs_of ?? '_default') : null
}

/** 悬停点的主簇带：同泳道优先（一点可同时归属碰撞窗与其它泳道的聚集簇，
 *  跨带全亮是高亮噪音，只亮读图焦点所在带）；同泳道无带时碰撞窗优先、
 *  再退首个所属带。 */
function primaryBandOf(hoverGroup: string | null, owners: Set<string>): string | null {
  if (hoverGroup !== null) {
    for (const b of owners) {
      if (nodeGroupOf(b) === hoverGroup) return b
    }
  }
  for (const b of owners) {
    if (bandKindById.value.get(b) === 'collision_window') return b
  }
  return owners.values().next().value ?? null
}

/** 悬停锚点（仅指针未拖拽时更新）；点/带互高亮，离开时间轴清空 */
const hoveredId = ref<string | null>(null)
const hoverPos = ref<{ x: number; y: number } | null>(null)

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
 *   点放大一档标记聚合——过程口径回答的本来就是「何时做过哪次深挖」。
 *
 * 聚合计数（badgeById）：库 slot 给的 stackSize 是「重叠簇占用泳道数」，且随
 * collisionWidth 在不同缩放下变化（全览时 ±8 天占位会把整段密集期连成一条
 * 长链，每个点都挂 ×4），不等于「同一时刻事件数」。这里按业务语义自算：
 * 同组同 start 的 point 数 / 同组同 start 的 range 数，>1 才挂到代表项
 * （point 代表项放大点径 + 其余同刻 point 收起，background 簇带仍用
 * ::after 数字徽标）。
 */
const model = computed(() => {
  const items: TItem[] = []
  const groupMap = new Map<string, TGroup>()
  /** key → 计数；key 与代表 item id */
  const counts = new Map<string, number>()
  const badgeKeys = new Map<string, string>()
  /** key → 同刻 item id 登记顺序（首个为代表点；其余同刻 point 收起不显示） */
  const orders = new Map<string, string[]>()

  function ensureGroup(gid: string, m: ObsMeta): void {
    if (!groupMap.has(gid)) {
      const { label, fullTitle } = groupLabelOf(m, gid)
      groupMap.set(gid, { id: gid, label, fullTitle: fullTitle || undefined })
    }
  }

  /** 登记一个 item 的同刻计数与顺序（代表点放大、非代表 point 收起用）。
   *  簇带键必须带 interval_kind：burst 与 collision_window 同日起算时
   *  不是同一语义的重叠，跨类合并会虚增徽标。 */
  function registerBadge(item: TItem): void {
    const key = item.type === 'point'
      ? `p:${item.group}:${item.start}`
      : `r:${item.intervalKind ?? 'burst'}:${item.group}:${item.start}`
    const c = (counts.get(key) ?? 0) + 1
    counts.set(key, c)
    if (!badgeKeys.has(key)) badgeKeys.set(key, item.id)
    const seq = orders.get(key)
    if (seq) seq.push(item.id)
    else orders.set(key, [item.id])
  }

  // ---- hover 联动派生（process 聚合点不挂簇归属，不参与）----
  const emBands = new Set<string>()
  const emPoints = new Set<string>()
  /** 悬停源点（指针正下方那个点）：放大梯度高于同簇兄弟点 */
  const emPrimary = new Set<string>()
  let hoverGroup: string | null = null
  const hv = mode.value === 'event' ? hoveredId.value : null
  if (hv) {
    const hoverNode = props.nodes.find((n) => n.id === hv)
    const hp = (hoverNode?.props ?? {}) as Record<string, unknown>
    hoverGroup = hoverNode ? String(hp.obs_of ?? '_default') : null
    const owners = bandMembers.value.byEvent.get(hv)
    if (owners && owners.size > 0) {
      // 成员点盖在胶囊上层（z-index），纤细胶囊的可悬停面几乎全被点截获，
      // 因此悬停簇内任意一点一律按「悬停整簇」处理。带只取主簇
      // （primaryBandOf：同泳道优先），避免点亮其它泳道的聚集簇/碰撞窗。
      emPrimary.add(hv)
      emPoints.add(hv)
      const primary = primaryBandOf(hoverGroup, owners)
      if (primary) {
        emBands.add(primary)
        for (const e of bandMembers.value.byBand.get(primary) ?? []) emPoints.add(e)
      }
    } else if (bandKindById.value.has(hv)) {
      // 直接悬停到簇带空白边缘：高亮带本身 + 全部成员点（无单一源点）
      emBands.add(hv)
      for (const e of bandMembers.value.byBand.get(hv) ?? []) emPoints.add(e)
    } else {
      // 簇外散点：源点显著放大，不暗化同泳道（无簇焦点时 dim 无对比意义）
      emPrimary.add(hv)
      emPoints.add(hv)
    }
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
      // event 口径下区间节点成 background 铺底带（burst/collision_window）：
      // 不参与分泳道、横贯泳道全高，事件点自然画在「带内部」的上层
      const range = intervalRange(n)
      if (range) {
        // 显示层补宽：真实数据里大量 burst start==end（单日聚集簇），
        // 零宽带在轴上不可见。补满 1 天仅影响渲染/聚焦，不改后端语义。
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
        const isFocused = focusedId.value === n.id
        // 带提亮同样按泳道门禁（与成员点一致，防回退场景跨泳道亮带）
        const isEm = hoverGroup !== null && hoverGroup === gid && emBands.has(n.id)
        // 淡影先入（DOM 在下）：全高低透明，纯视觉、不接事件；
        // 主胶囊后入（DOM 在上）：2em 居中实色，承载点击/hover/徽标/聚焦
        const shadowState: string[] = []
        if (isFocused) shadowState.push('otp-band-focus')
        if (isEm) shadowState.push('otp-band-em')
        items.push({
          id: `${n.id}${BAND_SHADOW_SUFFIX}`,
          group: gid,
          type: 'background',
          start: range.start,
          end,
          cssVariables: { '--item-background': bandShadowFill(range.kind) },
          className: ['otp-band-shadow', ...shadowState].join(' '),
        })
        const item: TItem = {
          id: n.id,
          group: gid,
          type: 'background',
          start: range.start,
          end,
          cssVariables: { '--item-background': bandFill(range.kind) },
          title,
          chain: isChain,
          intervalKind: range.kind,
          className: [
            'otp-band',
            isChain ? 'otp-band-chain' : '',
            isFocused ? 'otp-band-focus' : '',
            isEm ? 'otp-band-em' : '',
          ].filter(Boolean).join(' '),
        }
        items.push(item)
        registerBadge(item)
        continue
      }
      // 时间戳节点成 point；簇内成员点按「涉及」边挂色族描边
      const t = nodeTimestamp(n, 'event')
      if (t !== null) {
        ensureGroup(gid, meta)
        const cls = new Set<string>()
        let memLabel = ''
        const ownerBands = bandMembers.value.byEvent.get(n.id)
        if (ownerBands && ownerBands.size > 0) {
          let hasBurst = false
          let hasCollision = false
          for (const b of ownerBands) {
            if (bandKindById.value.get(b) === 'collision_window') hasCollision = true
            else if (bandKindById.value.get(b) === 'burst') hasBurst = true
          }
          // 同时归属两类时取簇橙（burst 样式在 CSS 中后定义以覆盖）
          if (hasCollision) cls.add('otp-mem-collision')
          if (hasBurst) cls.add('otp-mem-burst')
          memLabel = hasBurst ? '聚集簇内事件' : '碰撞窗内事件'
        }
        // em 类按泳道门禁：「涉及」边可跨观察引用事件节点，同一事件 id 会在
        // 其它泳道再渲染一个点实例；只亮读图焦点所在泳道的实例，防跨泳道高亮
        const emHit = hoverGroup === gid && (emPrimary.has(n.id) || emPoints.has(n.id))
        if (emHit) {
          // 注意：Set.add 只接受一个参数，不能合并调用
          cls.add('otp-pt-em')
          if (emPrimary.has(n.id)) cls.add('otp-pt-em-primary')
        }
        // dim 仅在存在簇/窗焦点时有意义：同泳道非成员点弱化衬突成员
        else if (emBands.size > 0 && hoverGroup === gid) cls.add('otp-pt-dim')
        const item: TItem = {
          id: n.id,
          group: gid,
          type: 'point',
          start: t,
          title: memLabel ? `${nodeTitle(n)}｜${memLabel}` : nodeTitle(n),
          className: cls.size > 0 ? Array.from(cls).join(' ') : undefined,
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
  // 簇带 ×N 徽标：background 无 slot，经 CSS 变量喂给 ::after；
  // 无徽标时不写该变量，CSS 回落 content:none 不生成伪元素
  for (const it of items) {
    if (it.type !== 'background') continue
    const c = badgeById.get(it.id)
    if (c !== undefined) it.cssVariables!['--otp-badge'] = `"×${c}"`
  }
  // 聚合点（计数 >1 的代表点）：不挂数字胶囊，统一放大一档点径
  // （12px→18px，库外壳 height/width 读 --item-point-size，居中平移不受影响），
  // 精确聚合数进 hover title；同刻其余点在下方 hiddenIds 收起
  for (const it of items) {
    if (it.type !== 'point') continue
    const c = badgeById.get(it.id)
    if (c === undefined) continue
    it.cssVariables = { ...(it.cssVariables ?? {}), '--item-point-size': '18px' }
    it.title = `${it.title ?? ''}｜聚合 ${c} 起`
  }
  // 同刻聚合只显示代表点（已放大一档）：其余同刻点与其同一 x 堆叠，
  // 大点下只露杂边且无独立语义，收起不发 item（代表点 title 已含
  // 「聚合 N 起」；被收起节点仍可从画布打开档案）。簇带（r: 键）不收起
  // ——每条是独立区间段。process 聚合点不经 registerBadge，orders 中无键。
  const hiddenIds = new Set<string>()
  for (const [key, seq] of orders) {
    if (!key.startsWith('p:') || seq.length <= 1) continue
    for (const id of seq.slice(1)) hiddenIds.add(id)
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
  const visibleItems = items.filter(it => !hiddenIds.has(it.id))
  return {
    items: visibleItems,
    groups: Array.from(groupMap.values()),
    hasTimeData: visibleItems.length > 0,
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

/** 悬停提示统一走自绘跟随浮层（点/带同一套）。弃用原生 title：点 hover 高亮
 *  会切换 transform scale，原生提示在出现的关键 0.5s 内被渲染层变化打断，
 *  表现为「时有时无」；background 簇带又本就没有 slot/title。
 *  point 取点自身 title（含「聚合 N 起」）+ 交互提示；background 取带 title。 */
const hoverTip = computed(() => {
  const id = hoveredId.value
  if (!id) return ''
  const it = model.value.items.find((i) => i.id === id)
  if (!it) return ''
  return it.type === 'point'
    ? `${it.title ?? ''}（单击聚焦，双击打开观察档案）`
    : (it.title ?? '')
})

/** 收起面板：Timeline 随 v-if 销毁、重开回到全览，锚点/悬停状态同步作废 */
watch(
  () => props.show,
  (v) => {
    if (!v) {
      focusedId.value = null
      hoveredId.value = null
    }
  },
)

/* ------------------------------------------------------------------ */
/* 视口导航（set-viewport 范式）：全览 → 钻取                           */
/* ------------------------------------------------------------------ */

// Timeline 是泛型函数式 SFC，InstanceType<typeof Timeline> 无法满足构造签名约束
// （TS2344）；此处只用到 expose 出的 setViewport，按实际用法声明最小结构类型。
const timelineRef = ref<{ setViewport: (start?: number, end?: number) => void } | null>(null)
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

/** 单击 item 聚焦：point → 该日前后各 7 天；簇带 → 区间两侧各留 3 天 */
function onItemFocus(item: TItem): void {
  // 固化锚点：视口跳过去之后靠簇带/点高亮 + 竖线 + 头部芯片辨认
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
 * 库事件统一在 Timeline 顶层接收（background 是裸 div、point 走 slot，
 * 两者都由库 emit 同形载荷 {time,event,item}；marker/空白点击 item=null）。
 * 单击/双击判别：单击聚焦会 setViewport，库视口变化时 item DOM 会被重建，
 * 浏览器原生 dblclick 要求两次落在同一元素会丢失，故 250ms 内同 item
 * 第二次 click → 打开档案；超时 → 聚焦。
 */
interface TTimelinePayload {
  time?: number
  event: MouseEvent
  item: { id: string; type: string } | null
}
const DBLCLICK_MS = 250
let clickTimer: ReturnType<typeof setTimeout> | null = null
let lastClickId = ''

function resolveItem(raw: TTimelinePayload['item']): TItem | null {
  if (!raw) return null
  // 淡影是合法的悬停/点击面（可见即可悬停），统一把 shadow id 归并到主胶囊
  const id = raw.id.endsWith(BAND_SHADOW_SUFFIX)
    ? raw.id.slice(0, -BAND_SHADOW_SUFFIX.length)
    : raw.id
  return model.value.items.find((i) => i.id === id) ?? null
}

function onTimelineClick(p: TTimelinePayload): void {
  const item = resolveItem(p.item)
  if (!item) return
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

/** 悬停联动：拖拽平移（buttons 非 0）期间不更新，避免跟随视口抖动 */
function onTimelinePointerMove(p: TTimelinePayload): void {
  if (p.event.buttons !== 0) return
  const item = resolveItem(p.item)
  const id = item?.id ?? null
  if (id !== hoveredId.value) hoveredId.value = id
  if (id) hoverPos.value = { x: p.event.clientX, y: p.event.clientY }
}

/** 指针在时间轴空白区域移动时清除悬停。
 *  库只在 item 上 stop 了 pointermove，mousemove 是另一种事件、仍从 item 冒泡到
 *  时间轴根：同一次物理移动会先 pointermove(item) 再 mousemove(冒泡)。若不排除
 *  item 冒泡源，hoveredId 会被以移动频率「设了又清」——鼠标一停终值为 null，
 *  高亮即消失（此前悬停看不到效果的根因）。 */
function onTimelineSpaceMove(p: { event: MouseEvent }): void {
  if (p.event.buttons !== 0) return
  const t = p.event.target as Element | null
  if (t?.closest('.item, .background')) return
  if (hoveredId.value !== null) hoveredId.value = null
}

function onTimelineLeave(): void {
  hoveredId.value = null
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
            @click="onTimelineClick"
            @pointermove="onTimelinePointerMove"
            @mousemove-timeline="onTimelineSpaceMove"
            @mouseleave-timeline="onTimelineLeave"
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
            <!-- 仅 point 走 slot（background 簇带是裸 div，状态走 className/CSS 变量）。
                 不设 title：原生提示会被 hover 高亮的 transform 切换打断，统一走 hoverTip 浮层；
                 slot props（item/lane/...）此处用不到，不解构以避免 TS6133 -->
            <template #item>
              <div class="otp-item otp-item--point"></div>
            </template>
          </Timeline>
        </div>
      </div>
    </Transition>
  </Teleport>
  <!-- 悬停提示浮层（点/带统一，跟随指针；弃用原生 title，见 hoverTip）。
       必须独立 Teleport 到 body：.otp-panel 的 backdrop-filter 会为后代建立
       containing block，把 position:fixed 收编成面板相对坐标（浮层会飞出视口） -->
  <Teleport to="body">
    <div
      v-if="hoverTip && hoverPos"
      class="otp-hover-tip"
      :style="{ left: `${hoverPos.x + 14}px`, top: `${hoverPos.y + 12}px` }"
    >{{ hoverTip }}</div>
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
/* slot 内层只有 point（库外壳决定尺寸/底色），透明壳承载徽标与手型 */
.otp-item {
  position: absolute;
  inset: 0;
  cursor: pointer;
  border-radius: 50%;
}

/* ---- 层叠扶正：backgrounds 层 DOM 在 items 之后，默认会盖住点并截获     */
/* 点击；显式铺底带 z-index:0、点 z-index:1（聚焦竖线 3、active 点 4） ---- */
.otp-body :deep(.item.point) {
  z-index: 1;
  /* 点必须自身可命中：悬停放大、单击聚焦、双击开档案都走 item 的指针事件 */
  pointer-events: auto;
  /* 库默认 contain:strict（=size+layout+paint+style）：paint 裁剪会连元素
     自身的 box-shadow 光环（簇色 ring、聚焦琥珀环）也裁进点盒。点盒尺寸
     由库显式给定（不依赖内容），解除不影响布局；点自带 transform 仍是
     绝对定位的 containing block。background 簇带保持 strict（徽标在带内） */
  contain: none;
}

/* 淡影：保持库默认 top:0/bottom:0 的泳道全高，低透明罩住多泳道成员点。
   交互上它就是「整簇悬停/点击面」——可见即可悬停（resolveItem 已把
   shadow id 归并到主胶囊）；点 z-index:1 仍在其上，悬停点走点→簇联动 */
.otp-body :deep(.background.otp-band-shadow) {
  z-index: 0;
  border-radius: 4px;
  cursor: pointer;
}

/* 主胶囊：2em 单行高、垂直居中（横向仍走库的 translate 独立属性，
   这里只叠加 transform 做垂直居中，互不冲突） */
.otp-body :deep(.background.otp-band) {
  z-index: 0;
  top: 50%;
  bottom: auto;
  height: var(--item-stack-height, 2em);
  transform: translateY(-50%);
  /* 无徽标时不生成 ::after；有徽标由 item cssVariables 覆盖为 "×N" */
  --otp-badge: none;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.22);
}

/* 簇带 ×N 徽标：background 无 slot，伪元素读 CSS 变量（none 时不生成） */
.otp-body :deep(.background.otp-band)::after {
  content: var(--otp-badge);
  position: absolute;
  top: 50%;
  right: 4px;
  transform: translateY(-50%);
  z-index: 1;
  display: inline-flex;
  align-items: center;
  height: 12px;
  padding: 0 4px;
  border-radius: 6px;
  font-size: 9px;
  font-weight: 700;
  line-height: 1;
  /* 浅底深字：不依赖与彩色胶囊的对比，橙/蓝灰带上都清晰 */
  color: #10212c;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 0 0 1px rgba(16, 33, 44, 0.28);
  pointer-events: none;
}

/* 链式簇：带宽是贪心链首末跨度而非天窗宽，虚线描边提示读图区别 */
.otp-body :deep(.background.otp-band-chain) {
  outline: 1.5px dashed rgba(255, 255, 255, 0.85);
  outline-offset: -1px;
}

/* ---- hover 联动：悬停簇带↔成员点互高亮，同泳道其余点弱化 ---- */
/* 半透明胶囊上 brightness 变化肉眼偏弱，叠加白描边+外发光确保可感 */
.otp-body :deep(.background.otp-band-em) {
  filter: brightness(1.6);
  box-shadow:
    inset 0 0 0 1.5px rgba(255, 255, 255, 0.65),
    0 0 10px rgba(255, 255, 255, 0.22);
}
.otp-body :deep(.background.otp-band-shadow.otp-band-em) {
  filter: brightness(1.8);
}
.otp-body :deep(.item.point.otp-pt-dim) {
  opacity: 0.2;
}
/* 同簇兄弟成员点：中等放大 + 轻白光（保留 mem 类自带的簇色 ring） */
.otp-body :deep(.item.point.otp-pt-em) {
  opacity: 1;
  z-index: 5;
  transform: translate(-50%, -50%) scale(1.4);
  filter: brightness(1.15) drop-shadow(0 0 3px rgba(255, 255, 255, 0.75));
}
/* 指针正下方的源点/簇外散点：近两倍放大 + 强白光环，最醒目 */
.otp-body :deep(.item.point.otp-pt-em.otp-pt-em-primary) {
  z-index: 6;
  transform: translate(-50%, -50%) scale(1.9);
  filter: brightness(1.3)
    drop-shadow(0 0 2px rgba(255, 255, 255, 0.95))
    drop-shadow(0 0 7px rgba(255, 255, 255, 0.6));
}

/* ---- 簇内成员点：白芯 + 所属簇色族描边；同时归属两类时簇橙优先         */
.otp-body :deep(.item.point.otp-mem-collision) {
  --item-background: #dfe7f5;
  box-shadow: 0 0 0 2px #8fa6cc;
  opacity: 0.96;
}
.otp-body :deep(.item.point.otp-mem-burst) {
  --item-background: #ffffff;
  box-shadow:
    0 0 0 2px #ff8a50,
    0 0 4px rgba(255, 112, 67, 0.7);
  opacity: 0.96;
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
/* 簇带聚焦高亮（库不给 background 挂 .active，走 otp-band-focus） */
.otp-body :deep(.background.otp-band.otp-band-focus) {
  outline: 2px solid #ffd54f;
  outline-offset: -1px;
  box-shadow:
    0 0 12px rgba(255, 213, 79, 0.55),
    inset 0 0 0 1px rgba(255, 255, 255, 0.22);
}
/* 淡影聚焦：仅内侧细金线，不抢主胶囊焦点 */
.otp-body :deep(.background.otp-band-shadow.otp-band-focus) {
  box-shadow: inset 0 0 0 1px rgba(255, 213, 79, 0.65);
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

/* 悬停提示浮层：fixed 跟随指针，事件由库顶层回传 clientX/Y */
.otp-hover-tip {
  position: fixed;
  z-index: 1000;
  max-width: 340px;
  padding: 4px 8px;
  border: 1px solid rgba(63, 191, 168, 0.4);
  border-radius: 4px;
  background: rgba(3, 17, 28, 0.95);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
  color: #d7e6ee;
  font-size: 11px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  pointer-events: none;
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
