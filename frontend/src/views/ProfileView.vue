<script setup lang="ts">
// FE-P-007 数据画像（MVP-2）：列级空值率/ distinct / 画像分、书写变体、扣分明细。
// 后端 /profiles 待补（⛔）——当前由 MSW mock 供演示；真后端无数据时走
// 「尚未接入数据源」空态（非红线功能，允许 mock）。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { profileApi } from '../api/endpoints/profile'
import { nullRateBand, scoreBand, severityBand, type ProfileData } from '../domain/profile'
import MetricCard from '../components/common/MetricCard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()

const loading = ref(false)
const notAvailable = ref(false)
const data = ref<ProfileData | null>(null)

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    data.value = null
    notAvailable.value = false
    return
  }
  loading.value = true
  notAvailable.value = false
  try {
    data.value = await profileApi.get(cs.currentCaseId)
  } catch {
    // 后端端点未就绪/案件未接入：统一空态（不弹错误，属待补能力）
    data.value = null
    notAvailable.value = true
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const maxDist = computed(() =>
  Math.max(1, ...(data.value?.variants.flatMap((v) => v.distribution.map((d) => d.count)) ?? [1])),
)
const sevLabel: Record<string, string> = { high: '高', medium: '中', low: '低' }
</script>

<template>
  <div class="page">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="数据画像按案件归属，在顶部案件选择器中选择后加载"
    />
    <template v-else>
      <div class="page-head">
        <h2>数据画像</h2>
        <span class="case-name">{{ cs.currentCase?.name ?? cs.currentCaseId }}</span>
        <NButton size="small" class="refresh" :loading="loading" @click="load">刷新</NButton>
      </div>

      <NSpin :show="loading">
        <EmptyState
          v-if="!loading && (notAvailable || !data)"
          type="empty"
          title="尚未接入数据源"
          desc="数据画像端点（GET /profiles）待后端补齐；接入数据源并完成 BUILD 后，将展示列级空值率、书写变体与扣分明细"
        />
        <template v-else-if="data">
          <div class="metrics">
            <MetricCard label="整库画像分" :value="data.overall_score.toFixed(1)" tone="ok" />
            <MetricCard label="已接入数据源" :value="data.source_count" unit="个" tone="cyan" />
            <MetricCard label="问题项" :value="data.issue_count" unit="项" tone="warn" />
            <MetricCard label="已对齐实体" :value="data.aligned_entities" unit="个" tone="info" />
          </div>

          <section class="panel">
            <h3>属性画像</h3>
            <table class="p-table">
              <thead>
                <tr>
                  <th>对象</th><th>属性</th><th>值类型</th><th>空值率</th><th>distinct</th>
                  <th>混装</th><th style="width:160px">画像分</th><th>问题标签</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in data.columns" :key="`${c.object}.${c.attribute}`">
                  <td class="mono">{{ c.object }}</td>
                  <td>{{ c.attribute }}</td>
                  <td class="mono dim">{{ c.value_type }}</td>
                  <td class="mono" :class="`band--${nullRateBand(c.null_rate)}`">
                    {{ (c.null_rate * 100).toFixed(1) }}%
                  </td>
                  <td class="mono dim">{{ c.distinct_count }}</td>
                  <td>
                    <span v-if="c.mixed_type" class="mix-warn" title="同列混装多种值类型">⚠ 混装</span>
                    <span v-else class="dim">—</span>
                  </td>
                  <td>
                    <div class="score-bar">
                      <div
                        class="score-fill"
                        :class="`band--${scoreBand(c.score)}`"
                        :style="{ width: `${c.score}%` }"
                      />
                      <span class="score-num">{{ c.score }}</span>
                    </div>
                  </td>
                  <td>
                    <span v-for="t in c.issues" :key="t" class="issue-tag">{{ t }}</span>
                    <span v-if="!c.issues.length" class="dim">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <div class="two-col">
            <section class="panel">
              <h3>实体书写变体</h3>
              <div v-for="v in data.variants" :key="v.canonical" class="variant-group">
                <p class="variant-title">
                  {{ v.group }} · <b>{{ v.canonical }}</b>
                </p>
                <div class="variant-chips">
                  <code v-for="name in v.variants" :key="name" class="variant-chip">{{ name }}</code>
                </div>
                <div class="dist">
                  <div v-for="d in v.distribution" :key="d.value" class="dist-row">
                    <span class="dist-label">{{ d.value }}</span>
                    <div class="dist-track">
                      <div class="dist-fill" :style="{ width: `${(d.count / maxDist) * 100}%` }" />
                    </div>
                    <span class="dist-count mono">{{ d.count }}</span>
                  </div>
                </div>
              </div>
            </section>

            <section class="panel">
              <h3>扣分明细</h3>
              <table class="p-table">
                <thead>
                  <tr><th>范围</th><th>代码</th><th>原因</th><th>严重度</th></tr>
                </thead>
                <tbody>
                  <tr v-for="d in data.deductions" :key="d.code">
                    <td class="dim">{{ d.scope }}</td>
                    <td class="mono">{{ d.ref }}<br /><b>{{ d.code }}</b></td>
                    <td>{{ d.reason }}</td>
                    <td>
                      <span class="sev-badge" :class="`band--${severityBand(d.severity)}`">
                        {{ sevLabel[d.severity] ?? d.severity }}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </section>
          </div>
        </template>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.page-head h2 {
  margin: 0;
  font-size: 18px;
}
.case-name {
  font-size: 13px;
  color: var(--sun-text-tertiary);
}
.refresh {
  margin-left: auto;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.panel {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 14px;
}
.panel h3 {
  margin: 0 0 10px;
  font-size: 14px;
  color: var(--sun-text-primary);
}
.p-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.p-table th {
  text-align: left;
  font-weight: 400;
  color: var(--sun-text-tertiary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--sun-border);
  white-space: nowrap;
}
.p-table td {
  padding: 7px 8px;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.6);
  vertical-align: middle;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
.band--ok {
  color: var(--sun-ok-text);
}
.band--warn {
  color: var(--sun-warn-text);
}
.band--error {
  color: var(--sun-error-text);
}
.mix-warn {
  color: var(--sun-warn-text);
  font-size: 12px;
}
.score-bar {
  position: relative;
  height: 10px;
  background: var(--sun-input-bg);
  border-radius: 5px;
  overflow: hidden;
}
.score-fill {
  height: 100%;
  border-radius: 5px;
}
.score-fill.band--ok {
  background: var(--sun-ok-border);
}
.score-fill.band--warn {
  background: var(--sun-warn-border);
}
.score-fill.band--error {
  background: var(--sun-error-border);
}
.score-num {
  position: absolute;
  right: 6px;
  top: -3px;
  font-size: 11px;
  font-family: var(--sun-font-mono);
  color: var(--sun-text-secondary);
}
.issue-tag {
  display: inline-block;
  font-size: 11px;
  padding: 0 8px;
  margin: 1px 4px 1px 0;
  border-radius: 10px;
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
}
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  align-items: start;
}
.variant-group {
  margin-bottom: 14px;
}
.variant-title {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.variant-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.variant-chip {
  font-family: var(--sun-font-mono);
  font-size: 11px;
  color: var(--sun-info-text);
  background: var(--sun-info-bg);
  border: 1px solid var(--sun-info-border);
  border-radius: 4px;
  padding: 1px 8px;
}
.dist-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
}
.dist-label {
  width: 110px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dist-track {
  flex: 1;
  height: 8px;
  background: var(--sun-input-bg);
  border-radius: 4px;
  overflow: hidden;
}
.dist-fill {
  height: 100%;
  background: var(--sun-info-border);
  border-radius: 4px;
}
.dist-count {
  width: 40px;
  text-align: right;
  font-size: 11px;
  color: var(--sun-text-secondary);
}
.sev-badge {
  display: inline-block;
  font-size: 11px;
  padding: 1px 10px;
  border-radius: 10px;
  border: 1px solid;
}
</style>
