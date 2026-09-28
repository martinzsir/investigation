<script setup lang="ts">
/**
 * 三维交汇时间轴视图。
 *
 * 看什么
 * ------
 * **不是**事件流水，而是"哪些日期段三维同时密集"。三条泳道 = 三个维度，
 * 每条锚点在它命中的维度上各出一点；**同一主体同一日三维齐全**则构成一个
 * 簇（cluster），用金色竖条高亮——这才是交汇在时间轴上的表达。
 *
 * 红线（与地图、G6 图、列表面板同口径）
 * --------------------------------------
 * 1. **精度档必须在轴上可见**：日期级（date）画空心点并虚线描边——它只说明
 *    "前后一天先后出现"，是同地异时；时刻级才画实心。否则两者在轴上长得
 *    一样，之前所有精度加权就被一条漂亮的时间轴重新掩盖。
 * 2. **不作定性**：簇只表示"这天三维都有证据"，不说"可疑"。
 *
 * 自绘 SVG 而非引入时间轴库：本视图是三泳道 + 簇高亮的**定制**形态，
 * 与通用 timeline 的 item/group 模型不合；且离线可用、无 DOM 依赖。
 */
import { computed, ref } from 'vue'
import { NEmpty } from 'naive-ui'
import type {
  ConvTimelineCluster,
  ConvTimelineModel,
} from '../../domain/convergence-timeline'

const props = withDefaults(defineProps<{
  model: ConvTimelineModel
  width?: number
}>(), { width: 760 })

const emit = defineEmits<{ (e: 'select', key: string): void }>()

const LANE_H = 40
const PAD_X = 46
const PAD_TOP = 22
const height = computed(() => PAD_TOP + props.model.lanes.length * LANE_H + 24)

const span = computed(() => {
  const s = props.model.domainStart
  const e = props.model.domainEnd
  return e > s ? e - s : 86_400_000 // 单点时给一天，避免除零
})

function xOf(ts: number): number {
  const w = props.width - PAD_X * 2
  return PAD_X + ((ts - props.model.domainStart) / span.value) * w
}

function laneY(i: number): number {
  return PAD_TOP + i * LANE_H + LANE_H / 2
}

/** 日期级 = 空心虚线点（同地异时）；时刻级 = 实心点 */
function isDateLevel(p: string): boolean {
  return p === 'date' || p === 'unknown'
}

const hovered = ref<{ text: string; x: number; y: number } | null>(null)

function onPick(key: string) {
  emit('select', key)
}

function fmtDate(d: string): string {
  return d || '—'
}

const fullClusters = computed<ConvTimelineCluster[]>(() =>
  props.model.clusters.filter((c) => c.full))
</script>

