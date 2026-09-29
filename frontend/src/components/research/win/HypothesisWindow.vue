<script setup lang="ts">
// P2 假设窗口（hypothesis → hypothesis，PRD 功能 3）。
// 数据：画布内挂到本假设的「推断为」结论清单 + 支撑/反驳证据
// （单向证据——只有支撑没有反驳或反之——显式提示，不替用户下结论）。
import { computed } from 'vue'
import type { WindowNode } from '../../../domain/canvas-window'

const props = defineProps<{
  node: WindowNode
  edges: Array<{ id?: string; source: string; target: string; rel?: string; data?: Record<string, unknown> }>
  nodeById?: ReadonlyMap<string, WindowNode>
}>()

const byId = computed(() => props.nodeById ?? new Map<string, WindowNode>())

interface EvidenceItem {
  edgeId: string
  rel: string
  resultId: string
  resultLabel: string
}

function collect(rel: string): EvidenceItem[] {
  return props.edges
    .filter((e) => e.target === props.node.id && e.rel === rel)
    .flatMap((e) => {
      const s = byId.value.get(e.source)
      if (!s) return []
      return [{
        edgeId: e.id ?? `${e.source}-${e.target}`,
        rel,
        resultId: s.id,
        resultLabel: s.label,
      }]
    })
}

// 系统推断边（推断为）：哪些结论节点推断到此假设
const inferences = computed(() => collect('推断为'))
// 人工确认边（支撑/反驳）：正兵认可后的证据方向
const supports = computed(() => collect('支撑'))
const refutes = computed(() => collect('反驳'))
const hasAny = computed(() =>
  inferences.value.length > 0 || supports.value.length > 0 || refutes.value.length > 0)
/** 覆盖缺口：单向证据（有支撑无反驳 / 有反驳无支撑） */
const gap = computed(() => {
  if (supports.value.length > 0 && refutes.value.length === 0)
    return '仅有支撑证据，尚无反驳证据（覆盖缺口）'
  if (supports.value.length === 0 && refutes.value.length > 0)
    return '仅有反驳证据，尚无支撑证据（覆盖缺口）'
  return ''
})
const content = computed(() => String(props.node.props?.content ?? ''))
</script>

<template>
  <div class="hyw" data-testid="hypothesis-window">
    <p v-if="content" class="hyw-content" data-testid="hypothesis-content">
      {{ content }}
    </p>

    <template v-if="hasAny">
      <!-- 系统推断来源（未经人工确认方向） -->
      <h4 v-if="inferences.length" class="hyw-sec">推断来源（{{ inferences.length }}）</h4>
      <ul v-if="inferences.length" class="hyw-list">
        <li v-for="it in inferences" :key="it.edgeId" class="hyw-item hyw-inf">
          <span class="hyw-rel">推断为</span>
          <span class="hyw-label" :title="it.resultLabel">{{ it.resultLabel }}</span>
        </li>
      </ul>
      <p v-if="inferences.length" class="hyw-hint">系统推断，方向（证实/查否）待人工确认</p>

      <h4 class="hyw-sec">支撑（{{ supports.length }}）</h4>
      <ul v-if="supports.length" class="hyw-list">
        <li v-for="it in supports" :key="it.edgeId" class="hyw-item hyw-sup">
          <span class="hyw-rel">支撑</span>
          <span class="hyw-label" :title="it.resultLabel">{{ it.resultLabel }}</span>
        </li>
      </ul>
      <p v-else class="hyw-none">暂无支撑证据</p>

      <h4 class="hyw-sec">反驳（{{ refutes.length }}）</h4>
      <ul v-if="refutes.length" class="hyw-list">
        <li v-for="it in refutes" :key="it.edgeId" class="hyw-item hyw-ref">
          <span class="hyw-rel">反驳</span>
          <span class="hyw-label" :title="it.resultLabel">{{ it.resultLabel }}</span>
        </li>
      </ul>
      <p v-else class="hyw-none">暂无反驳证据</p>

      <p v-if="gap" class="hyw-gap" data-testid="hypothesis-gap">{{ gap }}</p>
    </template>

    <div v-else class="hyw-empty" data-testid="hypothesis-empty">
      尚无推断/支撑/反驳证据挂接
    </div>
  </div>
</template>

<style scoped>
.hyw { display: flex; flex-direction: column; gap: 8px; }
.hyw-content {
  margin: 0;
  padding: 6px 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--sun-text-primary, #0a1b36);
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 6px;
}
.hyw-sec {
  margin: 2px 0 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--sun-text-secondary, #4a5a76);
}
.hyw-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.hyw-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--sun-border, #e5e9f2);
}
.hyw-inf { border-left: 3px solid #6b7c93; }
.hyw-sup { border-left: 3px solid #35a374; }
.hyw-ref { border-left: 3px solid #d03050; }
.hyw-rel { flex: none; font-weight: 700; }
.hyw-inf .hyw-rel { color: #4a5a76; }
.hyw-sup .hyw-rel { color: #1d7d58; }
.hyw-ref .hyw-rel { color: #c22550; }
.hyw-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sun-text-primary, #0a1b36);
}
.hyw-none, .hyw-gap, .hyw-hint { margin: 0; font-size: 12px; color: var(--sun-text-tertiary, #7c8aa5); }
.hyw-gap { color: #8a5a00; }
.hyw-hint { font-size: 11px; font-style: italic; }
.hyw-empty {
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
