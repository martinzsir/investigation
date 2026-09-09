<script setup lang="ts">
// 案件包校验七步清单（A1）：format/manifest/hash/declarations/schema/chain/duckdb。
// pass ✓ / warn ⚠（chain 不完整等，橙色不阻断）/ fail ✗；缺失步补 fail 占位。
import { computed } from 'vue'
import type { VerifyStep } from '../../api/endpoints/packageCase'
import { failCount, orderedSteps, stepTone } from '../../domain/packageFlow'

const props = defineProps<{
  steps: VerifyStep[]
}>()

const rows = computed(() => orderedSteps(props.steps))
const fails = computed(() => failCount(rows.value))
const warns = computed(() => rows.value.filter((s) => s.status === 'warn').length)

const ICON: Record<string, string> = { pass: '✓', warn: '⚠', fail: '✗' }
const TONE: Record<string, string> = { pass: 'ok', warn: 'warn', fail: 'fail' }
</script>

<template>
  <div class="steps">
    <div class="steps-head">
      <span class="steps-title">校验清单（七步）</span>
      <span v-if="fails" class="steps-badge steps-badge--fail">{{ fails }} 步失败</span>
      <span v-else-if="warns" class="steps-badge steps-badge--warn">{{ warns }} 步告警（不阻断）</span>
      <span v-else class="steps-badge steps-badge--ok">全部通过</span>
    </div>
    <ol class="steps-list">
      <li v-for="s in rows" :key="s.key" class="step" :class="`step--${TONE[stepTone(s.status)]}`">
        <span class="step-icon" :class="`step-icon--${TONE[stepTone(s.status)]}`">{{ ICON[s.status] }}</span>
        <span class="step-label">{{ s.label }}</span>
        <span v-if="s.detail" class="step-detail">{{ s.detail }}</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.steps {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.steps-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.steps-title {
  font-size: 13px;
  font-weight: 600;
}
.steps-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid;
}
.steps-badge--ok {
  color: var(--sun-ok-text);
  border-color: var(--sun-ok-border);
  background: var(--sun-ok-bg);
}
.steps-badge--warn {
  color: var(--sun-warn-text);
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
}
.steps-badge--fail {
  color: var(--sun-error-text);
  border-color: var(--sun-error-border);
  background: var(--sun-error-bg);
}
.steps-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.step {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
}
.step-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 700;
  flex: none;
}
.step-icon--ok {
  color: var(--sun-ok-text);
  background: var(--sun-ok-bg);
}
.step-icon--warn {
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
}
.step-icon--fail {
  color: var(--sun-error-text);
  background: var(--sun-error-bg);
}
.step-label {
  color: var(--sun-text-primary);
  min-width: 130px;
}
.step-detail {
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
  font-size: 11px;
  word-break: break-all;
}
.step--fail .step-label {
  color: var(--sun-error-text);
}
.step--warn .step-label {
  color: var(--sun-warn-text);
}
</style>
