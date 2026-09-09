<script setup lang="ts">
// FE-C-007 TaskProgressCard：进度条 + 阶段文案 + 可展开明细（决策 11 字段映射）。
// progress_pct 缺失/越界 → indeterminate「计算中」；终态色（成功绿/失败红显 error_code/取消灰）。
import { computed, ref } from 'vue'
import { NProgress, NButton } from 'naive-ui'
import {
  TASK_STATUS_META, isTerminal, isFailed, isIndeterminate,
  canCancel, failureSummary, taskTypeLabel, normPct,
  type TaskRow,
} from '../../domain/task'

const props = defineProps<{
  task: TaskRow
  /** 断线重连中（>3s 才由父级置 true，避免闪烁） */
  reconnecting?: boolean
  /** 是否显示取消按钮（任务中心运行中区） */
  cancellable?: boolean
}>()

const emit = defineEmits<{ (e: 'cancel', id: string): void }>()

const expanded = ref(false)

const meta = computed(() => TASK_STATUS_META[props.task.status] ?? TASK_STATUS_META.PENDING)
const pct = computed(() => normPct(props.task.progress_pct))
const indeterminate = computed(() => isIndeterminate(props.task.progress_pct))
const terminal = computed(() => isTerminal(props.task.status))
const failed = computed(() => isFailed(props.task.status))
const showCancel = computed(() => props.cancellable && canCancel(props.task.status))

const progressStatus = computed<'success' | 'error' | 'warning' | 'default'>(() => {
  if (props.task.status === 'SUCCEEDED') return 'success'
  if (props.task.status === 'FAILED') return 'error'
  if (props.task.status === 'CANCELLED') return 'warning'
  return 'default'
})

const stageText = computed(() => {
  const label = props.task.progress_label || props.task.progress_stage
  if (!label) return terminal.value ? meta.value.label : '计算中'
  return label
})

const hasDetail = computed(() => Boolean(props.task.progress_detail))
</script>

<template>
  <div class="tcard" :class="[`tcard--${meta.tone}`, { 'tcard--terminal': terminal }]">
    <div class="tcard-head">
      <span class="tcard-type">{{ taskTypeLabel(task.task_type) }}</span>
      <span class="tcard-id mono">{{ task.id }}</span>
      <span class="tcard-status" :class="`st-${meta.tone}`">{{ meta.label }}</span>
      <NButton
        v-if="showCancel"
        size="tiny"
        quaternary
        class="tcard-cancel"
        @click="emit('cancel', task.id)"
      >
        取消
      </NButton>
    </div>

    <div class="tcard-progress">
      <NProgress
        type="line"
        :percentage="indeterminate ? 100 : pct"
        :status="progressStatus"
        :processing="indeterminate || task.status === 'RUNNING'"
        :show-indicator="!indeterminate"
        :height="8"
        :border-radius="4"
      />
      <span v-if="indeterminate && !terminal" class="tcard-pct mono">计算中…</span>
      <span v-else class="tcard-pct mono">{{ Math.round(pct < 0 ? 0 : pct) }}%</span>
    </div>

    <div class="tcard-stage">
      <span>{{ stageText }}</span>
      <span v-if="reconnecting" class="tcard-reconnect">连接中断，正在自动重连…</span>
    </div>

    <div v-if="failed" class="tcard-error">
      <span class="tcard-error-label">失败：</span>{{ failureSummary(task) }}
    </div>

    <div v-if="hasDetail" class="tcard-detail-toggle">
      <button class="link-btn" @click="expanded = !expanded">
        {{ expanded ? '收起明细 ▲' : '展开明细 ▼' }}
      </button>
      <div v-if="expanded" class="tcard-detail mono">{{ task.progress_detail }}</div>
    </div>
  </div>
</template>

<style scoped>
.tcard {
  border: 1px solid var(--sun-border);
  border-left: 3px solid var(--sun-info-border, var(--sun-border));
  border-radius: 6px;
  padding: 10px 12px;
  background: var(--sun-bg-card);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tcard--ok { border-left-color: var(--sun-ok-border); }
.tcard--error { border-left-color: var(--sun-error-border); }
.tcard--muted { border-left-color: var(--sun-border); }
.tcard--warn { border-left-color: var(--sun-warn-border); }
.tcard--terminal.tcard--error {
  border-color: var(--sun-error-border);
  box-shadow: 0 0 0 1px var(--sun-error-border);
}
.tcard-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tcard-type { font-size: 13px; font-weight: 600; color: var(--sun-text-primary); }
.tcard-id { font-size: 11px; color: var(--sun-text-tertiary); }
.tcard-status {
  margin-left: auto;
  font-size: 11px;
  padding: 0 8px;
  border-radius: 10px;
  border: 1px solid;
}
.st-ok { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.st-info { color: var(--sun-info-text, var(--sun-ok-text)); border-color: var(--sun-info-border, var(--sun-ok-border)); background: var(--sun-info-bg, var(--sun-ok-bg)); }
.st-warn { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.st-error { color: var(--sun-error-text); border-color: var(--sun-error-border); background: var(--sun-error-bg); }
.st-muted { color: var(--sun-text-tertiary); border-color: var(--sun-border); }
.tcard-cancel { margin-left: 4px; }
.tcard-progress {
  display: flex;
  align-items: center;
  gap: 10px;
}
.tcard-progress :deep(.n-progress) { flex: 1; }
.tcard-pct {
  font-size: 12px;
  color: var(--sun-text-secondary);
  min-width: 52px;
  text-align: right;
}
.tcard-stage {
  font-size: 12px;
  color: var(--sun-text-secondary);
  display: flex;
  align-items: center;
  gap: 10px;
}
.tcard-reconnect {
  color: var(--sun-warn-text);
  font-size: 11px;
}
.tcard-error {
  font-size: 12px;
  color: var(--sun-error-text);
  background: var(--sun-error-bg);
  border: 1px solid var(--sun-error-border);
  border-radius: 4px;
  padding: 6px 8px;
}
.tcard-error-label { font-weight: 600; }
.tcard-detail-toggle { font-size: 12px; }
.link-btn {
  background: none;
  border: none;
  color: var(--sun-info-text, var(--sun-ok-text));
  cursor: pointer;
  padding: 0;
  font-size: 12px;
}
.tcard-detail {
  margin-top: 6px;
  padding: 6px 8px;
  background: var(--sun-input-bg, rgba(255, 255, 255, 0.03));
  border-radius: 4px;
  font-size: 11px;
  color: var(--sun-text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
}
.mono { font-family: var(--sun-font-mono); }
</style>
