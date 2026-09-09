<script setup lang="ts">
// 质量检查（MVP-4，/c/quality）。
// 四扫描（合规一致性/数据时效性/敏感面暴露/单位一致性）；
// 红线六：heuristic 启发式结果封顶 suggest（黄建议），永不出红阻断；
// 红线七：deterministic 违规 = block（红），必须处置而非仅提示。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { qualityApi, type QualityReport, type QualityCheck } from '../api/endpoints/quality'
import { presentError, isApiError } from '../api/errors'
import {
  groupBySeverity, hasBlocking, heuristicCeilingViolations, healthLevel,
  SEVERITY_LABEL, SEVERITY_COLOR, CATEGORY_LABEL, HEALTH_LABEL,
} from '../domain/qualityReport'
import { canWriteConfig } from '../domain/policyMatrix'
import { useAuthStore } from '../stores/auth'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const running = ref(false)
const report = ref<QualityReport | null>(null)

const canRun = computed(() => canWriteConfig(auth.clearance))
const available = computed(() => report.value?.available === true)
const health = computed(() => healthLevel(report.value))
/** available=true 时的报告（模板收窄用） */
const rep = computed(() =>
  report.value?.available === true
    ? (report.value as Extract<QualityReport, { available: true }>)
    : null,
)

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    report.value = null
    return
  }
  loading.value = true
  try {
    report.value = await qualityApi.latest(cs.currentCaseId)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

async function runChecks(): Promise<void> {
  if (!cs.currentCaseId) return
  running.value = true
  try {
    const task = await qualityApi.runCheck(cs.currentCaseId)
    message.success(`质量扫描已入队（任务 ${task.id}），可在任务中心查看进度；完成后刷新本页`)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    running.value = false
  }
}

const groups = computed(() =>
  available.value ? groupBySeverity((report.value as Extract<QualityReport, { available: true }>).checks) : null,
)
const blocking = computed(() =>
  available.value ? hasBlocking(report.value as Extract<QualityReport, { available: true }>) : false,
)
/** 红线六自检：heuristic 越界（理论上后端不出，前端兜底计数） */
const ceilingViolations = computed<QualityCheck[]>(() =>
  available.value ? heuristicCeilingViolations((report.value as Extract<QualityReport, { available: true }>).checks) : [],
)

const summary = computed(() =>
  available.value ? (report.value as Extract<QualityReport, { available: true }>).summary : null,
)

function sevColor(sev: string): string {
  return SEVERITY_COLOR[sev as keyof typeof SEVERITY_COLOR] ?? 'var(--sun-text-secondary)'
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>质量检查</h2>
      <p class="dim hint">
        四类扫描：合规一致性 / 数据时效性 / 敏感面暴露 / 单位一致性。确定性违规红牌阻断，启发式仅黄牌建议。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="质量报告按案件归属" />

    <template v-else>
      <div class="toolbar">
        <NButton type="primary" size="small" :loading="running" :disabled="!canRun" @click="runChecks">
          {{ canRun ? '运行四扫描' : '🔒 触发检查需偏将及以上' }}
        </NButton>
        <NButton size="small" :loading="loading" @click="load">刷新报告</NButton>
      </div>

      <NSpin :show="loading">
        <EmptyState
          v-if="!loading && !available"
          type="empty"
          title="尚未运行质量检查"
          desc="点击「运行四扫描」；检查覆盖语义层全部对象/属性，结果随报告留痕"
        />

        <template v-else-if="available && report">
          <!-- 总健康度 -->
          <div class="health-bar" :class="`health-${health}`">
            总健康度：{{ HEALTH_LABEL[health] }}
            <span v-if="summary" class="dim">
              （共 {{ summary.total }} 项：通过 {{ summary.passed }} / 警告 {{ summary.warnings }} / 违规 {{ summary.violations }}）
            </span>
            <span class="meta dim">{{ rep?.check_id }} · {{ rep?.created_by }} · {{ rep?.created_at }} · 数据版本 v{{ rep?.data_version }}</span>
          </div>

          <div v-if="blocking" class="block-bar">
            ⛔ 存在确定性违规（红牌）：违规项必须处置（修正数据/策略后重跑），不能仅作提示忽略。
          </div>
          <div v-if="ceilingViolations.length" class="block-bar">
            ⚠ 前端自检发现 {{ ceilingViolations.length }} 项启发式结果越界（应为 suggest），已按黄牌降级显示。
          </div>

          <!-- 分组展示：block → warn → suggest → ok -->
          <div v-for="sev in ['block', 'warn', 'suggest', 'ok']" :key="sev" class="sev-section">
            <div class="sev-head" :style="{ color: sevColor(sev) }">
              {{ SEVERITY_LABEL[sev as keyof typeof SEVERITY_LABEL] }}
              <span class="count">{{ groups?.[sev as keyof typeof groups]?.length ?? 0 }}</span>
            </div>
            <div v-if="(groups?.[sev as keyof typeof groups]?.length ?? 0) === 0" class="dim sev-empty">无</div>
            <div v-for="(c, i) in groups?.[sev as keyof typeof groups] ?? []" :key="i" class="check-card">
              <div class="check-head">
                <span class="sev-dot" :style="{ background: sevColor(c.severity) }"></span>
                <span class="mono obj">{{ c.obj }}.{{ c.prop }}</span>
                <NTag size="tiny" :bordered="false">{{ CATEGORY_LABEL[c.category] }}</NTag>
                <NTag size="tiny" :bordered="false" :type="c.mode === 'heuristic' ? 'warning' : 'info'">
                  {{ c.mode === 'heuristic' ? '启发式·建议' : '确定性' }}
                </NTag>
                <span v-if="c.count" class="dim count-num">{{ c.count }} 行/项</span>
                <span v-if="c.rule_id" class="mono dim rule-id">{{ c.rule_id }}</span>
              </div>
              <div class="check-msg">{{ c.message }}</div>
              <div v-if="c.samples_masked.length" class="dim samples">样本（已遮蔽）：{{ c.samples_masked.join('；') }}</div>
            </div>
          </div>
        </template>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.hint { font-size: 12px; margin: 4px 0 0; }
.toolbar { display: flex; gap: 8px; }
.health-bar {
  border-radius: 6px; padding: 10px 14px; font-size: 14px; font-weight: 600;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  border: 1px solid var(--sun-border); background: var(--sun-bg-card);
}
.health-pass { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.health-warn { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.health-block { color: var(--sun-error-text); border-color: var(--sun-error-border); background: var(--sun-error-bg); }
.health .meta, .meta { font-weight: 400; font-size: 11px; margin-left: auto; }
.block-bar {
  background: var(--sun-error-bg); border: 1px solid var(--sun-error-border);
  color: var(--sun-error-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.sev-section { display: flex; flex-direction: column; gap: 8px; }
.sev-head { font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.count { background: var(--sun-border); border-radius: 10px; padding: 0 8px; font-size: 11px; color: var(--sun-text-secondary); }
.sev-empty { font-size: 12px; }
.check-card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-left-width: 3px; border-radius: 4px; padding: 8px 12px; display: flex; flex-direction: column; gap: 4px;
}
.check-head { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.sev-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.obj { font-weight: 600; }
.count-num { font-size: 11px; }
.rule-id { font-size: 11px; }
.check-msg { font-size: 13px; }
.samples { font-size: 11px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
