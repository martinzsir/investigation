<script setup lang="ts">
// 庙算热力矩阵（D2：零依赖 CSS 色阶，不引 echarts）。
// 行 = 交叉等级（观察/线索/确认），列 = 五间；counts[level][jian]。
// 色阶五档 heat-0..heat-4（accent 单色渐变，按全表最大值归一）。
import { computed } from 'vue'
import type { HypothesesDto } from '../../api/endpoints/research'
import { heatLevel, heatMax } from '../../domain/miaoSuan'

const props = defineProps<{
  heatmap: HypothesesDto['heatmap']
}>()

const max = computed(() => heatMax(props.heatmap.counts))

function level(count: number): number {
  return heatLevel(count, max.value)
}

const total = computed(() =>
  props.heatmap.counts.reduce((acc, row) => acc + row.reduce((a, b) => a + b, 0), 0),
)
</script>

<template>
  <div class="heat">
    <div class="heat-head">
      <span class="heat-title">五间 × 交叉等级热力</span>
      <span class="heat-total dim">线索-间标签 {{ total }}</span>
    </div>
    <table class="heat-grid">
      <thead>
        <tr>
          <th class="heat-corner" />
          <th v-for="j in heatmap.jians" :key="j" class="heat-col-head">{{ j }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(lv, li) in heatmap.levels" :key="lv">
          <th class="heat-row-head">{{ lv }}</th>
          <td
            v-for="(c, ci) in heatmap.counts[li] ?? []"
            :key="heatmap.jians[ci] ?? ci"
            class="heat-cell"
            :class="`heat-${level(c)}`"
          >
            {{ c }}
          </td>
        </tr>
      </tbody>
    </table>
    <div class="heat-legend">
      <span class="dim">少</span>
      <span v-for="i in 5" :key="i" class="heat-swatch" :class="`heat-${i - 1}`" />
      <span class="dim">多</span>
    </div>
  </div>
</template>

<style scoped>
.heat {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.heat-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.heat-title {
  font-size: 13px;
  font-weight: 600;
}
.heat-total {
  font-size: 11px;
}
.heat-grid {
  border-collapse: separate;
  border-spacing: 4px;
}
.heat-corner {
  width: 72px;
}
.heat-col-head,
.heat-row-head {
  font-size: 12px;
  font-weight: 500;
  color: var(--sun-text-secondary);
  text-align: center;
  padding: 2px 6px;
}
.heat-row-head {
  text-align: right;
  white-space: nowrap;
}
.heat-cell {
  min-width: 64px;
  height: 40px;
  text-align: center;
  vertical-align: middle;
  border-radius: 4px;
  font-family: var(--sun-font-mono);
  font-size: 13px;
  color: var(--sun-text-primary);
  border: 1px solid transparent;
}
.heat-0 {
  background: rgba(110, 159, 193, 0.08);
  color: var(--sun-text-tertiary);
}
.heat-1 {
  background: rgba(109, 200, 236, 0.22);
}
.heat-2 {
  background: rgba(109, 200, 236, 0.42);
}
.heat-3 {
  background: rgba(109, 200, 236, 0.66);
  color: #0b2233;
}
.heat-4 {
  background: rgba(90, 216, 166, 0.85);
  color: #0b2233;
  font-weight: 700;
}
.heat-legend {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
}
.heat-swatch {
  display: inline-block;
  width: 18px;
  height: 10px;
  border-radius: 2px;
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
