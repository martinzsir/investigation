<script setup lang="ts">
// FE-P-009 处置看板（MVP-2「能批量」）：五列卡片 + 超期红框 + 卡内直接迁移状态。
// 演示路径：看板看超期 → 卡片迁移 → 审计链有记录（写操作唯一通道 ActionExecutor，
// 前端走 cluesApi.action + 状态机确认弹窗）。
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NSpin, NButton, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import { cluesApi, type ClueListItem } from '../api/endpoints/clues'
import { waitForTerminal } from '../api/endpoints/tasks'
import { failureSummary, type TaskRow } from '../domain/task'
import { presentError } from '../api/errors'
import { toBoardCard, type BoardCard } from '../domain/board'
import type { ClueAction } from '../domain/clue'
import { useCaseOntologyConfig } from '../composables/useCaseOntologyConfig'
import DisposalBoard from '../components/board/DisposalBoard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const health = useHealthStore()
const router = useRouter()
const message = useMessage()
const { config: ontologyCfg } = useCaseOntologyConfig()

const loading = ref(false)
const errorMsg = ref('')
const rawItems = ref<ClueListItem[]>([])
const busyId = ref('')

// 声明到达后（异步拉取）即时重算 SLA/超期，不重复请求列表
const cards = computed<BoardCard[]>(() =>
  rawItems.value.map((c) => toBoardCard(c, ontologyCfg.value)),
)

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    rawItems.value = []
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    // 看板按状态分列，取大批量（page_size=200）；默认服务端时间倒序
    const page = await cluesApi.list(cs.currentCaseId, { page: 1, page_size: 200 })
    rawItems.value = page.items as ClueListItem[]
  } catch (e) {
    errorMsg.value = presentError(e).title
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

/** 进行中的终态等待：组件卸载时中断，避免卸载后写 message/响应式状态 */
let waitAbort: AbortController | null = null
function beginWait(): AbortSignal {
  waitAbort?.abort()
  waitAbort = new AbortController()
  return waitAbort.signal
}
onUnmounted(() => waitAbort?.abort())

function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

/** 终态任务分支提示；返回 true 表示调用方应重读看板（仅 SUCCEEDED 变更了状态） */
function reportDisposeResult(task: TaskRow): boolean {
  if (task.status === 'SUCCEEDED') {
    message.success('处置完成，看板列与审计链已更新', { duration: 4000 })
    return true
  }
  if (task.status === 'CANCELLED') {
    message.warning('处置任务已取消，线索状态未变更', { duration: 5000 })
    return false
  }
  // FAILED：门禁/权限/状态机拒绝均不产生状态变更，不做乐观刷新
  if (task.error_code === 'VERIFY_PENDING') {
    message.error(
      `处置被核查门禁拦截：${task.error_message || '尚有核查项未结'}`,
      { duration: 7000 },
    )
    return false
  }
  message.error(`处置未执行：${failureSummary(task)}`, { duration: 6000 })
  return false
}

async function onSubmit(clueId: string, payload: { action: ClueAction; note?: string; reason?: string; legal_basis?: string }): Promise<void> {
  if (!cs.currentCaseId) return
  // 互斥：终态等待期间全看板 busy，禁止第二张卡片并发处置（锁必须包住副作用入口）
  if (busyId.value) return
  busyId.value = clueId
  try {
    const res = await cluesApi.action(cs.currentCaseId, clueId, payload)
    // 202 仅代表「已入队」：必须等到 Worker 终态再提示/刷新（与 ClueDetailView 同源纪律，
    // REQ-V-008 闭环）——否则 VERIFY_PENDING/ACTION_REJECTED 等异步失败对用户不可见，
    // 且入队即抢刷会读到 Worker 消费前的旧 state（卡片留在旧列）。
    const task = await waitForTerminal(res.id, { signal: beginWait() })
    if (reportDisposeResult(task)) await load()
  } catch (e) {
    if (isAbort(e)) return
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    busyId.value = ''
  }
}

function openClue(clueId: string): void {
  void router.push(`/c/clue/${encodeURIComponent(clueId)}`)
}

const busy = computed(() => busyId.value !== '')
</script>

<template>
  <div class="page">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="处置看板按案件归属，在顶部案件选择器中选择后加载"
    />
    <template v-else>
      <div class="page-head">
        <h2>处置看板</h2>
        <span class="case-name">{{ cs.currentCase?.name ?? cs.currentCaseId }}</span>
        <NButton size="small" class="refresh" :loading="loading" @click="load">刷新</NButton>
      </div>

      <NSpin :show="loading">
        <EmptyState v-if="errorMsg" type="error" title="看板加载失败" :desc="errorMsg">
          <template #action><NButton size="small" @click="load">重试</NButton></template>
        </EmptyState>
        <EmptyState
          v-else-if="!loading && !cards.length"
          type="empty"
          title="当前案件暂无处置项"
          desc="案件尚未运行分析（BUILD/RESCAN）或无线索产出——零命中诊断请回仪表盘查看"
        />
        <DisposalBoard
          v-else
          :cards="cards"
          :role="auth.role"
          :operator="auth.operator"
          :degraded="health.degraded"
          :busy="busy"
          @submit="onSubmit"
          @open="openClue"
        />
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
</style>
