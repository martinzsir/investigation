<script setup lang="ts">
// FE-P-009 看板列：五态竖排卡片列。
import { computed } from 'vue'
import { statusMetaOf, type ClueAction } from '../../domain/clue'
import type { BoardCard } from '../../domain/board'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'
import KanbanCard from './KanbanCard.vue'

const props = defineProps<{
  status: string
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

const meta = computed(() => statusMetaOf(props.status, cfg.value))
</script>

<template>
  <section class="kcol" :class="`kcol--${status}`">
    <header class="kcol-head">
      <span class="kcol-dot" :style="{ background: meta.border }" aria-hidden="true" />
      <span class="kcol-name">{{ status }}</span>
      <span class="kcol-count">{{ cards.length }}</span>
    </header>
    <div class="kcol-body">
      <KanbanCard
        v-for="c in cards"
        :key="c.clue_id"
        :card="c"
        :role="role"
        :operator="operator"
        :degraded="degraded"
        :busy="busy"
        @submit="(p) => emit('submit', c.clue_id, p)"
        @open="emit('open', $event)"
      />
      <p v-if="!cards.length" class="kcol-empty">—</p>
    </div>
  </section>
</template>

<style scoped>
.kcol {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 240px;
  flex: 1;
}
.kcol-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--sun-bg-sider);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  position: sticky;
  top: 0;
  z-index: 1;
}
.kcol-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.kcol-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--sun-text-primary);
}
.kcol-count {
  margin-left: auto;
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.kcol-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 60px;
}
.kcol-empty {
  margin: 0;
  text-align: center;
  color: var(--sun-text-tertiary);
  font-size: 12px;
  padding: 16px 0;
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
}
</style>
