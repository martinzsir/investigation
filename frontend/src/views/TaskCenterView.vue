<script setup lang="ts">
// FE-P-024 任务中心（MVP-3「能接入」，平台页 /tasks）。
// 服务端分页 + 状态/类型过滤：GET /tasks?status=&page=&page_size=。
// SSE 进度（决策 11）：订阅进行中任务，进度只增不减（mergeProgress）；
// 断线 >3s 才显重连提示（reconnectHint）；终态收通知（含行数明细）后刷新。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  NSpin, NButton, NInput, NSelect, useMessage,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { tasksApi } from '../api/endpoints/tasks'
import { taskEvents } from '../api/sse'
import type { StreamHandle } from '../api/transport/types'
import { presentError } from '../api/errors'
import {
  isActive, isTerminal, mergeProgress, taskTypeLabel,
  TASK_STATUS_META, reconnectHint, type TaskRow, type TaskStats,
} from '../domain/task'
import { PAGE_SIZE_DEFAULT, totalPages, clampPage, jumpPageError } from '../domain/pagination'
import TaskProgressCard from '../components/task/TaskProgressCard.vue'
import MetricCard from '../components/common/MetricCard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

const loading = ref(false)
const stats = ref<TaskStats>({ total: 0, pending: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, active: 0 })
/** 服务端 active 任务（PENDING/RUNNING） */
const serverActive = ref<TaskRow[]>([])
/** 历史任务当前页（终态） */
const history = ref<TaskRow[]>([])
const historyTotal = ref(0)
/** SSE 实时快照（taskId → 最新 TaskRow，单调合并） */
const live = ref<Map<string, TaskRow>>(new Map())

const downSince = new Map<string, number>()
const reconnecting = ref<Set<string>>(new Set())
const streams = new Map<string, StreamHandle>()
const reconnectTimers = new Map<string, ReturnType<typeof setTimeout>>()

type HistFilter = 'all' | 'active' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
const statusFilter = ref<HistFilter>('all')
const page = ref(1)

const statusOptions = [
  { label: '全部', value: 'all' },
  { label: '进行中', value: 'active' },
  { label: '已完成', value: 'SUCCEEDED' },
  { label: '失败', value: 'FAILED' },
  { label: '已取消', value: 'CANCELLED' },
]

/** 终态状态集（历史表 all 视角） */
const TERMINAL_STATUS = 'SUCCEEDED,FAILED,CANCELLED'

/** 进行中任务：服务端 active 经 SSE live 覆盖 + live 中新出现的 active */
const activeTasks = computed<TaskRow[]>(() => {
  const out: TaskRow[] = []
  const seen = new Set<string>()
  for (const t of serverActive.value) {
    if (seen.has(t.id)) continue
    seen.add(t.id)
    const lv = live.value.get(t.id)
    out.push(lv && isActive(lv.status) ? lv : t)
  }
  for (const [id, t] of live.value) {
    if (!seen.has(id) && isActive(t.status)) { seen.add(id); out.push(t) }
  }
  return out
})

const totalPagesHist = computed(() => totalPages(historyTotal.value, PAGE_SIZE_DEFAULT))
/** 当前页钳制到合法范围（过滤切换/数据刷新后 page 可能越界） */
const curPage = computed(() => clampPage(page.value, historyTotal.value, PAGE_SIZE_DEFAULT))
const histEmpty = computed(() => statusFilter.value === 'active' || history.value.length === 0)

/** 跳页输入框（与 DataTable 同一套纯函数校验） */
const jumpInput = ref('')
const jumpErr = ref('')

function goPage(p: number): void {
  const target = clampPage(p, historyTotal.value, PAGE_SIZE_DEFAULT)
  if (target !== page.value) page.value = target
}

function jumpPage(): void {
  const err = jumpPageError(jumpInput.value, historyTotal.value, PAGE_SIZE_DEFAULT)
  jumpErr.value = err
  if (err) return
  const n = Number.parseInt(jumpInput.value.trim(), 10)
  jumpInput.value = ''
  goPage(n)
}

function closeStream(id: string): void {
  streams.get(id)?.close()
  streams.delete(id)
  const timer = reconnectTimers.get(id)
  if (timer) { clearTimeout(timer); reconnectTimers.delete(id) }
  downSince.delete(id)
  reconnecting.value = new Set([...reconnecting.value].filter((x) => x !== id))
}

