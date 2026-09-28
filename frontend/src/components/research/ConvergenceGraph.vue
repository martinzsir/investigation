<script setup lang="ts">
// FE-P-011 ConvergenceGraph：单锚点三维证据子图（@antv/g6 v5，radial 布局）。
//
// 为什么只画"单锚点"，不画全局 229 节点
// ---------------------------------------
// 锚点是（人 · 时 · 地）三元组。全局力导向会把"谁在哪天"这个语义压平，
// 且三维交汇的价值恰在"**单个**锚点上有几个维度同时命中"——一张星型子图
// 即可完整表达。全局分布由时间轴 + 地图承担，不交给图。
//
// 图为什么长这样
// --------------
// anchor（中心）→ dim（三维）→ support（各自支撑观察），旁挂 co_present。
// 层级即语义：从中心往外是"谁在哪天 → 哪一维看到 → 凭哪条观察"。
//
// 纪律（与 GraphCanvas.vue 同）
// ----------------------------
// G6 经动态 import 装载；**装载失败 / 渲染异常 / 无 canvas 一律降级为结构化
// 列表**，禁空白——构图数据本就完备，降级只是换一种呈现，不是数据不可用。
//
// 样式口径不在本组件定义：颜色 / 线宽 / 线型由 domain/convergence-graph.ts
// 给出，这里只做「datum → G6 style」的映射。改样式改 domain，不必动模板，
// 且能脱离 canvas 单测（R-1 精度可见、R-2 重名不猜）。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type {
  ConvergenceDetailResult,
  ConvergenceItem,
} from '../../api/endpoints/convergence'
import { DIM_LABEL, DIM_ORDER } from '../../api/endpoints/convergence'
import {
  GRAPH_LEGEND,
  buildConvergenceGraph,
  type ConvergenceGraphEdge,
  type ConvergenceGraphNode,
} from '../../domain/convergence-graph'

const props = defineProps<{
  item: ConvergenceItem
  detail?: ConvergenceDetailResult | null
}>()

const emit = defineEmits<{
  (e: 'fallback'): void
  (e: 'open-observation', obsId: string): void
}>()

const containerEl = ref<HTMLDivElement | null>(null)
/** 降级态：图不可用，渲染结构化列表（数据不丢） */
const degraded = ref(false)

const graph = computed(() => buildConvergenceGraph(props.item, props.detail ?? null))

interface G6Instance {
  render?: () => Promise<unknown>
  setData?: (d: unknown) => void
  on?: (ev: string, cb: (e: unknown) => void) => void
  destroy?: () => void
}

let inst: G6Instance | null = null
let failed = false

const SIZE: Record<ConvergenceGraphNode['kind'], number> = {
  anchor: 46,
  dim: 34,
  support: 22,
  person: 26,
}

/** 无 canvas（SSR / 测试环境 / 浏览器异常）不得静默空白 */
function hasCanvas(): boolean {
  try {
    if (typeof document === 'undefined') return false
    const c = document.createElement('canvas')
    return !!(c.getContext && c.getContext('2d'))
  } catch {
    return false
  }
}

function sizeOf(n: ConvergenceGraphNode): number {
  return SIZE[n.kind] ?? 24
}

function toDatum(): unknown {
  return {
    nodes: graph.value.nodes.map((n) => ({
      id: n.id,
      data: {
        label: n.sub ? `${n.label}\n${n.sub}` : n.label,
        color: n.color,
        size: sizeOf(n),
        // R-2：重名锚点虚线边框 + 警示描边
        stroke: n.kind === 'anchor' && n.ambiguous ? '#d89614' : 'transparent',
        lineWidth: n.kind === 'anchor' && n.ambiguous ? 3 : 0,
        lineDash: n.kind === 'anchor' && n.ambiguous ? [4, 3] : undefined,
        obs: n.observationId ?? '',
      },
    })),
    edges: graph.value.edges.map((e, i) => ({
      id: `e${i}`,
      source: e.source,
      target: e.target,
      // R-1：线型 = 精度档，线宽 = 权重，色 = 精度色
      data: { color: e.color, width: e.width, dashed: e.dashed, label: e.label ?? '' },
    })),
  }
}

function fail(): void {
  if (failed) return
  failed = true
  try {
    inst?.destroy?.()
  } catch {
    // ignore
  }
  inst = null
  degraded.value = true
  emit('fallback')
}

function onNodeClick(ev: unknown): void {
  const id =
    (ev as { target?: { id?: string } })?.target?.id ??
    (ev as { id?: string })?.id ??
    ''
  const node = graph.value.nodes.find((n) => n.id === id)
  if (node?.observationId) emit('open-observation', node.observationId)
}

