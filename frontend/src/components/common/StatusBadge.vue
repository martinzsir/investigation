<script setup lang="ts">
// FE-C-001 StatusBadge：5 态/间类/优先级，色+图标+文字三重编码。
import { computed } from 'vue'
import { STATUS_META, type ClueStatus, type CrossLevel, ANOMALY_LEVEL } from '../../domain/clue'

const props = defineProps<{
  variant: 'status' | 'level' | 'room'
  value: string
}>()

const statusMeta = computed(() =>
  props.variant === 'status' ? STATUS_META[props.value as ClueStatus] : null,
)

/** 交叉等级配色：观察=灰、线索=青、可立案依据候选=金；异常待核实=琥珀 */
const levelClass = computed(() => {
  if (props.variant !== 'level') return ''
  if (props.value === ANOMALY_LEVEL) return 'level--pending'
  if (props.value === ('可立案依据候选' satisfies CrossLevel)) return 'level--candidate'
  if (props.value === ('线索' satisfies CrossLevel)) return 'level--clue'
  return 'level--watch'
})

const roomVar = computed(() => {
  if (props.variant !== 'room') return ''
  const map: Record<string, string> = {
    资金: 'var(--sun-jian-fund)',
    通讯: 'var(--sun-jian-comms)',
    行为: 'var(--sun-jian-behavior)',
    关系: 'var(--sun-jian-relation)',
    时间: 'var(--sun-jian-time)',
  }
  return map[props.value] ?? 'var(--sun-text-tertiary)'
})
</script>

<template>
  <span
    v-if="variant === 'status' && statusMeta"
    class="badge status-badge"
    :style="{
      color: statusMeta.text,
      borderColor: statusMeta.border,
      background: statusMeta.bg,
    }"
  >
    <span class="badge-icon" aria-hidden="true">{{ statusMeta.icon }}</span>
    {{ value }}
  </span>
  <span v-else-if="variant === 'level'" class="badge level-badge" :class="levelClass">
    {{ value }}
  </span>
  <span
    v-else
    class="badge room-badge"
    :style="{ color: roomVar, borderColor: roomVar, background: `color-mix(in srgb, ${roomVar} 12%, transparent)` }"
  >
    {{ value }}间
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 10px;
  border: 1px solid;
  border-radius: var(--sun-radius-badge, 12px);
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;
  font-weight: 500;
}
.badge-icon {
  font-size: 11px;
  line-height: 1;
}
.level-badge {
  border-style: solid;
}
.level--watch {
  color: var(--sun-text-secondary);
  border-color: var(--sun-text-tertiary);
  background: rgba(107, 131, 153, 0.12);
}
.level--clue {
  color: var(--sun-info-text);
  border-color: var(--sun-info-border);
  background: var(--sun-info-bg);
}
.level--candidate {
  color: var(--sun-filed-text);
  border-color: var(--sun-filed-border);
  background: var(--sun-filed-bg);
}
.level--pending {
  color: var(--sun-warn-text);
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
}
</style>
