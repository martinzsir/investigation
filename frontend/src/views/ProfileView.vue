<script setup lang="ts">
// FE-P-007 数据画像（MVP-2）：六层报告（L1/L2 列层值层、L3 关注命中、L4 五间分布、L5 质量分）。
// 后端 GET /cases/{cid}/profiles（profiles_view.assemble_profiles → OntologyProfiler.profile_all）。
// 案件未 BUILD/无物化对象 → available:false → 「尚未接入数据源」空态。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NTooltip, NInput } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { profileApi } from '../api/endpoints/profile'
import { derivedApi, type DerivedPropertyDto } from '../api/endpoints/derived'
import { presentError } from '../api/errors'
import {
  nullRateBand, scoreBand, severityBand, metricLabel, groupColumnsByObject,
  type ProfileData, type ProfileColumn,
} from '../domain/profile'
import MetricCard from '../components/common/MetricCard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()

const loading = ref(false)
const notAvailable = ref(false)
const data = ref<ProfileData | null>(null)

// 属性画像按对象折叠：默认全部收起，点组头展开；换案件/刷新后重置为全折叠
const collapsedObjects = ref<Set<string>>(new Set())
const columnGroups = computed(() =>
  data.value ? groupColumnsByObject(data.value.columns) : [])

function toggleGroup(object: string): void {
  const next = new Set(collapsedObjects.value)
  if (next.has(object)) next.delete(object)
  else next.add(object)
  collapsedObjects.value = next
}
function expandAllGroups(): void {
  collapsedObjects.value = new Set()
}
function collapseAllGroups(): void {
  collapsedObjects.value = new Set(columnGroups.value.map((g) => g.object))
}

watch(data, (d) => {
  collapsedObjects.value = d
    ? new Set(groupColumnsByObject(d.columns).map((g) => g.object))
    : new Set()
})

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

const statusLabel: Record<string, string> = {
  ok: '正常', unmaterialized_object: '对象未物化', missing_column: '缺列',
}

/** 未物化/缺列行不参与数值展示 */
function isDead(c: ProfileColumn): boolean {
  return c.status !== 'ok'
}

function pct(v: number | null): string {
  return v === null ? '—' : (v * 100).toFixed(1) + '%'
}

function fmtMetric(v: number | null, metric: string): string {
  if (v === null) return '—'
  if (metric.includes('rate') || metric === 'focus_hit_rate' || metric === 'window_coverage' || metric === 'wan_integer_rate') {
    return (v * 100).toFixed(1) + '%'
  }
  return String(v)
}

// ---- R8：派生属性按需查询（查询时计算；不进线索详情，避免膨胀）----
const dObjType = ref('person')
const dObjId = ref('')
const dProp = ref('transaction_count')
const dLoading = ref(false)
const dResult = ref<DerivedPropertyDto | null>(null)
const dError = ref('')

const dRows = computed<Array<Record<string, unknown>>>(() => {
  const v = dResult.value?.value
  return Array.isArray(v) ? (v as Array<Record<string, unknown>>) : []
})
const dRowCols = computed<string[]>(() => {
  const cols = new Set<string>()
  for (const r of dRows.value) for (const k of Object.keys(r)) cols.add(k)
  return [...cols]
})
const dScalar = computed(() => {
  const v = dResult.value?.value
  return Array.isArray(v) ? null : (v ?? null)
})

