<script setup lang="ts">
// P2 节点窗口式呈现——贴附浮窗容器（PRD V1.0.0 功能 3 / REQ-P2 窗口框架）。
// 职责仅容器：贴附定位（domain/windowPosition 纯函数：右侧默认、越界翻左、
// 垂直 clamp）、loading 骨架屏（可取消）、error 态、钉住角标、放大/关闭按钮。
// 分型内容由父组件按 windowFor(kind) 以默认插槽注入（win/*.vue）。
// 关闭（Esc/点空白/再点）与多窗口互斥、竞态取消由宿主（CanvasView）编排。
import { computed } from 'vue'
import { NButton, NIcon, NSpin } from 'naive-ui'
import {
  CloseOutline,
  PinOutline,
  ResizeOutline,
} from '@vicons/ionicons5'
import {
  windowPosition,
  type WindowKind,
} from '../../domain/canvas-window'

const props = defineProps<{
  windowKind: WindowKind
  title: string
  /** 节点中心在画布容器内的像素坐标（宿主随视口变化回传，实现跟随） */
  anchorX: number
  anchorY: number
  viewportW: number
  viewportH: number
  pinned: boolean
  loading: boolean
  /** 非空=窗口内错误态（宿主提供重试） */
  error?: string
  /** 放大目标路由（空=不渲染放大按钮，如假设窗口） */
  enlargeTo?: string
  width?: number
}>()

const emit = defineEmits<{
  close: []
  'toggle-pin': []
  enlarge: []
  cancel: []
  retry: []
}>()

/** 窗口分型中文标签（窗口头部） */
const KIND_LABELS: Record<WindowKind, string> = {
  relation: '关系',
  map: '地图',
  time: '时间',
  evidence: '证据',
  hypothesis: '假设',
  source: '溯源',
}

const WIN_DEFAULT_W = 300
/** 定位用高度估计（内容自适应渲染，仅影响垂直 clamp 精度） */
const WIN_EST_H = 320
const NARROW = 340

const width = computed(() => props.width ?? WIN_DEFAULT_W)
/** 视口过窄时窗口直接全宽（移动/分屏兜底） */
const narrow = computed(() => props.viewportW > 0 && props.viewportW < NARROW)
const effectiveW = computed(() =>
  narrow.value ? Math.max(160, props.viewportW - 8) : width.value)

const pos = computed(() =>
  windowPosition({
    anchorX: props.anchorX,
    anchorY: props.anchorY,
    viewportW: props.viewportW,
    viewportH: props.viewportH,
    winW: effectiveW.value,
    winH: WIN_EST_H,
  }))

const style = computed(() => ({
  left: `${pos.value.x}px`,
  top: `${pos.value.y}px`,
  width: `${effectiveW.value}px`,
}))
</script>

<template>
  <section
    class="cnw"
    :class="[`cnw-${windowKind}`, { 'cnw-pinned': pinned }]"
    :style="style"
    :data-testid="`node-window-${windowKind}`"
    :data-side="pos.side"
    @mousedown.stop
  >
    <header class="cnw-head">
      <span class="cnw-kind">{{ KIND_LABELS[windowKind] }}窗口</span>
      <span class="cnw-title" :title="title">{{ title }}</span>
      <span class="cnw-spacer" />
      <NButton
        v-if="enlargeTo"
        quaternary
        size="tiny"
        title="放大到全局视图"
        data-testid="win-enlarge"
        @click="emit('enlarge')"
      >
        <template #icon><NIcon><ResizeOutline /></NIcon></template>
        放大
      </NButton>
      <NButton
        quaternary
        size="tiny"
        :title="pinned ? '取消钉住' : '钉住窗口'"
        data-testid="win-pin"
        @click="emit('toggle-pin')"
      >
        <template #icon><NIcon><PinOutline /></NIcon></template>
      </NButton>
      <NButton
        quaternary
        size="tiny"
        title="关闭"
        data-testid="win-close"
        @click="emit('close')"
      >
        <template #icon><NIcon><CloseOutline /></NIcon></template>
      </NButton>
    </header>

    <div v-if="pinned" class="cnw-pin-badge" title="已钉住，Esc 不会关闭本窗口">钉</div>

    <div v-if="loading" class="cnw-body cnw-loading" data-testid="win-loading">
      <div class="cnw-skel" v-for="i in 3" :key="i" />
      <NButton size="tiny" quaternary data-testid="win-cancel" @click="emit('cancel')">
        取消加载
      </NButton>
      <NSpin size="small" class="cnw-spin" />
    </div>

    <div v-else-if="error" class="cnw-body cnw-error" data-testid="win-error">
      <p class="cnw-error-text">窗口数据加载失败：{{ error }}</p>
      <NButton size="tiny" @click="emit('retry')">重试</NButton>
    </div>

    <div v-else class="cnw-body" data-testid="win-body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.cnw {
  position: absolute;
  z-index: 30;
  display: flex;
  flex-direction: column;
  max-height: 60%;
  background: var(--sun-surface, #fff);
  border: 1px solid var(--sun-border, #d8dee9);
  border-radius: 10px;
  box-shadow: 0 8px 28px rgba(10, 27, 54, 0.18);
  overflow: hidden;
}
.cnw-pinned {
  border-color: var(--sun-primary, #2f6fed);
  box-shadow: 0 8px 28px rgba(47, 111, 237, 0.28);
}
.cnw-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--sun-border, #e5e9f2);
  background: var(--sun-surface-2, #f6f8fb);
}
.cnw-kind {
  flex: none;
  font-size: 11px;
  font-weight: 700;
  color: var(--sun-primary, #2f6fed);
}
.cnw-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-primary, #0a1b36);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cnw-spacer {
  flex: 1;
}
.cnw-pin-badge {
  position: absolute;
  top: 4px;
  left: -10px;
  z-index: 1;
  padding: 1px 12px;
  font-size: 10px;
  color: #fff;
  background: var(--sun-primary, #2f6fed);
  transform: rotate(-45deg) translateY(6px);
  pointer-events: none;
}
.cnw-body {
  position: relative;
  padding: 10px;
  overflow: auto;
}
.cnw-loading {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 120px;
}
.cnw-skel {
  height: 14px;
  border-radius: 4px;
  background: linear-gradient(90deg, #eef1f6 25%, #e2e8f2 37%, #eef1f6 63%);
  background-size: 400% 100%;
  animation: cnw-skel 1.2s ease infinite;
}
.cnw-skel:nth-child(2) { width: 78%; }
.cnw-skel:nth-child(3) { width: 55%; }
@keyframes cnw-skel {
  0% { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}
.cnw-spin {
  position: absolute;
  right: 12px;
  bottom: 10px;
}
.cnw-error {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.cnw-error-text {
  margin: 0;
  font-size: 12px;
  color: var(--sun-danger, #d03050);
}
</style>
