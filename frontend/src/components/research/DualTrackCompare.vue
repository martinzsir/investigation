<script setup lang="ts">
// FE-C-022 双轨覆盖对比：声明轨（miaosuan:dimension 分析师标注）vs
// 实证轨（miaosuan:dimension:empirical 数据推导）。两轨缺口并列、不互相替代；
// 声明缺口靠补标注，实证缺口靠补数据/补侦查动作。
import { computed } from 'vue'
import { NTag } from 'naive-ui'
import type { CoverageCard, HypothesesDto } from '../../api/endpoints/research'
import { gapCounts } from '../../domain/miaoSuan'

const props = defineProps<{
  coverage: HypothesesDto['coverage']
}>()

const counts = computed(() => gapCounts(props.coverage))

interface TrackView {
  key: 'declared' | 'empirical'
  title: string
  sub: string
  cards: CoverageCard[]
}

const tracks = computed<TrackView[]>(() => [
  {
    key: 'declared',
    title: '声明轨',
    sub: '分析师标注的五间覆盖（miaosuan:dimension）',
    cards: props.coverage.declared ?? [],
  },
  {
    key: 'empirical',
    title: '实证轨',
    sub: '数据推导的五间覆盖（miaosuan:dimension:empirical）',
    cards: props.coverage.empirical ?? [],
  },
])

function sevType(sev: string | null): 'default' | 'warning' | 'error' | 'success' {
  if (sev === 'critical') return 'error'
  if (sev === 'warning') return 'warning'
  if (sev === 'info') return 'success'
  return 'default'
}
</script>

<template>
  <div class="dtc">
    <div class="dtc-head">
      <span class="dtc-title">双轨覆盖对比</span>
      <span class="dtc-badge" :class="{ 'dtc-badge--warn': counts.declared }">
        声明缺口 {{ counts.declared }}
      </span>
      <span class="dtc-badge" :class="{ 'dtc-badge--warn': counts.empirical }">
        实证缺口 {{ counts.empirical }}
      </span>
    </div>
    <div class="dtc-grid">
      <div v-for="t in tracks" :key="t.key" class="dtc-track">
        <div class="dtc-track-head">
          <span class="dtc-track-title">{{ t.title }}</span>
          <span class="dtc-track-sub dim">{{ t.sub }}</span>
        </div>
        <p v-if="!t.cards.length" class="dtc-empty dim">无缺口记录</p>
        <ul v-else class="dtc-cards">
          <li v-for="(c, i) in t.cards" :key="`${t.key}-${i}`" class="dtc-card">
            <div class="dtc-card-row">
              <span class="dtc-dim">{{ c.dimension ?? '五间全覆盖' }}</span>
              <span class="dtc-count mono">{{ c.covered }}/{{ c.total }}</span>
              <NTag v-if="c.severity" size="tiny" :bordered="false" :type="sevType(c.severity)">
                {{ c.severity }}
              </NTag>
            </div>
            <div v-if="c.missing?.length" class="dtc-missing">
              <span class="dim">缺口：</span>
              <span v-for="m in c.missing" :key="m" class="dtc-chip">{{ m }}</span>
            </div>
            <p v-if="c.reason" class="dtc-reason dim">{{ c.reason }}</p>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dtc {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.dtc-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dtc-title {
  font-size: 13px;
  font-weight: 600;
}
.dtc-badge {
  font-size: 11px;
  font-family: var(--sun-font-mono);
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--sun-border);
  color: var(--sun-text-tertiary);
}
.dtc-badge--warn {
  color: var(--sun-warn-text);
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
}
.dtc-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.dtc-track {
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.dtc-track-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.dtc-track-title {
  font-size: 12px;
  font-weight: 600;
}
.dtc-track-sub {
  font-size: 11px;
}
.dtc-empty {
  font-size: 12px;
  margin: 4px 0;
}
.dtc-cards {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.dtc-card {
  background: var(--sun-input-bg, rgba(255, 255, 255, 0.03));
  border-radius: 4px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.dtc-card-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.dtc-dim {
  font-weight: 600;
}
.dtc-count {
  color: var(--sun-text-tertiary);
  font-size: 11px;
}
.dtc-missing {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  font-size: 11px;
}
.dtc-chip {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 8px;
  padding: 0 8px;
  font-size: 11px;
}
.dtc-reason {
  font-size: 11px;
  margin: 0;
  word-break: break-all;
}
.dim {
  color: var(--sun-text-tertiary);
}
.mono {
  font-family: var(--sun-font-mono);
}
</style>
