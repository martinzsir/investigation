<script setup lang="ts">
// P2 假设窗口（hypothesis → hypothesis，PRD 功能 3）。
// 数据：画布内挂到本假设的「支撑/反驳」结论清单 + 覆盖缺口提示
// （单向证据——只有支撑没有反驳或反之——显式提示，不替用户下结论）。
import { computed } from 'vue'
import type { CanvasDoc, CanvasNode } from '../../../domain/canvas'

const props = defineProps<{
  node: CanvasNode
  doc: CanvasDoc
}>()

const byId = computed(() => new Map(props.doc.nodes.map((n) => [n.id, n])))

interface EvidenceItem {
  edgeId: string
  rel: string
  resultId: string
  resultLabel: string
}

function collect(rel: string): EvidenceItem[] {
  return props.doc.edges
    .filter((e) => e.target === props.node.id && e.rel === rel)
    .flatMap((e) => {
      const s = byId.value.get(e.source)
      if (!s) return []
      return [{
        edgeId: e.id,
        rel,
        resultId: s.id,
        resultLabel: s.label,
      }]
    })
}

const supports = computed(() => collect('支撑'))
const refutes = computed(() => collect('反驳'))
const hasAny = computed(() => supports.value.length > 0 || refutes.value.length > 0)
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
      尚无支撑/反驳证据挂接
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
.hyw-sup { border-left: 3px solid #35a374; }
.hyw-ref { border-left: 3px solid #d03050; }
.hyw-rel { flex: none; font-weight: 700; }
.hyw-sup .hyw-rel { color: #1d7d58; }
.hyw-ref .hyw-rel { color: #c22550; }
.hyw-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sun-text-primary, #0a1b36);
}
.hyw-none, .hyw-gap { margin: 0; font-size: 12px; color: var(--sun-text-tertiary, #7c8aa5); }
.hyw-gap { color: #8a5a00; }
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