async function queryDerived(): Promise<void> {
  if (!cs.currentCaseId) return
  const t = dObjType.value.trim()
  const id = dObjId.value.trim()
  const prop = dProp.value.trim()
  if (!t || !id || !prop) {
    dError.value = '对象类型、对象标识、属性名均必填'
    return
  }
  dLoading.value = true
  dError.value = ''
  dResult.value = null
  try {
    dResult.value = await derivedApi.get(cs.currentCaseId, t, id, prop)
  } catch (e) {
    dError.value = presentError(e).title
  } finally {
    dLoading.value = false
  }
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
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
          desc="案件尚未导入数据或未完成 BUILD；接入数据源并完成 BUILD 后，将展示六层画像（L1/L2 列层值层、L3 关注命中、L4 五间分布、L5 质量分）"
        />
        <template v-else-if="data && data.available">
          <!-- 指标卡 -->
          <div class="metrics">
            <MetricCard
              label="整库画像分"
              :value="data.overall_score.toFixed(0)"
              :tone="scoreBand(data.overall_score) === 'ok' ? 'ok' : scoreBand(data.overall_score) === 'warn' ? 'warn' : 'error'"
            >
              <div class="score-range">可推翻至 {{ data.score_range[1] }}</div>
            </MetricCard>
            <MetricCard label="已物化对象" :value="data.source_count" unit="个" tone="cyan" />
            <MetricCard label="问题项" :value="data.issue_count" unit="项" tone="warn" />
            <MetricCard label="关注实体" :value="data.aligned_entities" unit="个" tone="info" />
          </div>

          <!-- 画像声明 + 锚点 -->
          <div class="declare-bar">
            <span class="declare-note">⚠ {{ data.note ?? '结论均为待核实候选；画像只观察不写回' }}</span>
            <span class="declare-anchor">
              锚点日期 {{ data.anchor_date ?? '—' }}<template v-if="data.window_days"> · 窗口 {{ data.window_days }} 天</template>
            </span>
          </div>

          <!-- R8 派生属性：按对象/属性按需查询（查询时计算，不进线索详情） -->
          <section class="panel">
            <h3>
              派生属性（查询时计算）
              <span class="dim" style="font-weight:400;font-size:12px">
                — 声明于 derived_properties.json，经白名单 Function 计算，结果可缓存可溯源
              </span>
            </h3>
            <div class="derived-form">
              <NInput v-model:value="dObjType" size="small" placeholder="对象类型，如 person" class="d-inp d-inp--type" />
              <span class="d-dot">.</span>
              <NInput v-model:value="dObjId" size="small" placeholder="对象标识（姓名等 name_property 值）" class="d-inp" @keyup.enter="queryDerived" />
              <span class="d-dot">/</span>
              <NInput v-model:value="dProp" size="small" placeholder="派生属性名，如 transaction_count" class="d-inp" @keyup.enter="queryDerived" />
              <NButton size="small" type="primary" secondary :loading="dLoading" @click="queryDerived">查询</NButton>
            </div>
            <p v-if="dError" class="d-err">{{ dError }}</p>
            <div v-if="dResult" class="d-result">
              <template v-if="!dResult.available">
                <p class="dim hint">{{ dResult.note ?? '派生属性不可用' }}</p>
              </template>
              <template v-else>
                <div class="d-meta">
                  <span class="tag tag--conn mono">{{ dResult.function }}</span>
                  <span class="tag tag--dead">{{ dResult.cache_policy }}</span>
                  <span
                    class="tag"
                    :class="dResult.cache === 'hit' ? 'tag--ok' : 'tag--dead'"
                    :title="`params_hash=${dResult.params_hash} · source=${dResult.source_version_set}`"
                  >cache: {{ dResult.cache }}</span>
                </div>
                <table v-if="dRows.length" class="p-table d-table">
                  <thead>
                    <tr><th v-for="c in dRowCols" :key="c">{{ c }}</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(r, i) in dRows" :key="i">
                      <td v-for="c in dRowCols" :key="c" class="mono">{{ fmtCell(r[c]) }}</td>
                    </tr>
                  </tbody>
                </table>
                <p v-else class="mono d-scalar">{{ fmtCell(dScalar) }}</p>
              </template>
            </div>
          </section>

          <!-- L4 五间分布 -->
          <section class="panel">
            <h3>五间分布（L4）</h3>
            <div class="jian-grid">
              <div
                v-for="j in data.jians"
                :key="j.jian"
                class="jian-card"
                :class="{ 'jian--dead': !j.declared, 'jian--live': j.has_materialized }"
              >
                <div class="jian-name">{{ j.jian }}</div>
                <div class="jian-counts">
                  <span>对象 <b>{{ j.objects.length }}</b></span>
                  <span>链接 <b>{{ j.links.length }}</b></span>
                </div>
                <div class="jian-status">
                  <span v-if="j.has_materialized" class="jstat jstat--live">已物化</span>
                  <span v-else-if="j.declared" class="jstat jstat--declared">仅声明</span>
                  <span v-else class="jstat jstat--none">未声明</span>
                </div>
                <div class="jian-members dim">{{ j.objects.join('、') || '—' }}</div>
              </div>
            </div>
          </section>

          <!-- L1/L2 属性画像 -->
          <section class="panel">
            <h3 class="l1l2-head">
              属性画像（L1/L2 列层 / 值层）
              <span class="grp-controls">
                <button class="grp-link" @click="expandAllGroups">全部展开</button>
                <button class="grp-link" @click="collapseAllGroups">全部折叠</button>
              </span>
            </h3>
            <div class="table-scroll">
              <table class="p-table">
                <thead>
                  <tr>
                    <th>对象</th><th>属性</th><th>类型</th><th>可连接</th><th>状态</th>
                    <th>行数</th><th>空值率</th><th>distinct</th><th>样本</th>
                    <th>清洗丢弃</th><th>合规率</th><th>类型落点</th>
                    <th style="width:140px">列评分</th><th>问题</th>
                  </tr>
                </thead>
                <tbody>
                  <template v-for="g in columnGroups" :key="g.object">
                    <tr class="group-row" @click="toggleGroup(g.object)">
                      <td colspan="14">
                        <span class="grp-arrow">{{ collapsedObjects.has(g.object) ? '▶' : '▼' }}</span>
                        <span class="mono grp-name">{{ g.object }}</span>
                        <span class="grp-badge">{{ g.columns.length }} 个属性</span>
                        <span v-if="g.issue_count > 0" class="grp-badge grp-badge--issue">{{ g.issue_count }} 项问题</span>
                        <span v-if="g.live_count < g.columns.length" class="grp-badge grp-badge--dead">
                          {{ g.columns.length - g.live_count }} 未物化
                        </span>
                      </td>
                    </tr>
                    <tr
                      v-for="c in g.columns"
                      v-show="!collapsedObjects.has(g.object)"
                      :key="`${c.object}.${c.attribute}`"
                      :class="{ 'row--dead': isDead(c) }"
                    >
                    <td class="obj-cell"></td>
                    <td>{{ c.attribute }}</td>
                    <td class="mono dim">{{ c.value_type }}</td>
                    <td>
                      <span v-if="c.connectable" class="tag tag--conn">可连接</span>
                      <span v-else class="dim">—</span>
                    </td>
                    <td>
                      <span v-if="isDead(c)" class="tag tag--dead">{{ statusLabel[c.status] ?? c.status }}</span>
                      <span v-else class="tag tag--ok">正常</span>
                    </td>
                    <td class="mono dim">{{ isDead(c) ? '—' : c.row_count }}</td>
                    <td class="mono" :class="isDead(c) ? 'dim' : `band--${nullRateBand(c.null_rate)}`">
                      {{ isDead(c) ? '—' : (c.null_rate * 100).toFixed(1) + '%' }}
                    </td>
                    <td class="mono dim">{{ isDead(c) ? '—' : c.distinct_count }}</td>
                    <td class="sample-cell">
                      <NTooltip v-if="c.samples.length" trigger="hover">
                        <template #trigger>
                          <span class="samples">{{ c.samples.slice(0, 2).join('、') }}<span v-if="c.samples.length > 2"> …</span></span>
                        </template>
                        {{ c.samples.join('、') }}
                      </NTooltip>
                      <span v-else class="dim">—</span>
                    </td>
                    <td class="mono">
                      <span v-if="c.dropped_rows !== null && c.dropped_rows > 0" class="band--warn" :title="c.clean_rule ?? ''">
                        −{{ c.dropped_rows }}
                      </span>
                      <span v-else class="dim">—</span>
                    </td>
                    <td class="mono" :class="c.compliance_rate !== null && c.compliance_rate > 0 ? 'band--error' : ''">
                      {{ pct(c.compliance_rate) }}
                    </td>
                    <td>
                      <span v-if="c.landing.length" class="landing">
                        <span v-for="l in c.landing" :key="l" class="tag tag--land">{{ l }}</span>
                      </span>
                      <span v-else class="dim">—</span>
                    </td>
                    <td>
                      <div v-if="!isDead(c)" class="score-bar">
                        <div class="score-fill" :class="`band--${scoreBand(c.score)}`" :style="{ width: `${c.score}%` }" />
                        <span class="score-num">{{ c.score }}</span>
                      </div>
                      <span v-else class="dim">—</span>
                    </td>
                    <td class="issue-cell">
                      <span v-if="c.mixed_type" class="issue-tag issue--block">混装</span>
                      <span v-if="c.composite_suspect > 0" class="issue-tag issue--block" :title="`疑似复合值 ${c.composite_suspect} 个`">复合×{{ c.composite_suspect }}</span>
                      <span v-if="c.needs_confirmation" class="issue-tag">待确认</span>
                      <span v-if="c.variants_rule + c.variants_alias > 0" class="issue-tag">变体{{ c.variants_rule + c.variants_alias }}</span>
                      <span v-for="t in c.issues" :key="t" class="issue-tag">{{ t }}</span>
                      <span v-if="!c.mixed_type && !c.composite_suspect && !c.needs_confirmation && !c.variants_rule && !c.variants_alias && !c.issues.length && !isDead(c)" class="dim">—</span>
                    </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </section>

          <div class="two-col">
            <!-- L3 关注命中 -->
            <section class="panel">
              <h3>关注命中与指标（L3）</h3>
              <p v-if="!data.metrics.length" class="dim hint">无 L3 指标（未提供关注实体或锚点日期）</p>
              <table v-else class="p-table">
                <thead>
                  <tr><th>对象.属性</th><th>指标</th><th>值</th><th>状态</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(m, i) in data.metrics" :key="i">
                    <td class="mono">{{ m.object }}.{{ m.prop }}</td>
                    <td>{{ metricLabel(m.metric) }}</td>
                    <td class="mono">
                      <span :class="m.status === 'ok' ? 'band--ok' : m.status === 'not_evaluated' ? 'dim' : 'band--warn'">
                        {{ fmtMetric(m.value, m.metric) }}
                      </span>
                    </td>
                    <td class="dim" :title="m.reason">{{ m.status === 'ok' ? '正常' : m.reason || m.status }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <!-- 书写变体 -->
            <section class="panel">
              <h3>书写变体候选</h3>
              <p v-if="!data.variants.length" class="dim hint">无可连接属性的变体候选</p>
              <div v-for="v in data.variants" :key="`${v.object}.${v.prop}`" class="variant-group">
                <p class="variant-title"><span class="mono">{{ v.object }}.{{ v.prop }}</span></p>
                <div class="variant-stats">
                  <span class="vstat" title="规则轨：同语言异写（拼音/编辑距离相似）">规则 {{ v.rule_count }}</span>
                  <span class="vstat" title="别名轨：case_knowledge 命中">别名 {{ v.alias_count }}</span>
                  <span class="vstat total">合计 {{ v.total }}</span>
                </div>
              </div>
            </section>
          </div>

          <!-- L5 扣分明细 -->
          <section class="panel">
            <h3>扣分明细（L5 质量分）<span class="dim" style="font-weight:400;font-size:12px"> — 画像分 = 100 + Σ(扣分)，启发式扣分可人工推翻</span></h3>
            <table class="p-table">
              <thead>
                <tr><th>范围</th><th>对象/属性</th><th>代码</th><th>原因</th><th>严重度</th><th>扣分</th></tr>
              </thead>
              <tbody>
                <tr v-for="(d, i) in data.deductions" :key="`${d.code}-${i}`">
                  <td class="dim">{{ d.scope === 'prop' ? '属性' : d.scope === 'object' ? '对象' : d.scope }}</td>
                  <td class="mono">{{ d.ref }}</td>
                  <td class="mono"><b>{{ d.code }}</b></td>
                  <td>{{ d.reason }}</td>
                  <td>
                    <span class="sev-badge" :class="`band--${severityBand(d.severity)}`">
                      {{ sevLabel[d.severity] ?? d.severity }}
                    </span>
                  </td>
                  <td class="mono neg">{{ d.points }}</td>
                </tr>
              </tbody>
            </table>
          </section>
        </template>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head { display: flex; align-items: baseline; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.case-name { font-size: 13px; color: var(--sun-text-tertiary); }
.refresh { margin-left: auto; }
.metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.score-range { font-size: 11px; font-weight: 400; color: var(--sun-text-tertiary); }
.declare-bar {
  display: flex; align-items: center; justify-content: space-between;
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.declare-note { color: var(--sun-warn-text); }
.declare-anchor { color: var(--sun-text-tertiary); }
.panel {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px;
}
.panel h3 { margin: 0 0 10px; font-size: 14px; color: var(--sun-text-primary); }
.table-scroll { overflow-x: auto; }
.p-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.p-table th {
  text-align: left; font-weight: 400; color: var(--sun-text-tertiary);
  padding: 6px 8px; border-bottom: 1px solid var(--sun-border); white-space: nowrap;
}
.p-table td { padding: 7px 8px; border-bottom: 1px dashed rgba(16, 49, 74, 0.6); vertical-align: middle; }
.row--dead { opacity: 0.55; }
/* 对象分组折叠 */
.l1l2-head { display: flex; align-items: center; gap: 12px; }
.grp-controls { margin-left: auto; font-weight: 400; display: flex; gap: 10px; }
.grp-link {
  background: none; border: none; padding: 0; cursor: pointer;
  font-size: 12px; color: var(--sun-info-text);
}
.grp-link:hover { text-decoration: underline; }
.group-row { cursor: pointer; background: var(--sun-input-bg); user-select: none; }
.group-row:hover { background: var(--sun-hover-bg, rgba(64, 158, 255, 0.08)); }
.group-row td { padding: 8px; border-bottom: 1px solid var(--sun-border); }
.grp-arrow {
  display: inline-block; width: 16px; font-size: 10px;
  color: var(--sun-text-tertiary); transition: transform 0.1s;
}
.grp-name { font-weight: 600; margin-right: 10px; }
.grp-badge {
  display: inline-block; font-size: 11px; padding: 0 8px; margin-right: 6px;
  border-radius: 10px; border: 1px solid var(--sun-border);
  color: var(--sun-text-secondary); background: var(--sun-bg-card);
}
.grp-badge--issue { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.grp-badge--dead { color: var(--sun-text-tertiary); }
.obj-cell { background: transparent; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
.hint { font-size: 12px; padding: 8px 0; }
/* R8 派生属性查询 */
.derived-form { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.d-inp { width: 200px; }
.d-inp--type { width: 120px; }
.d-dot { color: var(--sun-text-tertiary); font-family: var(--sun-font-mono); }
.d-err { color: var(--sun-error-text); font-size: 12px; margin: 8px 0 0; }
.d-result { margin-top: 10px; }
.d-meta { display: flex; gap: 6px; margin-bottom: 8px; }
.d-table { margin-top: 4px; }
.d-scalar { font-size: 12px; color: var(--sun-text-secondary); margin: 4px 0; }
.band--ok { color: var(--sun-ok-text); }
.band--warn { color: var(--sun-warn-text); }
.band--error { color: var(--sun-error-text); }
.neg { color: var(--sun-error-text); }
/* 标签 */
.tag {
  display: inline-block; font-size: 11px; padding: 0 8px; border-radius: 10px;
  border: 1px solid; white-space: nowrap;
}
.tag--conn { color: var(--sun-info-text); border-color: var(--sun-info-border); background: var(--sun-info-bg); }
.tag--ok { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.tag--dead { color: var(--sun-text-tertiary); border-color: var(--sun-border); background: var(--sun-input-bg); }
.tag--land { color: var(--sun-info-text); border-color: var(--sun-info-border); background: var(--sun-info-bg); margin: 1px 2px 1px 0; }
.samples {
  font-family: var(--sun-font-mono); font-size: 11px; color: var(--sun-text-secondary);
  cursor: help; border-bottom: 1px dotted var(--sun-border);
}
.score-bar { position: relative; height: 10px; background: var(--sun-input-bg); border-radius: 5px; overflow: hidden; }
.score-fill { height: 100%; border-radius: 5px; }
.score-fill.band--ok { background: var(--sun-ok-border); }
.score-fill.band--warn { background: var(--sun-warn-border); }
.score-fill.band--error { background: var(--sun-error-border); }
.score-num { position: absolute; right: 6px; top: -3px; font-size: 11px; font-family: var(--sun-font-mono); color: var(--sun-text-secondary); }
.issue-tag {
  display: inline-block; font-size: 11px; padding: 0 8px; margin: 1px 4px 1px 0;
  border-radius: 10px; border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); background: var(--sun-warn-bg); white-space: nowrap;
}
.issue--block { border-color: var(--sun-error-border); color: var(--sun-error-text); background: var(--sun-error-bg); }
/* 五间 */
.jian-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.jian-card {
  border: 1px solid var(--sun-border); border-radius: 6px; padding: 10px;
  background: var(--sun-input-bg);
}
.jian-card.jian--live { border-color: var(--sun-ok-border); }
.jian-card.jian--dead { opacity: 0.55; }
.jian-name { font-size: 14px; font-weight: 600; margin-bottom: 6px; }
.jian-counts { display: flex; gap: 10px; font-size: 12px; color: var(--sun-text-secondary); }
.jian-counts b { font-family: var(--sun-font-mono); }
.jian-status { margin: 6px 0; }
.jstat { display: inline-block; font-size: 11px; padding: 0 8px; border-radius: 10px; border: 1px solid; }
.jstat--live { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.jstat--declared { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.jstat--none { color: var(--sun-text-tertiary); border-color: var(--sun-border); }
.jian-members { font-size: 11px; margin-top: 4px; line-height: 1.5; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start; }
.variant-group { margin-bottom: 14px; }
.variant-title { margin: 0 0 6px; font-size: 12px; color: var(--sun-text-secondary); }
.variant-stats { display: flex; gap: 8px; }
.vstat {
  font-size: 11px; color: var(--sun-info-text); background: var(--sun-info-bg);
  border: 1px solid var(--sun-info-border); border-radius: 4px; padding: 1px 8px;
}
.vstat.total { color: var(--sun-warn-text); background: var(--sun-warn-bg); border-color: var(--sun-warn-border); }
.sev-badge { display: inline-block; font-size: 11px; padding: 1px 10px; border-radius: 10px; border: 1px solid; }
</style>
