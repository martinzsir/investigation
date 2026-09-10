<script setup lang="ts">
// FE-P-001 治理仪表盘（MVP-1）：健康度第一块（零记录 warn，禁止「一切正常」）
// + 待办/诊断指标 + 五间雷达 + 高优先级线索列表。无选中案件时不发案件请求。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NSpin, NAlert, NButton, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { dashboardApi, type DashboardDto } from '../api/endpoints/dashboard'
import { cluesApi, type ClueListItem } from '../api/endpoints/clues'
import { tasksApi } from '../api/endpoints/tasks'
import { taskEvents } from '../api/sse'
import type { StreamHandle } from '../api/transport/types'
import { presentError } from '../api/errors'
import { isTerminal, mergeProgress, type TaskRow } from '../domain/task'
import { healthBanner, dimensionRooms } from '../domain/clue'
import { useCaseOntologyConfig } from '../composables/useCaseOntologyConfig'
import MetricCard from '../components/common/MetricCard.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import EmptyState from '../components/common/EmptyState.vue'
import JianRadar from '../components/research/JianRadar.vue'

const cs = useCaseStore()
const router = useRouter()
const message = useMessage()
const { config: ontologyCfg } = useCaseOntologyConfig()

const loading = ref(false)
const errorMsg = ref('')
const dashboard = ref<DashboardDto | null>(null)
const clues = ref<ClueListItem[]>([])

// 手动运行诊断（DIAGNOSE 任务；BUILD 不自动留痕，由用户发起）
const diagnoseTask = ref<TaskRow | null>(null)
let diagnoseStream: StreamHandle | null = null
const diagnosing = computed(() =>
  !!diagnoseTask.value && !isTerminal(diagnoseTask.value.status))
const diagnoseLabel = computed(() => {
  const t = diagnoseTask.value
  if (!t || isTerminal(t.status)) return '运行诊断'
  return `诊断中 ${Math.round(t.progress_pct ?? 0)}%`
})

onBeforeUnmount(() => diagnoseStream?.close())

async function runDiagnose(): Promise<void> {
  if (!cs.currentCaseId || diagnosing.value) return
  try {
    const t = await tasksApi.create(cs.currentCaseId, { task_type: 'DIAGNOSE' })
    diagnoseTask.value = t
    message.info(`已入队「运行诊断」任务 ${t.id}`)
    if (isTerminal(t.status)) return
    diagnoseStream?.close()
    diagnoseStream = taskEvents(t.id, {
      onTask: (next) => {
        diagnoseTask.value = mergeProgress(diagnoseTask.value ?? t, next)
        if (!isTerminal(next.status)) return
        diagnoseStream?.close()
        diagnoseStream = null
        if (next.status === 'SUCCEEDED') {
          message.success('运行诊断完成，健康度已刷新', { duration: 5000 })
          void load()
        } else if (next.status === 'FAILED') {
          message.error(`运行诊断失败：${next.error_code} ${next.error_message}`)
        }
      },
      onErrorEvent: (msg) => message.error(`诊断任务错误：${msg}`),
    })
  } catch (e) {
    message.error(presentError(e).title)
  }
}

const banner = computed(() => healthBanner(dashboard.value?.health ?? null))

const disposal = computed(() => dashboard.value?.todo?.disposal ?? null)
const byStatus = computed(() => disposal.value?.by_status ?? {})
const diagSev = computed(() => dashboard.value?.diagnostics?.by_severity ?? {})

/** 侦查维度命中数：按线索 dimension 聚合（雷达只读已产出结果；维度集声明化） */
const dimensionNames = computed(() => dimensionRooms(ontologyCfg.value))
const rooms = computed<Record<string, number>>(() => {
  const acc: Record<string, number> = {}
  for (const r of dimensionNames.value) acc[r] = 0
  for (const c of clues.value) {
    for (const d of c.dimension ?? []) {
      if (dimensionNames.value.includes(d)) acc[d] = (acc[d] ?? 0) + 1
    }
  }
  return acc
})

/**
 * 处置指标卡：states 声明驱动，旁路态（tone=muted，如已排除）不出卡；
 * MetricCard 色调映射声明 tone（受控终态金橙 filed）。
 */
const METRIC_TONE: Record<string, 'info' | 'ok' | 'filed' | 'cyan'> = {
  warning: 'info',
  info: 'info',
  success: 'ok',
  danger: 'filed',
  filed: 'filed',
}
const metricStates = computed(() =>
  ontologyCfg.value.states.filter((s) => s.tone !== 'muted'),
)
function metricTone(tone: string): 'info' | 'ok' | 'filed' | 'cyan' {
  return METRIC_TONE[tone] ?? 'info'
}

/** 高优先级线索：按 priority_rank 取前 8 */
const topClues = computed(() =>
  [...clues.value]
    .sort((a, b) => (a.priority_rank ?? 999) - (b.priority_rank ?? 999))
    .slice(0, 8),
)

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    dashboard.value = null
    clues.value = []
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    const [dash, page] = await Promise.all([
      dashboardApi.get(cs.currentCaseId),
      cluesApi.list(cs.currentCaseId, { page: 1, page_size: 50 }),
    ])
    dashboard.value = dash
    clues.value = page.items ?? []
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '仪表盘加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

function openClue(id: string): void {
  void router.push(`/c/clue/${encodeURIComponent(id)}`)
}
</script>

