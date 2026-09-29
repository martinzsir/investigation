<script setup lang="ts">
/**
 * 节点窗口（WIN-02）：贴附在节点旁的 DOM 浮层。
 *
 * 为什么是浮层而不是卡内展开
 * --------------------------
 * 画布卡片 `research-card` 继承 G6 的 Rect，是 **canvas 图形节点不是 DOM**
 * （186×50，卡内坐标写死），内嵌不了任何 HTML。所以窗口只能是 DOM 浮层，
 * 按节点**投影后的屏幕坐标**定位（定位逻辑在 domain/canvas-window.ts，
 * 越界翻转可断言）。
 *
 * 内容按节点类型分化（WIN-03~09）
 * ------------------------------
 *   subject → 关系窗口    place → 地图窗口
 *   event   → 时间窗口    analysis_result → 证据窗口
 *   item    → 持有链窗口
 * 无专属窗口的节点不弹空窗，写明原因（点了没反应比没有这功能更糟）。
 *
 * 两条红线
 * --------
 * 1. 分数不单独表态：证据窗口必带三维 count/precision/weight（SYM-06）。
 * 2. 精度档贯穿：时间/关系/证据三处都用同一套 PRECISION 口径（SYM-01）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NTag } from 'naive-ui'
import {
  WINDOW_BOX,
  buildEvidenceWindow,
  buildHoldingWindow,
  deriveHoldingLinks,
  buildRelationWindow,
  buildTimeWindow,
  placeWindow,
  windowKindFor,
  type HoldingLink,
  type WindowNode,
} from '../../domain/canvas-window'
import {
  PRECISION_LABEL,
  chainEdgeSymbol,
  describeDim,
  nodeSymbol,
} from '../../domain/canvas-symbol'
import WindowMap from './WindowMap.vue'
import HypothesisWindow from './win/HypothesisWindow.vue'

const props = withDefaults(
  defineProps<{
    node: WindowNode | null
    /** 锚点屏幕坐标（画布容器坐标系，非画布数据坐标） */
    anchor: { x: number; y: number }
    viewport: { width: number; height: number }
    edges?: Array<{ source: string; target: string; rel?: string; data?: Record<string, unknown> }>
    nodeById?: ReadonlyMap<string, WindowNode>
    supports?: Array<{ obs_id?: string; label?: string; precision?: string; dim?: string }>
    events?: Array<{ id: string; at?: string; label?: string; precision?: string; dim?: string }>
    holdingLinks?: HoldingLink[]
    /** 后端取支撑观察中（仅研判结论节点会查） */
    loading?: boolean
    /** 加载失败或"无从定位"的原因——静默空列表会被读成"确实没有支撑" */
    error?: string
  }>(),
  { edges: () => [], supports: () => [], events: () => [], holdingLinks: () => [],
    loading: false, error: '' },
)

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-node', id: string): void
  (e: 'jump-observation', obsId: string): void
}>()

const resolved = computed(() => windowKindFor(props.node))
const placement = computed(() =>
  placeWindow(props.anchor, props.viewport, WINDOW_BOX),
)

const symbol = computed(() =>
  nodeSymbol({
    ambiguous: props.node?.props?.person_pk_ambiguous === true,
    precision: String(props.node?.props?.precision ?? ''),
    unanchored: props.node?.props?.person_pk == null,
  }),
)

const relation = computed(() =>
  props.node
    ? buildRelationWindow(props.node, props.edges, props.nodeById ?? new Map())
    : null,
)
const evidence = computed(() =>
  props.node ? buildEvidenceWindow(props.node, props.supports) : null,
)
const time = computed(() =>
  props.node ? buildTimeWindow(props.node, props.events) : null,
)
const holding = computed(() => {
  if (!props.node) return null
  // 宿主漏传 holdingLinks 时按边派生。漏传不该表现为"无持有记录"——
  // **有数据却显示没有**，比没有这个功能更糟。
  const links = props.holdingLinks.length
    ? props.holdingLinks
    : deriveHoldingLinks(props.node, props.edges, props.nodeById)
  return buildHoldingWindow(props.node, links)
})

