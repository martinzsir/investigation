<script setup lang="ts">
// ★ FE-C-013 ThreeColumnEvidence（承重墙）：事实（青）/推断（琥珀）/待核实（灰虚线）物理分隔。
// 红线 FE-T-004：无 source_rows 的推断拒绝渲染（partitionEvidence 已剔除并计数）；
// 红线 FE-T-005：推断内容永不进入事实栏；推断栏配色固定琥珀，结构上禁绿。
import { computed } from 'vue'
import { partitionEvidence, type EvidenceItem } from '../../domain/clue'
import StatusBadge from '../common/StatusBadge.vue'

const props = defineProps<{ items: EvidenceItem[] }>()

const col = computed(() => partitionEvidence(props.items))
</script>

<template>
  <div class="three-col">
    <section class="ev-col ev-col--fact">
      <header class="ev-head">
        <span class="ev-dot" aria-hidden="true"></span>
        <h4>事实</h4>
        <span class="ev-count">{{ col.facts.length }}</span>
      </header>
      <ul class="ev-list">
        <li v-for="it in col.facts" :key="it.id" class="ev-item">
          <p>{{ it.text }}</p>
          <div v-if="it.source_rows?.length" class="ev-uris">
            <code v-for="r in it.source_rows" :key="r.row_uri">{{ r.row_uri }}</code>
          </div>
        </li>
        <li v-if="!col.facts.length" class="ev-empty">暂无已核事实</li>
      </ul>
    </section>

    <section class="ev-col ev-col--inference">
      <header class="ev-head">
        <span class="ev-dot" aria-hidden="true"></span>
        <h4>推断</h4>
        <span class="ev-count">{{ col.inferences.length }}</span>
      </header>
      <ul class="ev-list">
        <li v-for="it in col.inferences" :key="it.id" class="ev-item">
          <p>{{ it.text }}</p>
          <div v-if="it.room" class="ev-room">
            <StatusBadge variant="room" :value="it.room" />
          </div>
          <div class="ev-uris">
            <code v-for="r in it.source_rows" :key="r.row_uri">{{ r.row_uri }}</code>
          </div>
        </li>
        <li v-if="!col.inferences.length" class="ev-empty">暂无挂溯源的推断</li>
      </ul>
      <p v-if="col.droppedInferences > 0" class="ev-drop-warn" role="alert">
        ⚠ {{ col.droppedInferences }} 条推断因缺少溯源（source_rows）已拒绝渲染
      </p>
    </section>

    <section class="ev-col ev-col--pending">
      <header class="ev-head">
        <span class="ev-dot" aria-hidden="true"></span>
        <h4>待核实</h4>
        <span class="ev-count">{{ col.pending.length }}</span>
      </header>
      <ul class="ev-list">
        <li v-for="it in col.pending" :key="it.id" class="ev-item ev-item--pending">
          <p>{{ it.text }}</p>
        </li>
        <li v-if="!col.pending.length" class="ev-empty">暂无待核实事项</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.three-col {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
@media (max-width: 1100px) {
  .three-col {
    grid-template-columns: 1fr;
  }
}
.ev-col {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.ev-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--sun-border);
}
.ev-head h4 {
  margin: 0;
  font-size: 14px;
}
.ev-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.ev-count {
  margin-left: auto;
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.ev-list {
  list-style: none;
  margin: 0;
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex: 1;
}
.ev-item {
  border-left: 3px solid;
  padding: 2px 0 2px 10px;
}
.ev-item p {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--sun-text-primary);
}
.ev-uris {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.ev-uris code {
  font-family: var(--sun-font-mono);
  font-size: 11px;
  color: var(--sun-text-tertiary);
  background: rgba(110, 222, 233, 0.06);
  border: 1px solid var(--sun-border);
  border-radius: 3px;
  padding: 0 6px;
}
.ev-room {
  margin-top: 6px;
}
.ev-empty {
  font-size: 12px;
  color: var(--sun-text-tertiary);
  padding: 8px 0;
}
.ev-drop-warn {
  margin: 0;
  padding: 8px 14px;
  font-size: 12px;
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border-top: 1px dashed var(--sun-warn-border);
}

/* 事实栏：青 */
.ev-col--fact {
  border-top: 2px solid var(--sun-border-active);
}
.ev-col--fact .ev-dot {
  background: var(--sun-border-active);
  box-shadow: 0 0 8px rgba(110, 222, 233, 0.8);
}
.ev-col--fact .ev-item {
  border-left-color: var(--sun-border-active);
}

/* 推断栏：琥珀（结构固定，禁绿——FE-T-005） */
.ev-col--inference {
  border-top: 2px solid var(--sun-gold);
}
.ev-col--inference .ev-dot {
  background: var(--sun-gold);
  box-shadow: 0 0 8px rgba(242, 181, 77, 0.8);
}
.ev-col--inference .ev-item {
  border-left-color: var(--sun-gold);
}

/* 待核实栏：灰虚线 */
.ev-col--pending {
  border-top: 2px dashed var(--sun-text-tertiary);
  border-style: dashed;
}
.ev-col--pending .ev-head {
  border-bottom: 1px dashed var(--sun-text-tertiary);
}
.ev-col--pending .ev-dot {
  background: var(--sun-text-tertiary);
}
.ev-col--pending .ev-item--pending {
  border-left-color: var(--sun-text-tertiary);
  border-left-style: dashed;
  opacity: 0.85;
}
</style>
