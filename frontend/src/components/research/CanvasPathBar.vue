<script setup lang="ts">
// 焦点路径条（P1）：把「层层挖掘」的来路显式化——
// 全部 › 假设 › 事实 › 数据行，每一段可点回退 / 跳转。
// 纯展示 + 事件上抛；轨迹与焦点状态在 ResearchCanvas。
import { computed } from 'vue'

export interface PathSegment {
  id: string
  label: string
  /** 类型中文单字（规/实/体/行/档/核/证/假/备/查） */
  glyph: string
  chip: string
  ink: string
}

const props = withDefaults(
  defineProps<{
    trail: PathSegment[]
    currentId?: string | null
    /** 最多显示几段（其余折叠为「另有 N 层」） */
    maxVisible?: number
  }>(),
  { currentId: null, maxVisible: 4 },
)

const emit = defineEmits<{
  (e: 'jump', id: string): void
  (e: 'reset'): void
}>()

const hiddenCount = computed(() =>
  Math.max(0, props.trail.length - props.maxVisible),
)

/** 溢出时只保留最近 maxVisible 段（前面的层用计数代替，不再单列） */
const shown = computed<PathSegment[]>(() =>
  props.trail.slice(-props.maxVisible),
)
</script>

<template>
  <div class="pathbar" data-testid="canvas-pathbar">
    <button
      type="button"
      class="seg seg-root"
      data-testid="pathbar-root"
      @click="emit('reset')"
    >
      全部
    </button>
    <span v-if="hiddenCount > 0" class="ellipsis">… 另有 {{ hiddenCount }} 层</span>
    <template v-for="item in shown" :key="item.id">
      <span class="sep">›</span>
      <button
        type="button"
        class="seg"
        :class="{ on: item.id === currentId }"
        :data-testid="`pathbar-${item.id}`"
        :title="item.label"
        @click="emit('jump', item.id)"
      >
        <span class="chip" :style="{ background: item.chip, color: item.ink }">
          {{ item.glyph }}
        </span>
        <span class="label">{{ item.label }}</span>
      </button>
    </template>
    <span v-if="currentId" class="tail-hint">Esc 或点空白处返回全局</span>
  </div>
</template>

<style scoped>
.pathbar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  max-width: calc(100% - 280px);
  padding: 4px 10px;
  border: 1px solid var(--sun-border);
  border-radius: 999px;
  background: rgba(5, 21, 34, 0.92);
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.seg {
  appearance: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 168px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--sun-text-secondary);
  font-size: 12px;
  padding: 1px 8px 1px 2px;
  cursor: pointer;
}
.seg:hover {
  border-color: var(--sun-border-active);
  color: var(--sun-text-primary);
}
.seg.on {
  border-color: var(--sun-border-active);
  color: var(--sun-text-primary);
  background: rgba(110, 222, 233, 0.12);
}
.seg-root {
  padding: 1px 10px;
}
.chip {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  flex: none;
}
.label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sep {
  color: var(--sun-text-tertiary);
}
.ellipsis,
.tail-hint {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
</style>
