<script setup lang="ts">
// UX P0 画布图例：十类节点中文释义、五维色点、两种连线语义；
// 兼作视图开关（简洁/完整 + 实体/数据行/数据源三层强制显示）。
// 纯展示+事件上抛，状态与持久化在 ResearchCanvas。
import { computed } from 'vue'
import { KIND_LABELS, KIND_ORDER, type NodeKind } from '../../domain/canvas'
import type { DetailLayerKind, ViewMode } from '../../domain/canvas-view'
import { canvasTokens, colors } from '../../design/tokens'

const props = defineProps<{
  viewMode: ViewMode
  layers: Record<DetailLayerKind, boolean>
  /** 收起态：只留胶囊按钮，避免遮挡右侧数据源列 */
  collapsed?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:view-mode', mode: ViewMode): void
  (e: 'toggle-layer', layer: DetailLayerKind): void
  (e: 'update:collapsed', collapsed: boolean): void
}>()

const kindItems = computed(() =>
  KIND_ORDER.map((kind: NodeKind) => ({
    kind,
    glyph: KIND_GLYPH[kind],
    label: KIND_LABELS[kind],
    chip: canvasTokens.kind[kind].chip,
    ink: canvasTokens.kind[kind].ink,
  })),
)

const KIND_GLYPH: Record<NodeKind, string> = {
  rule: '规',
  fact: '实',
  object: '体',
  source_row: '行',
  source_file: '档',
  verify_item: '核',
  evidence: '证',
  hypothesis: '假',
  note: '备',
  function_result: '查',
}

const jianItems = [
  { label: '资金', color: colors.jian.fund },
  { label: '通讯', color: colors.jian.comms },
  { label: '行为', color: colors.jian.behavior },
  { label: '关系', color: colors.jian.relation },
  { label: '时间', color: colors.jian.time },
]

const layerItems: Array<{ layer: DetailLayerKind; label: string }> = [
  { layer: 'object', label: '实体' },
  { layer: 'source_row', label: '数据行' },
  { layer: 'source_file', label: '数据源' },
]

function toggleView(mode: ViewMode): void {
  if (props.viewMode !== mode) emit('update:view-mode', mode)
}
</script>

<template>
  <!-- 单根：父级传入的 class/data-testid 才能正常落到容器上 -->
  <div
    class="legend-wrap"
    :class="{ 'legend-wrap--collapsed': collapsed }"
    data-testid="canvas-legend-panel"
  >
    <button
      v-if="collapsed"
      type="button"
      class="pill-btn"
      data-testid="legend-collapse"
      @click="emit('update:collapsed', false)"
    >
      图例
    </button>

    <div v-else class="legend">
      <div class="legend-head">
        <span class="legend-title">图例</span>
        <button
          type="button"
          class="collapse-btn"
          data-testid="legend-collapse"
          @click="emit('update:collapsed', true)"
        >
          收起
        </button>
      </div>

      <div class="seg">
        <button
          type="button"
          class="seg-btn"
          :class="{ on: viewMode === 'compact' }"
          data-testid="legend-view-compact"
          @click="toggleView('compact')"
        >
          简洁
        </button>
        <button
          type="button"
          class="seg-btn"
          :class="{ on: viewMode === 'full' }"
          data-testid="legend-view-full"
          @click="toggleView('full')"
        >
          完整
        </button>
      </div>

      <div class="hint">双击节点或点 +/− 展开明细</div>

      <div v-if="viewMode === 'compact'" class="layers">
        <button
          v-for="item in layerItems"
          :key="item.layer"
          type="button"
          class="layer-chip"
          :class="{ on: layers[item.layer] }"
          :data-testid="`legend-layer-${item.layer}`"
          @click="emit('toggle-layer', item.layer)"
        >
          {{ item.label }}
        </button>
      </div>

      <div class="section-title">节点类型</div>
      <div class="kinds">
        <div v-for="item in kindItems" :key="item.kind" class="kind-row">
          <span
            class="chip"
            :style="{ background: item.chip, color: item.ink }"
          >{{ item.glyph }}</span>
          <span class="kind-label">{{ item.label }}</span>
        </div>
      </div>

      <div class="section-title">研判五维</div>
      <div class="jians">
        <span v-for="j in jianItems" :key="j.label" class="jian-item">
          <span class="dot" :style="{ background: j.color }" />
          {{ j.label }}
        </span>
      </div>

      <div class="section-title">连线</div>
      <div class="line-row">
        <svg width="34" height="10" aria-hidden="true">
          <line
            x1="2" y1="5" x2="32" y2="5"
            :stroke="canvasTokens.edgeSystem"
            stroke-width="1.6"
          />
        </svg>
        <span>系统溯源</span>
      </div>
      <div class="line-row">
        <svg width="34" height="10" aria-hidden="true">
          <line
            x1="2" y1="5" x2="32" y2="5"
            :stroke="canvasTokens.edgeManual"
            stroke-width="1.6"
            stroke-dasharray="5 3"
          />
        </svg>
        <span>人工研判</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.legend {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: rgba(5, 21, 34, 0.92);
  backdrop-filter: blur(4px);
  /* 11px + 三级色在深色底上低于 AA：正文提到 12px 二级色 */
  font-size: 12px;
  color: var(--sun-text-secondary);
  pointer-events: auto;
  max-height: 556px;
  overflow-y: auto;
}
.legend-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.legend-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-primary);
}
.collapse-btn,
.pill-btn {
  appearance: none;
  border: 1px solid var(--sun-border);
  border-radius: 999px;
  background: transparent;
  color: var(--sun-text-secondary);
  font-size: 11px;
  padding: 2px 9px;
  cursor: pointer;
}
.collapse-btn:hover,
.pill-btn:hover {
  border-color: var(--sun-border-active);
  color: var(--sun-border-active);
}
/* 收起态：只留胶囊，容器不再画面板底 */
.legend-wrap--collapsed {
  padding: 0;
  border: 0;
  background: transparent;
}
.seg {
  display: inline-flex;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  overflow: hidden;
  align-self: flex-start;
}
.seg-btn {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--sun-text-secondary);
  font-size: 12px;
  padding: 3px 12px;
  cursor: pointer;
}
.seg-btn.on {
  background: rgba(110, 222, 233, 0.14);
  color: var(--sun-border-active);
}
.hint {
  color: var(--sun-text-tertiary);
  font-size: 11px;
}
.layers {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
}
.layer-chip {
  appearance: none;
  border: 1px solid var(--sun-border);
  border-radius: 999px;
  background: transparent;
  color: var(--sun-text-secondary);
  font-size: 11px;
  padding: 2px 9px;
  cursor: pointer;
}
.layer-chip.on {
  border-color: var(--sun-border-active);
  color: var(--sun-border-active);
  background: rgba(110, 222, 233, 0.1);
}
.section-title {
  margin-top: 2px;
  font-size: 11px;
  color: var(--sun-text-secondary);
  letter-spacing: 0.5px;
}
/* 单列：12px 下双列会把十类节点压成密集小字，与卡片 chip 对不上 */
.kinds {
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px;
}
.kind-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.chip {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  flex: none;
}
.kind-label {
  white-space: nowrap;
}
.jians {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}
.jian-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}
.line-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
</style>
