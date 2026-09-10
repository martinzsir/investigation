<script setup lang="ts">
// ★ FE-C-033 KanbanCard：处置看板卡片。
// 左 3px 竖条=状态色（已立案金）；分数+间类标签；负责人+停留天数；
// 超期（SLA）整卡红框 + 红字「超期 N 天」；卡内可直接迁移状态（走 ClueStatusMachine
// 的确认弹窗与四重门禁，写操作唯一通道不变）。
import { computed } from 'vue'
import { NPopover } from 'naive-ui'
import {
  isControlledTerminal,
  scoreBasisRows,
  statusMetaOf,
  type ClueAction,
} from '../../domain/clue'
import type { BoardCard } from '../../domain/board'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'
import StatusBadge from '../common/StatusBadge.vue'
import ClueStatusMachine from '../research/ClueStatusMachine.vue'

const props = defineProps<{
  card: BoardCard
  role: string
  operator: string
  degraded: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  submit: [payload: { action: ClueAction; note?: string; reason?: string; legal_basis?: string }]
  open: [clueId: string]
}>()

const { config: cfg } = useCaseOntologyConfig()

const meta = computed(() => statusMetaOf(props.card.status, cfg.value))
const barColor = computed(() => meta.value?.border ?? 'var(--sun-border)')
const isFiled = computed(() => isControlledTerminal(props.card.status, cfg.value))
const rooms = computed(() => (props.card.dimension ?? props.card.jian_types ?? []).slice(0, 4))
const operatorName = computed(() => props.card.operator || '未分派')
const initial = computed(() => operatorName.value.slice(0, 1))
// R13：分数可解释（有 score_basis 才出现 popover）
const basisRows = computed(() => scoreBasisRows(props.card.score_basis))
const hasBasis = computed(() => basisRows.value.length > 0)
</script>

<template>
  <div
    class="kcard"
    :class="{ 'kcard--overdue': card.overdue, 'kcard--filed': isFiled }"
    @click="emit('open', card.clue_id)"
  >
    <span class="kcard-bar" :style="{ background: barColor }" aria-hidden="true" />
    <div class="kcard-body">
      <div class="kcard-head">
        <code class="kcard-id">{{ card.clue_id }}</code>
        <NPopover v-if="hasBasis" trigger="hover" placement="bottom-end" :show-arrow="false">
          <template #trigger>
            <span class="kcard-score kcard-score--link">{{ card.priority_score ?? '—' }}</span>
          </template>
          <div class="score-pop">
            <div class="score-pop-head">
              优先级分 <b>{{ card.priority_score ?? '—' }}</b>
              <span class="score-src">{{ card.score_source ?? '' }}</span>
            </div>
            <table class="score-pop-table">
              <tbody>
                <tr v-for="r in basisRows" :key="r.key">
                  <td class="sp-label">{{ r.label }}</td>
                  <td class="sp-num">{{ r.raw.toFixed(2) }}</td>
                  <td class="sp-op">×{{ r.weight }}</td>
                  <td class="sp-num sp-contrib">{{ r.contrib.toFixed(3) }}</td>
                </tr>
              </tbody>
            </table>
            <code v-if="card.score_formula" class="score-formula">{{ card.score_formula }}</code>
          </div>
        </NPopover>
        <span v-else class="kcard-score">
          {{ card.priority_score ?? '—' }}
        </span>
      </div>

      <p class="kcard-title">{{ card.title }}</p>

      <div class="kcard-tags">
        <StatusBadge variant="level" :value="card.level ?? '观察'" />
        <StatusBadge v-for="r in rooms" :key="r" variant="room" :value="r" />
      </div>

      <div class="kcard-rule">
        <span class="kcard-rule-label">规则</span>
        <code class="kcard-rule-name">{{ card.skill_id ?? '—' }}</code>
      </div>

      <div class="kcard-foot">
        <span class="kcard-operator">
          <span class="kcard-avatar">{{ initial }}</span>
          {{ operatorName }}
        </span>
        <span v-if="card.overdue" class="kcard-overdue">超期 {{ card.stayDays }} 天</span>
        <span v-else class="kcard-stay">停留 {{ card.stayDays }} 天</span>
      </div>

      <!-- 卡内直接迁移：复用状态机组件（二次确认/门禁/降级禁用同源） -->
      <div class="kcard-actions" @click.stop>
        <ClueStatusMachine
          :status="card.status"
          :role="role"
          :operator="operator"
          :degraded="degraded"
          :loading="busy"
          @submit="emit('submit', $event)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.kcard {
  position: relative;
  display: flex;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  cursor: pointer;
  overflow: hidden;
  transition: border-color 0.15s ease;
}
.kcard:hover {
  border-color: var(--sun-border-active);
}
/* 超期：整卡红框（FE-C-033） */
.kcard--overdue {
  border-color: var(--sun-error-border);
  box-shadow: 0 0 0 1px var(--sun-error-border);
}
.kcard--overdue:hover {
  border-color: var(--sun-error-text);
}
.kcard--filed {
  border-color: var(--sun-filed-border);
}
.kcard-bar {
  flex: 0 0 3px;
  align-self: stretch;
}
.kcard-body {
  flex: 1;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.kcard-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.kcard-id {
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-border-active);
}
.kcard-score {
  font-family: var(--sun-font-mono);
  font-size: 13px;
  font-weight: 700;
  color: var(--sun-warn-text);
}
.kcard-score--link {
  cursor: help;
  border-bottom: 1px dotted currentColor;
}
/* R13 分数依据 popover */
.score-pop { min-width: 220px; font-size: 12px; }
.score-pop-head {
  display: flex; align-items: baseline; gap: 8px;
  margin-bottom: 6px; color: var(--sun-text-primary);
}
.score-src {
  margin-left: auto; font-size: 11px;
  color: var(--sun-text-tertiary); font-family: var(--sun-font-mono);
}
.score-pop-table { width: 100%; border-collapse: collapse; }
.score-pop-table td { padding: 2px 4px; }
.sp-label { color: var(--sun-text-secondary); }
.sp-num {
  font-family: var(--sun-font-mono); text-align: right;
  color: var(--sun-text-secondary);
}
.sp-op { color: var(--sun-text-tertiary); text-align: center; }
.sp-contrib { color: var(--sun-warn-text); font-weight: 600; }
.score-formula {
  display: block; margin-top: 6px; padding-top: 6px;
  border-top: 1px dashed var(--sun-border);
  font-size: 11px; color: var(--sun-text-tertiary);
  white-space: normal;
}
.kcard-title {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--sun-text-primary);
}
.kcard-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.kcard-rule {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.kcard-rule-label {
  color: var(--sun-text-tertiary);
}
.kcard-rule-name {
  font-family: var(--sun-font-mono);
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.kcard-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 2px;
}
.kcard-operator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.kcard-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--sun-info-bg);
  border: 1px solid var(--sun-info-border);
  color: var(--sun-info-text);
  font-size: 11px;
}
.kcard-stay {
  font-size: 12px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.kcard-overdue {
  font-size: 12px;
  font-weight: 700;
  color: var(--sun-error-text);
  font-family: var(--sun-font-mono);
}
.kcard-actions {
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px dashed rgba(16, 49, 74, 0.6);
}
</style>
