<script setup lang="ts">
// 全局缩略图（P2）：放大/拖拽后告诉用户「我在整张图的哪个位置」，点一下跳过去。
//
// G6 v5 不再内置 minimap 插件（v4 有、v5 的 lib/plugins 下没有），
// 挂第二个 Graph 实例只为画个缩略图不划算（双倍渲染 + 双份事件）。
// 这里只做纯映射：内容 bbox → 一个小矩形（等比、letterbox 居中），
// 映射数学全在 domain/canvas-viewport，组件只负责画与收点击。
import { computed, ref } from 'vue'
import {
  CARD_HEIGHT,
  CARD_WIDTH,
  minimapFrame,
  minimapProject,
  minimapUnproject,
  type ContentBox,
  type ViewportRect,
} from '../../domain/canvas-viewport'

export interface MinimapNode {
  id: string
  x: number
  y: number
  /** 卡片类型色（canvasTokens.kind[kind].chip） */
  fill: string
}

const props = withDefaults(
  defineProps<{
    nodes: ReadonlyArray<MinimapNode>
    /** 内容包围盒（画布坐标） */
    box: ContentBox | null
    /** 当前可见区域（画布坐标） */
    view: ViewportRect | null
    width?: number
    height?: number
  }>(),
  { width: 188, height: 120 },
)

const emit = defineEmits<{
  (e: 'pan', point: [number, number]): void
}>()

const rootEl = ref<HTMLDivElement | null>(null)

const frame = computed(() =>
  minimapFrame(props.box, props.width, props.height),
)

/** 节点点阵（卡片等比缩放，下限 2×1.5 保证小比例下仍可见） */
const dots = computed(() => {
  const f = frame.value
  if (!f) return []
  const w = Math.max(2, CARD_WIDTH * f.scale)
  const h = Math.max(1.5, CARD_HEIGHT * f.scale)
  return props.nodes
    .filter((n) => Number.isFinite(n.x) && Number.isFinite(n.y))
    .map((n) => {
      const [px, py] = minimapProject(f, props.box, n.x, n.y)
      return { id: n.id, x: px - w / 2, y: py - h / 2, w, h, fill: n.fill }
    })
})

/** 当前视口框（像素） */
const viewBox = computed(() => {
  const f = frame.value
  if (!f || !props.view) return null
  const [x, y] = minimapProject(f, props.box, props.view.x, props.view.y)
  return {
    x,
    y,
    w: Math.max(props.view.w * f.scale, 6),
    h: Math.max(props.view.h * f.scale, 4),
  }
})

function onPick(ev: MouseEvent): void {
  const el = rootEl.value
  if (!el || !frame.value) return
  const rect = el.getBoundingClientRect()
  const point = minimapUnproject(
    frame.value,
    props.box,
    ev.clientX - rect.left,
    ev.clientY - rect.top,
  )
  emit('pan', point)
}
</script>

<template>
  <div
    ref="rootEl"
    class="minimap"
    :style="{ width: `${width}px`, height: `${height}px` }"
    data-testid="canvas-minimap"
    title="全局缩略图：点击定位"
    @click="onPick"
  >
    <svg
      class="mm-svg"
      :width="width"
      :height="height"
      :viewBox="`0 0 ${width} ${height}`"
      data-testid="canvas-minimap-svg"
    >
      <rect
        v-for="d in dots"
        :key="d.id"
        :x="d.x"
        :y="d.y"
        :width="d.w"
        :height="d.h"
        :fill="d.fill"
        rx="1"
        opacity="0.72"
      />
      <rect
        v-if="viewBox"
        class="mm-view"
        :x="viewBox.x"
        :y="viewBox.y"
        :width="viewBox.w"
        :height="viewBox.h"
      />
    </svg>
    <span class="mm-label">全局缩略</span>
  </div>
</template>

<style scoped>
.minimap {
  position: relative;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: rgba(5, 21, 34, 0.92);
  overflow: hidden;
  cursor: crosshair;
}
.mm-svg {
  display: block;
}
.mm-view {
  fill: rgba(110, 222, 233, 0.1);
  stroke: var(--sun-border-active);
  stroke-width: 1.5;
}
.mm-label {
  position: absolute;
  left: 6px;
  bottom: 3px;
  font-size: 10px;
  color: var(--sun-text-tertiary);
  pointer-events: none;
}
</style>
