<script setup lang="ts">
// ★ FE-C-017 雷达（侦查维度）：N 通道命中数多边形 + 双阈值环。
// 环 r=1/3 观察线、r=2/3 线索线、r=1 候选线（3 独立维升格「可立案依据候选」）；
// 轴色板见 tokens jianRoomVar（FE-D-007）。维度集/数量/等级名均声明化（R5）：
// angleOf/ring/顶点数全部按 dimensions 长度 N 计算，不再写死五边形。
import { computed } from 'vue'
import { crossLevel, dimensionRooms } from '../../domain/clue'
import { jianRoomVar } from '../../design/tokens'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'

const props = defineProps<{
  /** 每通道命中条数（≥0），key 为侦查维度中文名 */
  rooms: Partial<Record<string, number>>
  size?: number
}>()

const { config: cfg } = useCaseOntologyConfig()

const CX = 160
const CY = 150
const R = 105

/** 维度轴（名称/数量取 dimensions 声明，默认五维） */
const axes_ = computed(() => dimensionRooms(cfg.value))
const nAxes = computed(() => Math.max(1, axes_.value.length))

function angleOf(i: number): number {
  // 顶点起（资金在正上方），顺时针；N 由声明决定
  return -Math.PI / 2 + (i * 2 * Math.PI) / nAxes.value
}
function point(i: number, ratio: number): { x: number; y: number } {
  const a = angleOf(i)
  return { x: CX + R * ratio * Math.cos(a), y: CY + R * ratio * Math.sin(a) }
}
function ring(ratio: number): string {
  return axes_.value.map((_, i) => {
    const p = point(i, ratio)
    return `${p.x.toFixed(1)},${p.y.toFixed(1)}`
  }).join(' ')
}

const levelDecl = computed(() =>
  cfg.value.cross_levels
    .slice()
    .sort((a, b) => a.min_independent_sources - b.min_independent_sources),
)

const gridRings = computed(() =>
  levelDecl.value.map((l) => ({
    ratio: l.min_independent_sources / 3,
    label: l.name,
    cls:
      l.min_independent_sources === 3 ? 'ring-candidate'
        : l.min_independent_sources === 2 ? 'ring-clue'
          : 'ring-watch',
  })),
)

const axes = computed(() =>
  axes_.value.map((room, i) => {
    const outer = point(i, 1)
    const labelP = point(i, 1.18)
    const v = Math.max(0, props.rooms[room] ?? 0)
    return { room, i, outer, labelP, value: v, color: jianRoomVar(room) }
  }),
)

const dataPoints = computed(() =>
  axes_.value.map((room, i) => {
    const v = Math.min(3, Math.max(0, props.rooms[room] ?? 0))
    return point(i, v / 3)
  }),
)
const dataPoly = computed(() => dataPoints.value.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '))

const hitRooms = computed(() => axes_.value.filter((r) => (props.rooms[r] ?? 0) > 0).length)
const level = computed(() => crossLevel(hitRooms.value, cfg.value))
</script>

<template>
  <div class="radar">
    <svg :width="size ?? 320" :height="(size ?? 320) * 0.94" viewBox="0 0 320 300" role="img" aria-label="侦查维度交叉雷达">
      <!-- 阈值环 -->
      <polygon
        v-for="g in gridRings"
        :key="g.label"
        :points="ring(g.ratio)"
        class="ring"
        :class="g.cls"
      />
      <!-- 轴线 -->
      <line
        v-for="ax in axes"
        :key="`ax-${ax.room}`"
        :x1="CX" :y1="CY"
        :x2="ax.outer.x" :y2="ax.outer.y"
        class="axis-line"
      />
      <!-- 数据多边形 -->
      <polygon :points="dataPoly" class="data-poly" />
      <circle
        v-for="(p, i) in dataPoints"
        :key="`dp-${i}`"
        :cx="p.x" :cy="p.y" r="3.5"
        :fill="jianRoomVar(axes[i].room)"
        class="data-dot"
      />
      <!-- 轴标签 -->
      <text
        v-for="ax in axes"
        :key="`lb-${ax.room}`"
        :x="ax.labelP.x" :y="ax.labelP.y"
        text-anchor="middle" dominant-baseline="middle"
        class="axis-label"
        :style="{ fill: ax.color }"
      >
        {{ ax.room }}·{{ ax.value }}
      </text>
    </svg>
    <div class="radar-legend">
      <span v-for="l in levelDecl" :key="l.name" class="legend-item" :class="`legend-${l.min_independent_sources}`">
        <i class="sw" :class="l.min_independent_sources === 3 ? 'sw-candidate' : l.min_independent_sources === 2 ? 'sw-clue' : 'sw-watch'"></i>
        {{ l.min_independent_sources }} 维 · {{ l.name }}
      </span>
      <span class="radar-level">当前命中 <b>{{ hitRooms }}</b> 维 → <b>{{ level }}</b></span>
    </div>
  </div>
</template>

<style scoped>
.radar {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.ring {
  fill: none;
  stroke-width: 1;
}
.ring-watch {
  stroke: rgba(107, 131, 153, 0.5);
  stroke-dasharray: 4 3;
}
.ring-clue {
  stroke: rgba(0, 212, 255, 0.45);
}
.ring-candidate {
  stroke: rgba(212, 175, 55, 0.6);
}
.axis-line {
  stroke: rgba(16, 49, 74, 0.8);
  stroke-width: 1;
}
.data-poly {
  fill: rgba(110, 222, 233, 0.14);
  stroke: var(--sun-border-active);
  stroke-width: 1.5;
}
.data-dot {
  stroke: rgba(3, 10, 20, 0.9);
  stroke-width: 1;
}
.axis-label {
  font-size: 12px;
  font-weight: 600;
}
.radar-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  justify-content: center;
  font-size: 12px;
  color: var(--sun-text-secondary);
  margin-top: 2px;
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.sw {
  display: inline-block;
  width: 14px;
  height: 2px;
}
.sw-watch {
  background: rgba(107, 131, 153, 0.7);
}
.sw-clue {
  background: rgba(0, 212, 255, 0.8);
}
.sw-candidate {
  background: rgba(212, 175, 55, 0.9);
}
.radar-level {
  color: var(--sun-text-tertiary);
}
.radar-level b {
  color: var(--sun-border-active);
}
</style>
