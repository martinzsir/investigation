<script setup lang="ts">
// FE-C-002 MetricCard：指标卡含变化率 ↑↓；大数字等宽（JetBrains Mono）。
import { computed } from 'vue'

const props = defineProps<{
  label: string
  value: number | string
  unit?: string
  /** 变化率：正数 ↑、负数 ↓、0/缺省不显示 */
  delta?: number | null
  /** 语义强调色变量名后缀：ok/warn/error/info/filed，缺省青 */
  tone?: 'ok' | 'warn' | 'error' | 'info' | 'filed' | 'cyan'
}>()

const deltaText = computed(() => {
  if (props.delta === null || props.delta === undefined) return ''
  if (props.delta > 0) return `↑ ${props.delta}`
  if (props.delta < 0) return `↓ ${Math.abs(props.delta)}`
  return '— 0'
})
</script>

<template>
  <div class="metric" :class="`metric--${tone ?? 'cyan'}`">
    <div class="metric-label">{{ label }}</div>
    <div class="metric-value">
      <span class="num">{{ value }}</span>
      <span v-if="unit" class="unit">{{ unit }}</span>
    </div>
    <div v-if="deltaText" class="metric-delta" :class="{ up: (delta ?? 0) > 0, down: (delta ?? 0) < 0 }">
      {{ deltaText }}
    </div>
  </div>
</template>

<style scoped>
.metric {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 16px;
  min-width: 0;
}
.metric-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
  margin-bottom: 6px;
}
.metric-value {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.num {
  font-family: var(--sun-font-mono);
  font-size: 26px;
  font-weight: 700;
  line-height: 1.1;
}
.unit {
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.metric-delta {
  margin-top: 4px;
  font-size: 12px;
  font-family: var(--sun-font-mono);
}
.metric-delta.up {
  color: var(--sun-warn-text);
}
.metric-delta.down {
  color: var(--sun-ok-text);
}
.metric--cyan .num {
  color: var(--sun-border-active);
}
.metric--ok .num {
  color: var(--sun-ok-text);
}
.metric--warn .num {
  color: var(--sun-warn-text);
}
.metric--error .num {
  color: var(--sun-error-text);
}
.metric--info .num {
  color: var(--sun-info-text);
}
.metric--filed .num {
  color: var(--sun-filed-text);
}
</style>
