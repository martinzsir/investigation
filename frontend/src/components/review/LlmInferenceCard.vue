<script setup lang="ts">
// ★ FE-C-023 LlmInferenceCard：LLM 判读三件套——结论 / 溯源 / 模型归属，缺一不渲染。
// 置信度色阶（≥.85 青 / .60–.84 琥珀 / <.60 红）；归属间标签明示信号来源，
// 是 FE-T-007「LLM 同源去重」的展示载体：同模型多条判读共享同一归属标记。
import { computed } from 'vue'
import { completeLlmInference, confidenceBand, type LlmInference } from '../../domain/review'
import StatusBadge from '../common/StatusBadge.vue'

const props = defineProps<{ inference: LlmInference }>()

const complete = computed(() => completeLlmInference(props.inference))
const band = computed(() =>
  props.inference.confidence === undefined ? 'warn' : confidenceBand(props.inference.confidence),
)
const pct = computed(() =>
  props.inference.confidence === undefined ? '—' : `${Math.round(props.inference.confidence * 100)}%`,
)
</script>

<template>
  <!-- 三件套缺一：组件整体不渲染（无主推断/无溯源推断不上屏） -->
  <div v-if="complete" class="llm-card">
    <div class="llm-head">
      <span class="llm-badge">LLM 判读</span>
      <span class="llm-model">模型 {{ inference.llm_model }}</span>
      <StatusBadge v-if="inference.room" variant="room" :value="inference.room" />
      <span class="llm-conf" :class="`llm-conf--${band}`">置信度 {{ pct }}</span>
    </div>
    <p class="llm-text">{{ inference.text }}</p>
    <details class="llm-src">
      <summary>溯源 {{ inference.source_rows?.length ?? 0 }} 行（推断必挂溯源）</summary>
      <ul>
        <li v-for="r in inference.source_rows" :key="r.row_uri">
          <code>{{ r.row_uri }}</code>
          <span v-if="r.source" class="llm-src-name">{{ r.source }}</span>
        </li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.llm-card {
  border: 1px solid var(--sun-warn-border);
  background: var(--sun-warn-bg);
  border-radius: 6px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.llm-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.llm-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 12px;
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  background: color-mix(in srgb, var(--sun-warn-text) 8%, transparent);
}
.llm-model {
  font-family: var(--sun-font-mono);
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.llm-conf {
  margin-left: auto;
  font-family: var(--sun-font-mono);
  font-size: 12px;
  font-weight: 700;
}
.llm-conf--ok {
  color: var(--sun-ok-text);
}
.llm-conf--warn {
  color: var(--sun-warn-text);
}
.llm-conf--error {
  color: var(--sun-error-text);
}
.llm-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--sun-text-primary);
}
.llm-src {
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.llm-src summary {
  cursor: pointer;
}
.llm-src ul {
  margin: 6px 0 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.llm-src code {
  font-family: var(--sun-font-mono);
  font-size: 11px;
  color: var(--sun-info-text);
}
.llm-src-name {
  margin-left: 8px;
  color: var(--sun-text-tertiary);
}
</style>