<template>
  <div class="conv-tl">
    <svg
      v-if="model.lanes.some((l) => l.items.length)"
      :viewBox="`0 0 ${width} ${height}`"
      class="tl-svg"
      role="img"
      aria-label="三维交汇时间轴"
    >
      <!-- 三维齐全的簇：金色竖条，跨三泳道 -->
      <rect
        v-for="c in fullClusters"
        :key="`c-${c.personKey}-${c.date}`"
        :x="xOf(c.ts) - 7"
        :y="PAD_TOP - 6"
        :width="14"
        :height="model.lanes.length * LANE_H"
        class="cluster-bar"
      />

      <!-- 泳道 -->
      <g v-for="(lane, i) in model.lanes" :key="lane.dim">
        <line
          :x1="PAD_X" :y1="laneY(i)"
          :x2="width - PAD_X" :y2="laneY(i)"
          class="axis"
        />
        <text :x="6" :y="laneY(i) + 4" class="lane-lbl" :fill="lane.color">
          {{ lane.label }}
        </text>
        <g v-for="it in lane.items" :key="`${lane.dim}-${it.key}`">
          <circle
            :cx="xOf(it.ts)" :cy="laneY(i)"
            :r="it.dimHit >= 3 ? 6 : it.dimHit === 2 ? 5 : 4"
            :class="['dot', isDateLevel(it.precision) ? 'hollow' : 'solid',
                     { amb: it.ambiguous }]"
            :style="{ stroke: lane.color }"
            @click="onPick(it.key)"
            @mouseenter="hovered = { text: `${it.date} ${it.personLabel} · ${it.count} 条 · ${it.precision} 档`, x: xOf(it.ts), y: laneY(i) }"
            @mouseleave="hovered = null"
          />
          <title>{{ it.date }} {{ it.personLabel }}｜{{ it.count }} 条｜{{ it.precision }} 档</title>
        </g>
      </g>

      <!-- 时间刻度（首尾） -->
      <text :x="PAD_X" :y="height - 6" class="tick">
        {{ fmtDate(model.lanes.flatMap((l) => l.items).map((i) => i.date).sort()[0]) }}
      </text>
      <text :x="width - PAD_X" :y="height - 6" class="tick" text-anchor="end">
        {{ fmtDate(model.lanes.flatMap((l) => l.items).map((i) => i.date).sort().slice(-1)[0]) }}
      </text>
    </svg>
    <NEmpty v-else description="无带日期的交汇锚点，时间轴不可用" />

    <div v-if="hovered" class="tip">{{ hovered.text }}</div>

    <div class="legend">
      <span class="lg"><i class="sw solid" />时刻级（可判时间窗重叠）</span>
      <span class="lg"><i class="sw hollow" />日期级（仅同地异时，非同时）</span>
      <span class="lg"><i class="sw bar" />同一主体同一日三维齐全（真交汇）</span>
      <span v-if="model.undated" class="lg dim">{{ model.undated }} 条锚点无日期，未入轴</span>
    </div>

    <div v-if="fullClusters.length" class="clusters">
      <div class="h">三维齐全的日期（{{ fullClusters.length }}）</div>
      <div v-for="c in fullClusters" :key="`l-${c.personKey}-${c.date}`" class="row">
        <span class="date">{{ c.date }}</span>
        <span class="who">{{ c.personLabel }}</span>
        <span class="dim">{{ c.addresses.join('、') }}｜最低精度 {{ c.minPrecision }} 档</span>
        <button class="go" @click="onPick(c.convKeys[0])">查看</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.conv-tl { display: flex; flex-direction: column; }
.tl-svg { width: 100%; height: auto; background: #fafbfc; border: 1px solid #e5e7eb; border-radius: 6px; }
.axis { stroke: #e5e7eb; stroke-width: 1; }
.cluster-bar { fill: rgba(240, 160, 32, 0.18); stroke: #f0a020; stroke-width: 1; }
.lane-lbl { font-size: 11px; font-weight: 600; }
.dot { cursor: pointer; stroke-width: 1.5; fill: #fff; }
.dot.solid { fill-opacity: 1; }
.dot.hollow { fill: #fff; stroke-dasharray: 3 2; }
.dot.amb { stroke-dasharray: 2 2; }
.dot:hover { stroke-width: 2.5; }
.tick { font-size: 10px; fill: #999; }
.tip { margin-top: 6px; font-size: 12px; background: #f3f4f6; padding: 4px 8px; border-radius: 4px; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; font-size: 12px; color: #555; }
.lg { display: inline-flex; align-items: center; gap: 4px; }
.dim { color: #999; }
.sw { width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid #666; display: inline-block; }
.sw.solid { background: #666; }
.sw.hollow { background: #fff; border-style: dashed; }
.sw.bar { border: none; border-radius: 2px; background: rgba(240,160,32,0.5); height: 12px; width: 6px; }
.clusters { margin-top: 10px; border-top: 1px dashed #e5e7eb; padding-top: 8px; }
.clusters .h { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.clusters .row { font-size: 12px; display: flex; gap: 8px; align-items: center; padding: 3px 0; }
.date { color: #b4781a; font-weight: 600; }
.who { color: #333; }
.go { border: 1px solid #d9d9d9; background: #fff; border-radius: 4px; font-size: 11px; padding: 1px 6px; cursor: pointer; }
</style>