function subscribe(t: TaskRow): void {
  if (streams.has(t.id) || isTerminal(t.status)) return
  const handle = taskEvents(t.id, {
    onTask: (next) => {
      downSince.delete(t.id)
      reconnecting.value = new Set([...reconnecting.value].filter((x) => x !== t.id))
      const timer = reconnectTimers.get(t.id)
      if (timer) { clearTimeout(timer); reconnectTimers.delete(t.id) }
      const merged = mergeProgress(live.value.get(t.id) ?? t, next)
      const map = new Map(live.value)
      map.set(t.id, merged)
      live.value = map
      if (isTerminal(next.status)) {
        closeStream(t.id)
        notifyTerminal(next)
        void reload()
      }
    },
    onTransportError: () => {
      if (!downSince.has(t.id)) {
        downSince.set(t.id, Date.now())
        const timer = setTimeout(() => {
          const since = downSince.get(t.id)
          if (since && reconnectHint(Date.now() - since)) {
            reconnecting.value = new Set([...reconnecting.value, t.id])
          }
        }, 3100)
        reconnectTimers.set(t.id, timer)
      }
    },
  })
  streams.set(t.id, handle)
}

function notifyTerminal(t: TaskRow): void {
  if (t.status === 'SUCCEEDED') {
    const detail = t.progress_detail ? `：${t.progress_detail}` : ''
    message.success(`「${taskTypeLabel(t.task_type)}」已完成${detail}`, { duration: 6000 })
  } else if (t.status === 'FAILED') {
    message.error(`「${taskTypeLabel(t.task_type)}」失败：${t.error_code || ''} ${t.error_message || ''}`, { duration: 8000 })
  } else if (t.status === 'CANCELLED') {
    message.info(`「${taskTypeLabel(t.task_type)}」已取消`, { duration: 4000 })
  }
}

function syncSubscriptions(): void {
  const activeIds = new Set(activeTasks.value.map((t) => t.id))
  for (const id of [...streams.keys()]) {
    if (!activeIds.has(id)) closeStream(id)
  }
  for (const t of activeTasks.value) subscribe(t)
}

async function loadActive(): Promise<void> {
  if (!cs.currentCaseId) { serverActive.value = []; return }
  const res = await tasksApi.list(cs.currentCaseId, { status: 'active', pageSize: 200 })
  serverActive.value = res.items
  stats.value = res.stats
}

async function loadHistory(): Promise<void> {
  if (!cs.currentCaseId) { history.value = []; historyTotal.value = 0; return }
  if (statusFilter.value === 'active') { history.value = []; historyTotal.value = 0; return }
  const status = statusFilter.value === 'all' ? TERMINAL_STATUS : statusFilter.value
  const res = await tasksApi.list(cs.currentCaseId, {
    status, page: page.value, pageSize: PAGE_SIZE_DEFAULT,
  })
  history.value = res.items
  historyTotal.value = res.total
}