/** 链上盒 = 可排序环节 + 不可判定环节（后者单独成行，且不连任何边） */
const chainNodes = computed(() => {
  const l = holding.value?.layout
  if (!l) return []
  return [...l.nodes, ...l.detached]
})

/** 符号在 computed 里预计算——模板里重复调函数会让同一条边算三遍 */
const chainEdges = computed(() => {
  const l = holding.value?.layout
  if (!l) return []
  return l.edges.map((e) => {
    const sym = chainEdgeSymbol(e)
    // 实线的 dasharray 必须给 undefined 而不是空串——空串在部分浏览器
    // 会被当成"虚线 0"，线直接消失。
    return { ...e, sym, dashArray: sym.lineDash.join(' ') || undefined }
  })
})

/**
 * 图例必给：没有图例的彩色线等于没画。
 * 文案**直接取符号的 note**，不另写一套——否则图例和线会说不一致的话。
 */
const chainLegend = computed(() => {
  const seen = new Set<string>()
  const out: Array<{ note: string; stroke: string; dash: string }> = []
  for (const e of chainEdges.value) {
    if (seen.has(e.sym.note)) continue
    seen.add(e.sym.note)
    out.push({ note: e.sym.note, stroke: e.sym.stroke, dash: e.sym.lineDash.join(' ') })
  }
  return out
})

