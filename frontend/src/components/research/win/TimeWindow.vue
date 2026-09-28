<script setup lang="ts">
// P2 时间窗口（event → time，PRD 功能 3）。
// 数据：画布文档内全部带 time_start 的节点投影到水平轨道；本事件高亮，
// 列出时间相邻的前后事件。精度档徽标消费 precisionSymbol（红线 R1：
// minute=实心橙 可判时间窗重叠；date=空心蓝 仅同日异时）。
import { computed } from 'vue'
import { precisionSymbol, parseTimeMs } from '../../../domain/canvas-window'
import { KIND_LABELS, type CanvasDoc, type CanvasNode } from '../../../domain/canvas'

const props = defineProps<{
  node: CanvasNode
  doc: CanvasDoc
}>()

interface TrackPoint {
  id: string
  label: string
  kindLabel: string
  ms: number
  self: boolean
  fillMode: string
  color: string
}

const selfMs = computed(() => parseTimeMs(props.node.props?.time_start))
const precision = computed(() => String(props.node.props?.time_precision ?? ''))
const timeEnd = computed(() => String(props.node.props?.time_end ?? ''))
const timeStartText = computed(() => String(props.node.props?.time_start ?? ''))

/** 画布内全部时间点（含本节点），按时间升序 */
const points = computed<TrackPoint[]>(() => {
  const out: TrackPoint[] = []
  for (const n of props.doc.nodes) {
    const ms = parseTimeMs((n.props as Record<string, unknown> | undefined)?.time_start)
    if (ms === null) continue
    const sym = precisionSymbol(
      (n.props as Record<string, unknown> | undefined)?.time_precision,
    )
    out.push({
      id: n.id,
      label: n.label,
      kindLabel: KIND_LABELS[n.kind] ?? n.kind,
      ms,
      self: n.id === props.node.id,
      fillMode: sym.fillMode,
      color: sym.color,
    })
  }
  return out.sort((a, b) => a.ms - b.ms)
})

const selfIdx = computed(() => points.value.findIndex((p) => p.self))
const hasTrack = computed(() => selfMs.value !== null && points.value.length > 0)

/** 轨道投影（0~1；单点居中） */
function relPos(ms: number): number {
  const list = points.value
  if (list.length < 2) return 0.5
  const min = list[0].ms
  const max = list[list.length - 1].ms
  if (max <= min) return 0.5
  return Math.min(1, Math.max(0, (ms - min) / (max - min)))
}

/** 时间相邻事件（各取 3 条） */
const neighbors = computed<TrackPoint[]>(() => {
  const idx = selfIdx.value
  if (idx < 0) return []
  const out: TrackPoint[] = []
  for (let d = 1; d <= 3; d++) {
    if (idx - d >= 0) out.push(points.value[idx - d])
    if (idx + d < points.value.length) out.push(points.value[idx + d])
  }
  return out.sort((a, b) => a.ms - b.ms)
})

function fmtMs(ms: number): string {
  const d = new Date(ms)
  const p = (v: number): string => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
</script>

<template>
  <div class="timw" data-testid="time-window">
    <template v-if="hasTrack">
      <div class="timw-head">
        <span class="timw-when" data-testid="time-when">{{ timeStartText }}</span>
        <span
          v-if="timeEnd"
          class="timw-span"
          data-testid="time-span"
          >→ {{ timeEnd }}</span
        >
        <span v-if="precision" class="timw-precision" :data-precision="precision">
          {{ precision === 'minute' ? '时刻级（可判时间窗重叠）' : '日期级（仅同日异时）' }}
        </span>
      </div>

      <div class="timw-track" data-testid="time-track">
        <div class="timw-axis" />
        <span
          v-for="p in points"
          :key="p.id"
          class="timw-pt"
          :class="{ 'timw-pt-self': p.self, 'timw-pt-hollow': p.fillMode === 'hollow' }"
          :style="{ left: `${relPos(p.ms) * 100}%`, borderColor: p.color, background: p.fillMode === 'solid' ? p.color : '#fff' }"
          :title="`${p.label}（${p.kindLabel} · ${fmtMs(p.ms)}）`"
        />
      </div>

      <h4 class="timw-sec">时间相邻事件</h4>
      <ul v-if="neighbors.length" class="timw-list">
        <li v-for="p in neighbors" :key="p.id" class="timw-item">
          <span class="timw-date">{{ fmtMs(p.ms) }}</span>
          <span class="timw-label" :title="p.label">{{ p.label }}</span>
          <span class="timw-kind">{{ p.kindLabel }}</span>
        </li>
      </ul>
      <p v-else class="timw-none">轨道上暂无其他带时间节点</p>
    </template>
    <div v-else class="timw-empty" data-testid="time-empty">暂无时间数据</div>
  </div>
</template>

<style scoped>
.timw { display: flex; flex-direction: column; gap: 8px; }
.timw-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.timw-when { font-size: 13px; font-weight: 700; color: var(--sun-text-primary, #0a1b36); }
.timw-span { font-size: 11px; color: var(--sun-text-secondary, #4a5a76); }
.timw-precision { font-size: 11px; color: var(--sun-text-tertiary, #7c8aa5); }
.timw-track {
  position: relative;
  height: 26px;
  margin: 4px 6px;
}
.timw-axis {
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  height: 2px;
  background: #dfe6f0;
  border-radius: 1px;
}
.timw-pt {
  position: absolute;
  top: 50%;
  width: 10px;
  height: 10px;
  border: 2px solid;
  border-radius: 50%;
  transform: translate(-50%, -50%);
  box-sizing: border-box;
}
.timw-pt-self { width: 14px; height: 14px; box-shadow: 0 0 0 3px rgba(255, 112, 67, 0.2); }
.timw-sec {
  margin: 2px 0 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--sun-text-secondary, #4a5a76);
}
.timw-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.timw-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 6px;
}
.timw-date { flex: none; font-family: var(--sun-font-mono, monospace); font-size: 11px; color: var(--sun-text-tertiary, #7c8aa5); }
.timw-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sun-text-primary, #0a1b36);
}
.timw-kind { flex: none; font-size: 11px; color: var(--sun-text-tertiary, #7c8aa5); }
.timw-none { margin: 0; font-size: 12px; color: var(--sun-text-tertiary, #7c8aa5); }
.timw-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 80px;
  font-size: 12px;
  color: var(--sun-text-tertiary, #7c8aa5);
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 8px;
}
</style>