async function reload(): Promise<void> {
  if (!cs.currentCaseId) {
    stats.value = { total: 0, pending: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, active: 0 }
    serverActive.value = []; history.value = []; historyTotal.value = 0
    return
  }
  loading.value = true
  try {
    await Promise.all([loadActive(), loadHistory()])
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch([serverActive, live], syncSubscriptions, { immediate: true, deep: false })

watch(() => cs.currentCaseId, () => {
  for (const id of [...streams.keys()]) closeStream(id)
  live.value = new Map()
  page.value = 1
  void reload()
}, { immediate: true })

watch(statusFilter, () => {
  page.value = 1
  jumpInput.value = ''
  jumpErr.value = ''
  void loadHistory()
})
watch(page, () => void loadHistory())

onBeforeUnmount(() => {
  for (const id of [...streams.keys()]) closeStream(id)
})

async function onCancel(id: string): Promise<void> {
  try {
    await tasksApi.cancel(id)
    message.success('已请求取消该排队任务')
    await reload()
  } catch (e) {
    message.error(presentError(e).title)
  }
}

function fmtTime(s: string): string {
  return s ? s.replace('T', ' ').slice(0, 19) : '—'
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>任务中心</h2>
      <span class="case-name">{{ cs.currentCase?.name ?? (cs.currentCaseId ?? '未选择案件') }}</span>
      <NButton size="small" class="refresh" :loading="loading" @click="reload">刷新</NButton>
    </div>

    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="任务按案件归属，请在顶部案件选择器中选择案件"
    />

    <template v-else>
      <NSpin :show="loading">
        <!-- 五统计卡（后端 stats） -->
        <div class="metrics">
          <MetricCard label="任务总数" :value="stats.total" unit="个" tone="cyan" />
          <MetricCard label="排队 / 运行" :value="stats.active" unit="个" tone="info" />
          <MetricCard label="已完成" :value="stats.succeeded" unit="个" tone="ok" />
          <MetricCard label="失败" :value="stats.failed" unit="个" :tone="stats.failed ? 'error' : 'ok'" />
          <MetricCard label="已取消" :value="stats.cancelled" unit="个" tone="info" />
        </div>

        <!-- 运行中区（SSE 实时） -->
        <section class="panel">
          <h3>进行中
            <span v-if="activeTasks.length" class="dim">（{{ activeTasks.length }}）</span>
          </h3>
          <p v-if="!activeTasks.length" class="dim hint">当前无进行中任务。导入数据或构建语义层后，此处实时显示进度。</p>
          <div v-else class="active-list">
            <TaskProgressCard
              v-for="t in activeTasks"
              :key="t.id"
              :task="t"
              :reconnecting="reconnecting.has(t.id)"
              cancellable
              @cancel="onCancel"
            />
          </div>
        </section>

        <!-- 历史表（服务端分页） -->
        <section class="panel">
          <div class="hist-head">
            <h3>历史任务</h3>
            <NSelect
              v-model:value="statusFilter"
              :options="statusOptions"
              size="small"
              class="hist-filter"
            />
          </div>
          <p v-if="histEmpty" class="dim hint">
            {{ statusFilter === 'active' ? '进行中任务见上方「进行中」区。' : '暂无历史任务。' }}
          </p>
          <div v-else class="table-scroll">
            <table class="t-table">
              <thead>
                <tr>
                  <th>任务 ID</th><th>类型</th><th>状态</th><th>进度</th>
                  <th>明细 / 错误</th><th>创建人</th><th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in history" :key="t.id">
                  <td class="mono dim">{{ t.id }}</td>
                  <td>{{ taskTypeLabel(t.task_type) }}</td>
                  <td>
                    <span class="status-badge" :class="`st-${TASK_STATUS_META[t.status].tone}`">
                      {{ TASK_STATUS_META[t.status].label }}
                    </span>
                  </td>
                  <td class="mono">{{ t.status === 'SUCCEEDED' ? '100%' : Math.round(t.progress_pct || 0) + '%' }}</td>
                  <td class="detail-cell">
                    <span v-if="t.status === 'FAILED'" class="err-text">
                      {{ [t.error_code, t.error_message].filter(Boolean).join(' · ') || '失败' }}
                    </span>
                    <span v-else class="dim">{{ t.progress_detail || t.progress_label || '—' }}</span>
                  </td>
                  <td class="dim">{{ t.created_by || '—' }}</td>
                  <td class="mono dim">{{ fmtTime(t.updated_at || t.created_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <!-- 分页栏常驻：非「进行中」视角始终显示「共 N 条 / 第 X / Y 页」，单页时按钮禁用 -->
          <div v-if="statusFilter !== 'active'" class="pager">
            <span class="pager-total">共 {{ historyTotal }} 条</span>
            <NButton size="tiny" :disabled="curPage <= 1" @click="goPage(curPage - 1)">上一页</NButton>
            <span class="pager-page">第 <b>{{ curPage }}</b> / {{ totalPagesHist }} 页</span>
            <NButton size="tiny" :disabled="curPage >= totalPagesHist" @click="goPage(curPage + 1)">下一页</NButton>
            <span class="pager-jump">
              跳至
              <NInput
                v-model:value="jumpInput"
                size="tiny"
                class="pager-jump-input"
                placeholder="页"
                @keyup.enter="jumpPage"
              />
              页
              <NButton size="tiny" type="primary" @click="jumpPage">Go</NButton>
            </span>
            <span v-if="jumpErr" class="pager-jump-err">{{ jumpErr }}</span>
          </div>
        </section>
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
.metrics { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.panel {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px;
}
.panel h3 { margin: 0 0 10px; font-size: 14px; }
.hint { font-size: 12px; padding: 8px 0; }
.active-list { display: flex; flex-direction: column; gap: 10px; }
.hist-head { display: flex; align-items: center; justify-content: space-between; }
.hist-head h3 { margin: 0 0 10px; }
.hist-filter { width: 140px; }
.table-scroll { overflow-x: auto; }
.t-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.t-table th {
  text-align: left; font-weight: 400; color: var(--sun-text-tertiary);
  padding: 6px 8px; border-bottom: 1px solid var(--sun-border); white-space: nowrap;
}
.t-table td { padding: 7px 8px; border-bottom: 1px dashed rgba(16, 49, 74, 0.6); vertical-align: middle; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
.err-text { color: var(--sun-error-text); }
.detail-cell { max-width: 360px; }
.status-badge {
  display: inline-block; font-size: 11px; padding: 0 8px; border-radius: 10px; border: 1px solid;
}
.st-ok { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.st-info { color: var(--sun-info-text, var(--sun-ok-text)); border-color: var(--sun-info-border, var(--sun-ok-border)); background: var(--sun-info-bg, var(--sun-ok-bg)); }
.st-warn { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.st-error { color: var(--sun-error-text); border-color: var(--sun-error-border); background: var(--sun-error-bg); }
.st-muted { color: var(--sun-text-tertiary); border-color: var(--sun-border); }
.pager { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 10px; font-size: 12px; color: var(--sun-text-secondary); flex-wrap: wrap; }
.pager-total { font-family: var(--sun-font-mono); color: var(--sun-text-tertiary); }
.pager-page b { color: var(--sun-border-active); font-family: var(--sun-font-mono); }
.pager-jump { display: inline-flex; align-items: center; gap: 6px; margin-left: 8px; }
.pager-jump-input { width: 64px; }
.pager-jump-err { color: var(--sun-error-text); }
</style>
