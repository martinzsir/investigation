<script setup lang="ts">
// ★ FE-P-009 处置看板：顶部统计条（五态计数 / 平均停留 / 超期 N 件）+ 五列卡片。
// 数据获取在页面层（BoardView）；本组件纯编排，超期/停留判定走 domain/board.ts。
import { computed } from 'vue'
import { boardColumns, boardStats, type BoardCard } from '../../domain/board'
import { statusMetaOf, type ClueAction } from '../../domain/clue'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'
import KanbanColumn from './KanbanColumn.vue'

const props = defineProps<{
  cards: BoardCard[]
  role: string
  operator: string
  degraded: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  submit: [clueId: string, payload: { action: ClueAction; note?: string; reason?: string; legal_basis?: string }]
  open: [clueId: string]
}>()

const { config: cfg } = useCaseOntologyConfig()

// D5：列序按 states 声明，旁路态（muted）置末
const columns = computed(() => boardColumns(cfg.value))
const stats = computed(() => boardStats(props.cards, cfg.value))
const cardsByStatus = computed(() => {
  const m = new Map<string, BoardCard[]>(columns.value.map((s) => [s, []]))
  for (const c of props.cards) {
    m.get(c.status)?.push(c)
  }
  return m
})
function dotColor(status: string): string {
  return statusMetaOf(status, cfg.value).border
}
</script>

<template>
  <div class="board">
    <div class="board-stats">
      <template v-for="s in columns" :key="s">
        <div class="stat" :class="`stat--${s}`">
          <span class="stat-dot" :style="{ background: dotColor(s) }" aria-hidden="true" />
          <span class="stat-label">{{ s }}</span>
          <span class="stat-num">{{ stats.byStatus[s] ?? 0 }}</span>
        </div>
      </template>
      <div class="stat stat--stay">
        <span class="stat-label">平均停留</span>
        <span class="stat-num">{{ stats.avgStay }} 天</span>
      </div>
      <div class="stat stat--overdue">
        <span class="stat-label">超期</span>
        <span class="stat-num" :class="{ 'stat-num--alert': stats.overdue > 0 }">{{ stats.overdue }} 件</span>
      </div>
    </div>

    <div class="board-cols">
      <KanbanColumn
        v-for="s in columns"
        :key="s"
        :status="s"
        :cards="cardsByStatus.get(s) ?? []"
        :role="role"
        :operator="operator"
        :degraded="degraded"
        :busy="busy"
        @submit="(clueId, p) => emit('submit', clueId, p)"
        @open="emit('open', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.board {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.board-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
}
.stat {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 12px;
  border-right: 1px solid var(--sun-border);
  font-size: 13px;
}
.stat:last-child {
  border-right: none;
}
.stat-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.stat-label {
  color: var(--sun-text-secondary);
}
.stat-num {
  font-family: var(--sun-font-mono);
  font-weight: 700;
  color: var(--sun-text-primary);
}
.stat-num--alert {
  color: var(--sun-error-text);
}
.stat--overdue .stat-label {
  color: var(--sun-error-text);
}
.board-cols {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  overflow-x: auto;
  padding-bottom: 8px;
}
</style>