<template>
  <div class="page dashboard">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="治理仪表盘按案件聚合：在顶部案件选择器中选择一个案件后加载"
    />
    <template v-else>
      <div class="page-head">
        <h2>治理仪表盘</h2>
        <span class="case-name">{{ cs.currentCase?.name ?? cs.currentCaseId }}</span>
      </div>

      <NSpin :show="loading">
        <EmptyState
          v-if="errorMsg"
          type="error"
          title="仪表盘加载失败"
          :desc="errorMsg"
        >
          <template #action><NButton size="small" @click="load">重试</NButton></template>
        </EmptyState>

        <template v-else>
          <!-- 健康度：第一块；零记录/降级均为 warn 语义，不显示「一切正常」 -->
          <div class="health-bar">
            <NAlert
              :type="banner.tone === 'ok' ? 'success' : 'warning'"
              :title="banner.title"
              class="health-alert"
              :bordered="true"
            >
              {{ banner.detail }}
            </NAlert>
            <!-- 诊断不随 BUILD 自动发起：用户手动运行（DIAGNOSE 任务） -->
            <NButton
              class="diagnose-btn"
              size="small"
              type="primary"
              secondary
              :loading="diagnosing"
              :disabled="diagnosing"
              @click="runDiagnose"
            >
              {{ diagnoseLabel }}
            </NButton>
          </div>

          <!-- 指标行 -->
          <div class="metric-row">
            <MetricCard label="待办处置线索" :value="disposal?.total ?? 0" unit="条" tone="cyan" />
            <MetricCard
              v-for="s in metricStates"
              :key="s.name"
              :label="s.name"
              :value="byStatus[s.name] ?? 0"
              unit="条"
              :tone="metricTone(s.tone)"
            />
            <MetricCard
              label="严重诊断"
              :value="diagSev.critical ?? 0"
              unit="条"
              :tone="(diagSev.critical ?? 0) > 0 ? 'error' : 'cyan'"
            />
          </div>

          <div class="panels">
            <!-- 五间雷达 -->
            <section class="panel radar-panel">
              <header class="panel-head">
                <h3>五间交叉雷达</h3>
                <span class="panel-sub">通道命中数 · 双阈值升格</span>
              </header>
              <JianRadar :rooms="rooms" />
            </section>

            <!-- 高优先级线索 -->
            <section class="panel clues-panel">
              <header class="panel-head">
                <h3>高优先级线索</h3>
                <RouterLink class="panel-link" to="/c/clues">全部线索 →</RouterLink>
              </header>
              <table class="clue-table" v-if="topClues.length">
                <thead>
                  <tr>
                    <th>#</th><th>线索</th><th>级别</th><th>通道</th><th>状态</th><th>溯源</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="c in topClues" :key="c.clue_id" class="clue-row" @click="openClue(c.clue_id)">
                    <td class="rank">{{ c.priority_rank ?? '—' }}</td>
                    <td class="title-cell">
                      <span class="title">{{ c.title }}</span>
                      <span v-if="c.basis" class="basis-sub">{{ c.basis }}</span>
                      <code class="cid">{{ c.clue_id }}</code>
                    </td>
                    <td><StatusBadge variant="level" :value="c.level ?? '观察'" /></td>
                    <td>
                      <StatusBadge v-for="d in (c.dimension ?? []).slice(0, 3)" :key="d" variant="room" :value="d" class="room-cell" />
                    </td>
                    <td><StatusBadge variant="status" :value="c.status" /></td>
                    <td class="num-cell">{{ c.source_row_count ?? 0 }}</td>
                  </tr>
                </tbody>
              </table>
              <EmptyState v-else type="empty" title="暂无已产出线索" desc="案件尚未运行 BUILD/RESCAN，或规则零命中（零命中与「无线索」语义不同，详见诊断）" />
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
  color: var(--sun-text-primary);
}
.case-name {
  font-size: 13px;
  color: var(--sun-text-tertiary);
}
.health-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.health-alert {
  flex: 1;
  min-width: 0;
  border-radius: 6px;
}
.diagnose-btn {
  flex: none;
  white-space: nowrap;
}
.metric-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
  padding-top:10px;
  padding-bottom:10px;
}
@media (max-width: 1200px) {
  .metric-row {
    grid-template-columns: repeat(3, 1fr);
  }
}
.panels {
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 12px;
}
@media (max-width: 1200px) {
  .panels {
    grid-template-columns: 1fr;
  }
}
.panel {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 14px;
  min-width: 0;
}
.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.panel-head h3 {
  margin: 0;
  font-size: 14px;
}
.panel-sub {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.panel-link {
  font-size: 12px;
  color: var(--sun-border-active);
  text-decoration: none;
}
.clue-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.clue-table th {
  text-align: left;
  font-weight: 400;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--sun-border);
}
.clue-table td {
  padding: 8px;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.6);
  vertical-align: middle;
}
.clue-row {
  cursor: pointer;
}
.clue-row:hover td {
  background: rgba(110, 222, 233, 0.05);
}
.rank {
  font-family: var(--sun-font-mono);
  color: var(--sun-warn-text);
  width: 32px;
}
.title-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.title {
  color: var(--sun-text-primary);
}
.basis-sub {
  font-size: 12px;
  color: var(--sun-text-secondary);
  line-height: 1.4;
  word-break: break-all;
}
.cid {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.room-mini {
  font-size: 12px;
  margin-right: 8px;
  font-weight: 600;
}
.num-cell {
  font-family: var(--sun-font-mono);
  color: var(--sun-text-secondary);
}
</style>
