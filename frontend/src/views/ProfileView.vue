<script setup lang="ts">
// FE-P-007 数据画像（MVP-2）：列级空值率/ distinct / 画像分、书写变体、扣分明细。
// 后端 GET /cases/{cid}/profiles 已实现（六层报告），前端 adaptProfile 适配。
// 案件未 BUILD → available:false → 「尚未接入数据源」空态。
import { ref, watch } from 'vue'
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
    if (!data.value.available) notAvailable.value = true
  } catch {
    data.value = null
    notAvailable.value = true
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const sevLabel: Record<string, string> = {
  block: '阻断', warn: '告警', high: '高', medium: '中', low: '低',
}
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
          v-if="!loading && (notAvailable || !data || !data.available)"
          type="empty"
          title="尚未接入数据源"
          desc="案件尚未导入数据或未完成 BUILD；接入数据源并完成 BUILD 后，将展示列级空值率、书写变体与扣分明细"
        />
        <template v-else-if="data && data.available">
          <div class="metrics">
            <MetricCard label="整库画像分" :value="data.overall_score.toFixed(1)" tone="ok" />
            <MetricCard label="已物化对象" :value="data.source_count" unit="个" tone="cyan" />
            <MetricCard label="问题项" :value="data.issue_count" unit="项" tone="warn" />
            <MetricCard label="关注实体" :value="data.aligned_entities" unit="个" tone="info" />
          </div>

          <section class="panel">
            <h3>属性画像（L1/L2）</h3>
            <table class="p-table">
              <thead>
                <tr>
                  <th>对象</th><th>属性</th><th>值类型</th><th>空值率</th><th>distinct</th>
                  <th>混装</th><th style="width:160px">列评分</th><th>问题标签</th>
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
                    <span v-if="!c.issues.length" class="dim">{{ c.status === 'ok' ? '—' : c.status }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <div class="two-col">
            <section class="panel">
              <h3>书写变体候选</h3>
              <p v-if="!data.variants.length" class="dim hint">无可连接属性的变体候选</p>
              <div v-for="v in data.variants" :key="`${v.object}.${v.prop}`" class="variant-group">
                <p class="variant-title">
                  <span class="mono">{{ v.object }}.{{ v.prop }}</span>
                </p>
                <div class="variant-stats">
                  <span class="vstat" title="规则轨：同语言异写（拼音/编辑距离相似）">规则 {{ v.rule_count }}</span>
                  <span class="vstat" title="别名轨：case_knowledge 命中">别名 {{ v.alias_count }}</span>
                  <span class="vstat total">合计 {{ v.total }}</span>
                </div>
              </div>
            </section>

            <section class="panel">
              <h3>扣分明细（L5）</h3>
              <table class="p-table">
                <thead>
                  <tr><th>范围</th><th>代码</th><th>原因</th><th>严重度</th><th>扣分</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(d, i) in data.deductions" :key="`${d.code}-${i}`">
                    <td class="dim">{{ d.scope }}</td>
                    <td class="mono">{{ d.ref }}<br /><b>{{ d.code }}</b></td>
                    <td>{{ d.reason }}</td>
                    <td>
                      <span class="sev-badge" :class="`band--${severityBand(d.severity)}`">
                        {{ sevLabel[d.severity] ?? d.severity }}
                      </span>
                    </td>
                    <td class="mono" :class="d.points < 0 ? 'neg' : 'dim'">{{ d.points }}</td>
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
.hint {
  font-size: 12px;
  padding: 8px 0;
}
.band--ok { color: var(--sun-ok-text); }
.band--warn { color: var(--sun-warn-text); }
.band--error { color: var(--sun-error-text); }
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
.score-fill.band--ok { background: var(--sun-ok-border); }
.score-fill.band--warn { background: var(--sun-warn-border); }
.score-fill.band--error { background: var(--sun-error-border); }
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
.variant-stats {
  display: flex;
  gap: 8px;
}
.vstat {
  font-size: 11px;
  color: var(--sun-info-text);
  background: var(--sun-info-bg);
  border: 1px solid var(--sun-info-border);
  border-radius: 4px;
  padding: 1px 8px;
}
.vstat.total {
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border-color: var(--sun-warn-border);
}
.sev-badge {
  display: inline-block;
  font-size: 11px;
  padding: 1px 10px;
  border-radius: 10px;
  border: 1px solid;
}
.neg {
  color: var(--sun-error-text);
}
</style>