async function mount(): Promise<void> {
  if (failed || !containerEl.value) return
  if (!hasCanvas()) {
    fail()
    return
  }
  let GraphCtor: new (cfg: unknown) => G6Instance
  try {
    const mod = await import('@antv/g6')
    GraphCtor = mod.Graph as unknown as new (cfg: unknown) => G6Instance
  } catch {
    fail()
    return
  }
  try {
    inst = new GraphCtor({
      container: containerEl.value,
      autoResize: true,
      autoFit: 'view',
      padding: 18,
      data: toDatum(),
      // 星型层级：以锚点为焦点向外辐射
      layout: {
        type: 'radial',
        focusNode: 'anchor',
        unitRadius: 68,
        linkDistance: 130,
        preventOverlap: true,
        nodeSize: 30,
      },
      node: {
        style: {
          size: (d: { data?: { size?: number } }) => d.data?.size ?? 24,
          fill: (d: { data?: { color?: string } }) => d.data?.color ?? '#6e9fc1',
          stroke: (d: { data?: { stroke?: string } }) => d.data?.stroke ?? 'transparent',
          lineWidth: (d: { data?: { lineWidth?: number } }) => d.data?.lineWidth ?? 0,
          lineDash: (d: { data?: { lineDash?: number[] } }) => d.data?.lineDash,
          labelText: (d: { data?: { label?: string } }) => d.data?.label ?? '',
          labelFill: '#e8eef4',
          labelFontSize: 11,
          labelPlacement: 'bottom',
        },
      },
      edge: {
        style: {
          stroke: (d: { data?: { color?: string } }) => d.data?.color ?? '#3a5a72',
          lineWidth: (d: { data?: { width?: number } }) => d.data?.width ?? 1,
          lineDash: (d: { data?: { dashed?: boolean } }) =>
            d.data?.dashed ? [4, 3] : undefined,
          endArrow: true,
          labelText: (d: { data?: { label?: string } }) => d.data?.label ?? '',
          labelFill: '#8aa5b8',
          labelFontSize: 10,
        },
      },
      behaviors: ['drag-canvas', 'zoom-canvas'],
    })
    inst.on?.('node:click', onNodeClick)
    await inst.render?.()
  } catch {
    fail()
  }
}

function dimNode(dim: string): ConvergenceGraphNode | undefined {
  return graph.value.nodes.find((n) => n.id === `dim:${dim}`)
}

function dimEdge(dim: string): ConvergenceGraphEdge | undefined {
  return graph.value.edges.find(
    (e) => e.source === 'anchor' && e.target === `dim:${dim}`,
  )
}

function supportsOf(dim: string): ConvergenceGraphNode[] {
  return graph.value.nodes.filter((n) => n.kind === 'support' && n.dim === dim)
}

function people(): ConvergenceGraphNode[] {
  return graph.value.nodes.filter((n) => n.kind === 'person')
}

onMounted(() => {
  void mount()
})

watch(
  () => [props.item.key, props.detail],
  () => {
    if (failed || !inst) return
    try {
      inst.setData?.(toDatum())
      void inst.render?.()
    } catch {
      fail()
    }
  },
)

onBeforeUnmount(() => {
  try {
    inst?.destroy?.()
  } catch {
    // ignore
  }
  inst = null
})
</script>