const KIND_TITLE: Record<string, string> = {
  relation: '关系窗口',
  map: '地图窗口',
  time: '时间窗口',
  evidence: '证据窗口',
  holding: '持有链窗口',
  hypothesis: '假设窗口',
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    v-if="node"
    class="nwin"
    :style="{ left: placement.left + 'px', top: placement.top + 'px', width: WINDOW_BOX.width + 'px' }"
    role="dialog"
    :aria-label="`${KIND_TITLE[resolved.kind ?? ''] ?? '节点'}：${node.label}`"
  >
    <div class="nwin-head">
      <span class="nwin-prefix" v-if="symbol.labelPrefix">{{ symbol.labelPrefix }}</span>
      <span class="nwin-title">{{ node.label }}</span>
      <NTag size="small" :bordered="false">{{ KIND_TITLE[resolved.kind ?? ''] ?? '节点' }}</NTag>
      <button class="nwin-close" type="button" aria-label="关闭" @click="emit('close')">×</button>
    </div>
    <div class="nwin-note">{{ symbol.note }}</div>

    <!-- 取数中 / 取数失败：失败必须看得见，空列表会被读成"没有支撑" -->
    <div v-if="loading" class="nwin-empty">支撑观察加载中…</div>
    <div v-else-if="error" class="nwin-empty">{{ error }}</div>

    <!-- 无专属窗口：写明原因，不弹空窗 -->
    <div v-if="!resolved.kind" class="nwin-empty">{{ resolved.reason }}</div>

    <!-- WIN-03 关系 -->
    <div v-else-if="resolved.kind === 'relation' && relation" class="nwin-body">
      <div v-if="relation.empty" class="nwin-empty">{{ relation.empty }}</div>
      <template v-else>
        <button
          v-for="r in relation.rows"
          :key="r.id"
          type="button"
          class="row"
          @click="emit('open-node', r.id)"
        >
          <span class="row-main">{{ r.label }}</span>
          <span class="row-sub">{{ r.text }}</span>
          <span class="row-tag" :class="r.system ? 'row-tag--sys' : 'row-tag--man'">
            {{ r.system ? '系统推断' : '人工确认' }}
          </span>
        </button>
        <div class="nwin-hint">排序按加权分，不按裸次数</div>
      </template>
    </div>

    <!-- WIN-04 地图 -->
    <div v-else-if="resolved.kind === 'map'" class="nwin-body">
      <WindowMap :node="node" :height="168" />
    </div>

    <!-- WIN-05 时间 -->
    <div v-else-if="resolved.kind === 'time' && time" class="nwin-body">
      <div v-if="time.empty" class="nwin-empty">{{ time.empty }}</div>
      <template v-else>
        <div class="nwin-stat">
          可判时间窗重叠 <b>{{ time.determinableCount }}</b> 条 ·
          仅同地异时 <b>{{ time.degradedCount }}</b> 条
        </div>
        <div
          v-for="r in time.rows"
          :key="r.id"
          class="row row--static"
          :class="r.precision === 'minute' || r.precision === 'second' ? 'row--solid' : 'row--dash'"
        >
          <span class="row-main">{{ r.at }}</span>
          <span class="row-sub">{{ r.label }}</span>
          <span class="row-tag">{{ PRECISION_LABEL[r.precision] }}</span>
        </div>
      </template>
    </div>

    <!-- WIN-06 证据 -->
    <div v-else-if="resolved.kind === 'evidence' && evidence" class="nwin-body">
      <div class="matrix">
        <div
          v-for="c in evidence.dims"
          :key="c.dim"
          class="cell"
          :class="c.hit ? 'cell--hit' : 'cell--miss'"
        >
          <span class="cell-text">{{ describeDim(c) }}</span>
        </div>
      </div>
      <div v-if="!evidence.scoreAlone" class="nwin-warn">
        {{ evidence.scoreAloneReason }}，已强制渲染三维矩阵
      </div>
      <div class="nwin-score">加权分 {{ evidence.score }}</div>
      <div v-if="evidence.supports.length" class="block">
        <div class="block-title">支撑观察（点击回档案）</div>
        <button
          v-for="s in evidence.supports"
          :key="s.obs_id"
          type="button"
          class="row"
          :disabled="!s.obs_id"
          @click="s.obs_id && emit('jump-observation', s.obs_id)"
        >
          <span class="row-main">{{ s.label || s.obs_id }}</span>
          <span class="row-sub">{{ s.dim }}｜{{ PRECISION_LABEL[s.precision] }}</span>
        </button>
      </div>
      <div v-if="evidence.falsification.length" class="block">
        <div class="block-title">证伪条件</div>
        <ul class="falsify">
          <li v-for="(f, i) in evidence.falsification" :key="i">{{ f }}</li>
        </ul>
      </div>
    </div>

    <!-- WIN-09 持有链（WIN-11 蛇形布局） -->
    <div v-else-if="resolved.kind === 'holding' && holding" class="nwin-body">
      <div class="nwin-note nwin-note--warn">{{ holding.note }}</div>

      <!-- snake：蛇形折返链图 -->
      <div v-if="holding.layoutKind === 'snake' && holding.layout" class="chain">
        <svg
          class="chain-svg"
          :viewBox="`0 0 ${holding.layout.width} ${holding.layout.height}`"
          :width="holding.layout.width"
          :height="holding.layout.height"
          role="img"
          :aria-label="`持有链：${holding.item}，共 ${holding.layout.nodes.length} 手`"
        >
          <path
            v-for="(e, i) in chainEdges"
            :key="`e${i}`"
            :d="e.d"
            fill="none"
            :stroke="e.sym.stroke"
            :stroke-width="e.sym.lineWidth"
            :stroke-dasharray="e.dashArray"
          />
          <g v-for="n in chainNodes" :key="n.id">
            <rect
              :x="n.x"
              :y="n.y"
              :width="n.w"
              :height="n.h"
              rx="4"
              :class="n.unknownRange ? 'chain-box chain-box--unknown' : 'chain-box'"
            />
            <text class="chain-name" :x="n.x + 8" :y="n.y + 19">{{ n.holder }}</text>
            <text class="chain-range" :x="n.x + 8" :y="n.y + 35">{{ n.rangeText }}</text>
          </g>
        </svg>

        <div class="chain-legend">
          <span v-for="(g, i) in chainLegend" :key="i" class="lg">
            <svg class="lg-line" width="18" height="6">
              <line
                x1="0"
                y1="3"
                x2="18"
                y2="3"
                :stroke="g.stroke"
                stroke-width="2"
                :stroke-dasharray="g.dash || undefined"
              />
            </svg>
            {{ g.note }}
          </span>
        </div>

        <!--
          不可判定环节不进链序（红线 1）。必须显式说明，
          否则正兵会把"没画进链里"读成"这条链到此为止"。
        -->
        <div v-if="holding.layout.detached.length" class="nwin-note">
          {{ holding.layout.detached.length }} 个环节时间不可判定，未纳入链序（不排≠不在）
        </div>
      </div>

      <!-- flat：无可排序环节时列表比图清楚（不是降级） -->
      <template v-else>
        <div v-for="(l, i) in holding.links" :key="i" class="row row--static">
          <span class="row-main">{{ l.holder }}</span>
          <span class="row-sub">
            {{ l.unknownRange ? '持有区间不可判定' : `${l.start} → ${l.end}` }}
          </span>
        </div>
      </template>
      <div v-if="!holding.links.length" class="nwin-empty">暂无持有链记录</div>
    </div>

    <!-- WIN-07 假设窗口：推断来源 + 支撑/反驳证据 -->
    <HypothesisWindow
      v-else-if="resolved.kind === 'hypothesis'"
      :node="node"
      :edges="edges"
      :node-by-id="nodeById"
    />
  </div>
</template>

<style scoped>
.nwin {
  position: absolute;
  z-index: 40;
  max-height: 420px;
  overflow-y: auto;
  padding: 10px 12px;
  border: 1px solid var(--sun-border-active);
  border-radius: 6px;
  background: rgba(5, 21, 34, 0.97);
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.5);
}
.nwin-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.nwin-prefix {
  color: var(--sun-warn-text);
  font-weight: 700;
}
.nwin-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.nwin-close {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--sun-text-tertiary);
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 2px;
}
.nwin-note {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  margin-bottom: 6px;
}
.nwin-note--warn {
  color: var(--sun-warn-text);
}
.nwin-empty {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  padding: 8px 0;
}
.nwin-warn {
  font-size: 10px;
  color: var(--sun-warn-text);
  margin: 4px 0;
}
.nwin-score {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  margin: 4px 0;
}
.nwin-stat {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  margin-bottom: 6px;
}
.nwin-hint {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  margin-top: 4px;
}
.row {
  appearance: none;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  margin-bottom: 3px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: var(--sun-bg-card);
  cursor: pointer;
  text-align: left;
}
.row--static {
  cursor: default;
}
.row--solid {
  border-left: 3px solid #18a058;
}
.row--dash {
  border-left: 3px dashed #f0a020;
}
.row-main {
  font-size: 12px;
  color: var(--sun-text-primary);
  white-space: nowrap;
}
.row-sub {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-tag {
  flex: none;
  font-size: 9px;
  padding: 2px 5px;
  border-radius: 8px;
  border: 1px solid var(--sun-border);
  color: var(--sun-text-tertiary);
}
.row-tag--sys {
  color: var(--sun-info-text);
  border-color: var(--sun-info-border);
}
.row-tag--man {
  color: var(--sun-success-text, #18a058);
  border-color: var(--sun-success-border, #2f7d5b);
}
.matrix {
  display: grid;
  gap: 3px;
  margin-bottom: 6px;
}
.cell {
  font-size: 11px;
  padding: 4px 6px;
  border-radius: 4px;
  border: 1px solid var(--sun-border);
}
.cell--hit {
  color: var(--sun-text-primary);
  background: var(--sun-bg-card);
}
.cell--miss {
  color: var(--sun-text-tertiary);
  border-style: dashed;
}
.block {
  margin-top: 6px;
}
.block-title {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  margin-bottom: 4px;
}
.falsify {
  margin: 0;
  padding-left: 16px;
  font-size: 10px;
  color: var(--sun-text-tertiary);
}

/* 持有链（WIN-11 蛇形） */
.chain {
  margin-bottom: 6px;
}
.chain-svg {
  display: block;
  max-width: 100%;
  height: auto;
}
.chain-box {
  fill: var(--sun-bg-card);
  stroke: var(--sun-border-active);
  stroke-width: 1;
}
/* 不可判定环节：空心 + 虚线框。空心的理由同地图质心——
   实心会让它看起来和已确认的环节一样可靠。 */
.chain-box--unknown {
  fill: transparent;
  stroke: var(--sun-text-tertiary);
  stroke-dasharray: 3 2;
}
.chain-name {
  font-size: 11px;
  fill: var(--sun-text-primary);
}
.chain-range {
  font-size: 9px;
  fill: var(--sun-text-tertiary);
}
.chain-legend {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 4px;
  font-size: 9px;
  color: var(--sun-text-tertiary);
}
.lg {
  display: flex;
  align-items: center;
  gap: 4px;
}
.lg-line {
  flex: none;
}
</style>