<template>
  <div class="cg">
    <div class="cg-head">
      <span class="cg-title">证据构成</span>
      <span class="cg-hint">
        点击支撑节点可跳回观察档案；拖动/滚轮可平移缩放
      </span>
    </div>

    <!-- 图：单锚点星型。降级时整块替换为下面的结构化列表 -->
    <div
      v-if="!degraded"
      ref="containerEl"
      class="cg-canvas"
      data-testid="cg-canvas"
    />

    <!-- 降级列表：构图数据完备，只是换一种呈现；不得空白 -->
    <div v-else class="cg-fallback" data-testid="cg-fallback">
      <p class="cg-fbnote">图渲染不可用，已降级为结构化列表（数据未丢）：</p>
      <ul class="cg-tree">
        <li class="cg-anchor">
          <span class="cg-badge" :class="{ 'cg-badge--amb': item.person_ambiguous }">
            {{ item.person_ambiguous ? '待裁决' : '锚点' }}
          </span>
          {{ (item.person_names || []).join('、') || item.person_key }} ·
          {{ item.date }}
        </li>
        <li v-for="dim in DIM_ORDER" :key="dim" class="cg-dimrow">
          <div class="cg-dimhead">
            <span class="cg-dot" :style="{ background: dimNode(dim)?.color }" />
            <b>{{ DIM_LABEL[dim] }}</b>
            <span v-if="dimNode(dim)?.count" class="cg-cnt">
              {{ dimNode(dim)!.count }} 条
            </span>
            <span class="cg-prec">{{ dimNode(dim)?.sub || '未命中' }}</span>
            <span
              class="cg-line"
              :class="{ 'cg-line--solid': dimEdge(dim) && !dimEdge(dim)!.dashed }"
              :style="{ borderColor: dimEdge(dim)?.color }"
              :title="
                dimEdge(dim)?.dashed
                  ? '虚线：日期级，仅同地异时，非同时'
                  : '实线：时刻级，可判时间窗重叠'
              "
            />
          </div>
          <p v-if="!supportsOf(dim).length" class="cg-miss">
            {{ dimNode(dim)?.missReason || '该维度无支撑观察' }}
          </p>
          <ul v-else class="cg-sup">
            <li v-for="s in supportsOf(dim)" :key="s.id">
              <a
                v-if="s.observationId"
                class="cg-link"
                href="javascript:void(0)"
                @click.stop="emit('open-observation', s.observationId!)"
              >
                {{ s.label }}
              </a>
              <span v-else>{{ s.label }}</span>
            </li>
          </ul>
        </li>
        <li v-for="p in people()" :key="p.id" class="cg-person">
          同现主体：{{ p.label }}
          <span class="cg-note2">（仅陈述空间/时间接近，不构成接触结论）</span>
        </li>
      </ul>
    </div>

    <!-- 图例：口径常驻，不只降级时可见 -->
    <ul class="cg-legend" data-testid="cg-legend">
      <li v-for="(l, i) in GRAPH_LEGEND" :key="i">
        <span
          class="cg-lgl"
          :class="{ 'cg-lgl--dashed': l.dashed }"
          :style="{ background: l.color }"
        />
        {{ l.text }}
      </li>
    </ul>
    <p class="cg-note">{{ graph.note }}</p>
  </div>
</template>

<style scoped>
.cg {
  margin: 6px 0 10px;
}
.cg-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}
.cg-title {
  font-size: 12px;
  font-weight: 600;
}
.cg-hint {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.cg-canvas {
  width: 100%;
  height: 360px;
  border: 1px solid var(--sun-border, #e5e5e5);
  border-radius: 6px;
  background: var(--sun-canvas-bg, #1b2733);
}
.cg-fallback {
  border: 1px dashed var(--sun-border, #e5e5e5);
  border-radius: 6px;
  padding: 8px 10px;
}
.cg-fbnote {
  margin: 0 0 6px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.cg-tree,
.cg-sup {
  list-style: none;
  margin: 0;
  padding: 0;
}
.cg-anchor {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 6px;
}
.cg-badge {
  font-size: 10px;
  border-radius: 3px;
  padding: 1px 5px;
  margin-right: 4px;
  background: var(--sun-border, #e5e5e5);
}
.cg-badge--amb {
  background: var(--sun-warning, #d89614);
  color: #fff;
}
.cg-dimrow {
  padding-left: 10px;
  border-left: 2px solid var(--sun-border, #e5e5e5);
  margin-bottom: 6px;
}
.cg-dimhead {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.cg-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.cg-cnt {
  font-variant-numeric: tabular-nums;
}
.cg-prec {
  color: var(--sun-text-tertiary);
}
.cg-line {
  width: 26px;
  border-top: 2px dashed currentColor;
}
.cg-line--solid {
  border-top-style: solid;
  border-top-width: 3px;
}
.cg-miss {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.cg-sup {
  margin: 3px 0 0;
  padding-left: 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.cg-link {
  font-size: 11px;
  color: var(--sun-primary, #2080f0);
  text-decoration: none;
}
.cg-link:hover {
  text-decoration: underline;
}
.cg-person {
  margin-top: 6px;
  font-size: 11px;
}
.cg-note2 {
  color: var(--sun-text-tertiary);
}
.cg-legend {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 10px;
  color: var(--sun-text-tertiary);
}
.cg-legend li {
  display: flex;
  align-items: center;
  gap: 4px;
}
.cg-lgl {
  width: 18px;
  height: 3px;
  border-radius: 2px;
  display: inline-block;
}
.cg-lgl--dashed {
  height: 0;
  border-top: 2px dashed currentColor;
  background: none !important;
  opacity: 0.75;
}
.cg-note {
  margin: 6px 0 0;
  font-size: 10px;
  line-height: 1.55;
  color: var(--sun-text-tertiary);
}
</style>
