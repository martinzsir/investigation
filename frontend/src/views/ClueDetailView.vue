<script setup lang="ts">
// FE-P-004 线索详情（MVP-1 闭环核心）：三栏证据 + 溯源抽屉 + 处置状态机。
// 演示路径：仪表盘 → 点线索 → 三栏（青/琥珀/灰虚线）→ 溯源抽屉 → 处置确认 → 审计回执。
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NButton, NDrawer, NDrawerContent, NAlert, NIcon, useMessage } from 'naive-ui'
import { ArrowBackOutline, DocumentTextOutline, LockClosedOutline } from '@vicons/ionicons5'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import { cluesApi, type ClueDetail } from '../api/endpoints/clues'
import { verifyApi, type VerifyTransitionBody } from '../api/endpoints/verify'
import { waitForTerminal } from '../api/endpoints/tasks'
import { failureSummary, type TaskRow } from '../domain/task'
import {
  type VerifyItem,
  type VerifyItemsPage,
  type VerifyProgress,
} from '../domain/verify'
import { presentError } from '../api/errors'
import {
  type EvidenceItem, type ClueAction, scoreBasisRows,
} from '../domain/clue'
import StatusBadge from '../components/common/StatusBadge.vue'
import EmptyState from '../components/common/EmptyState.vue'
import ThreeColumnEvidence from '../components/research/ThreeColumnEvidence.vue'
import TraceabilityPanel from '../components/research/TraceabilityPanel.vue'
import ClueStatusMachine from '../components/research/ClueStatusMachine.vue'
import VerifyWorkbench from '../components/research/VerifyWorkbench.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const cs = useCaseStore()
const auth = useAuthStore()
const health = useHealthStore()

const clueId = computed(() => String(route.params.clueId ?? ''))
const loading = ref(false)
const submitting = ref(false)
const notFound = ref(false)
const forbidden = ref(false)
const errorMsg = ref('')
const detail = ref<ClueDetail | null>(null)
const drawerOpen = ref(false)
// REQ-V-006：核查工作区写请求与刷新
const verifyRef = ref<InstanceType<typeof VerifyWorkbench> | null>(null)
const verifySubmitting = ref(false)
// REQ-V-007：工作台清单回传 → 三栏待核实卡「文本→状态」映射（供给侧文本原样入库，按文本精确匹配）
const verifyItems = ref<VerifyItem[]>([])
// REQ-V-008：门禁计数直接取后端 progress.pending（与 Worker verify_progress 同源，
// 不在客户端重复口径；未加载前缺省 0，等同无未结项）
const verifyProgress = ref<VerifyProgress | null>(null)
const verifyStatusByText = computed(() => {
  const m = new Map<string, string>()
  for (const it of verifyItems.value) m.set(it.text, it.status)
  return m
})
const verifyPendingCount = computed(() => verifyProgress.value?.pending ?? 0)

function onVerifyLoaded(page: VerifyItemsPage): void {
  verifyItems.value = page.items ?? []
  verifyProgress.value = page.progress ?? null
}

/** 进行中的终态等待：组件卸载时中断，避免卸载后写响应式状态 */
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

/** 终态任务分支提示；返回 true 表示调用方应刷新读面（仅 SUCCEEDED/CANCELLED 语义） */
function reportDisposeResult(task: TaskRow): 'reload' | 'refresh-verify' | 'none' {
  if (task.status === 'SUCCEEDED') {
    message.success('处置完成，线索状态与审计链已更新', { duration: 4000 })
    return 'reload'
  }
  if (task.status === 'CANCELLED') {
    message.warning('处置任务已取消，线索状态未变更', { duration: 5000 })
    return 'none'
  }
  // FAILED：门禁/权限/状态机拒绝均不产生状态变更，不做乐观刷新
  if (task.error_code === 'VERIFY_PENDING') {
    message.error(
      `处置被核查门禁拦截：${task.error_message || '尚有核查项未结'}`,
      { duration: 7000 },
    )
    // 同步工作区（可能在他处已有裁决），让警示计数回到真值
    return 'refresh-verify'
  }
  message.error(`处置未执行：${failureSummary(task)}`, { duration: 6000 })
  return 'none'
}

function reportVerifyResult(kindLabel: string, task: TaskRow): boolean {
  if (task.status === 'SUCCEEDED') {
    message.success(`${kindLabel}已完成`, { duration: 3000 })
    return true
  }
  if (task.status === 'CANCELLED') {
    message.warning(`${kindLabel}任务已取消`, { duration: 4000 })
    return false
  }
  message.error(`${kindLabel}失败：${failureSummary(task)}`, { duration: 6000 })
  return false
}

/** 三栏待核实卡点击：命中核查项则滚动高亮；未命中由工作台预填人工添加框 */
function onPendingVerify(text: string): void {
  void verifyRef.value?.jumpTo(text)
}

const evidence = computed<EvidenceItem[]>(() => {
  const ev = detail.value?.evidence
  return Array.isArray(ev) ? ev : []
})

const suppressedCount = computed(() => (detail.value?.suppressed_log as unknown[] | undefined)?.length ?? 0)

// R13：优先级分分解（旧产物无 score_basis → 面板不渲染）
const basisRows = computed(() => scoreBasisRows(detail.value?.score_basis))

async function load(): Promise<void> {
  if (!cs.currentCaseId || !clueId.value) return
  loading.value = true
  notFound.value = false
  forbidden.value = false
  errorMsg.value = ''
  try {
    detail.value = await cluesApi.detail(cs.currentCaseId, clueId.value)
  } catch (e) {
    const p = presentError(e)
    // 内间线索秩级过滤：后端对越权访问统一 404（不暴露线索存在性）
    if (p.kind === 'notfound') notFound.value = true
    else if (p.kind === 'permission') forbidden.value = true
    else errorMsg.value = p.title
  } finally {
    loading.value = false
  }
}

watch([() => cs.currentCaseId, clueId], load, { immediate: true })

async function onSubmit(payload: { action: ClueAction; note?: string; reason?: string; legal_basis?: string }): Promise<void> {
  if (!detail.value || !cs.currentCaseId) return
  submitting.value = true
  try {
    const res = await cluesApi.action(cs.currentCaseId, detail.value.clue_id, payload)
    // 202 仅代表入队：必须等到 Worker 终态再提示/刷新，
    // 否则 VERIFY_PENDING 等异步失败对用户不可见（REQ-V-008 闭环）
    const task = await waitForTerminal(res.id, { signal: beginWait() })
    switch (reportDisposeResult(task)) {
      case 'reload':
        await load()
        await verifyRef.value?.refresh()
        break
      case 'refresh-verify':
        await verifyRef.value?.refresh()
        break
      case 'none':
        break
    }
  } catch (e) {
    if (isAbort(e)) return
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    submitting.value = false
  }
}

// REQ-V-006：核查工作区写动作（202 入队 TASK_VERIFY，终态后才拉新清单——
// 入队即刷新会读到 Worker 消费前的旧 state，门禁计数/列表出现竞态旧值）
async function onVerifyAdd(payload: { text: string }): Promise<void> {
  if (!detail.value || !cs.currentCaseId) return
  verifySubmitting.value = true
  try {
    const res = await verifyApi.add(
      cs.currentCaseId, detail.value.clue_id, payload.text)
    const task = await waitForTerminal(res.id, { signal: beginWait() })
    if (reportVerifyResult('核查项添加', task)) {
      await verifyRef.value?.refresh()
    }
  } catch (e) {
    if (isAbort(e)) return
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    verifySubmitting.value = false
  }
}

async function onVerifyTransition(payload: VerifyTransitionBody & { item_id: string }): Promise<void> {
  if (!detail.value || !cs.currentCaseId) return
  verifySubmitting.value = true
  try {
    const res = await verifyApi.transition(
      cs.currentCaseId, detail.value.clue_id, payload.item_id, {
        next_status: payload.next_status,
        conclusion: payload.conclusion,
        text: payload.text,
      })
    const task = await waitForTerminal(res.id, { signal: beginWait() })
    if (reportVerifyResult('核查裁决', task)) {
      await verifyRef.value?.refresh()
    }
  } catch (e) {
    if (isAbort(e)) return
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    verifySubmitting.value = false
  }
}
</script>

<template>
  <div class="page">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="线索详情按案件归属，在顶部案件选择器中选择后加载"
    />
    <template v-else>
      <NSpin :show="loading">
        <EmptyState
          v-if="notFound"
          type="forbidden"
          title="线索不存在或无权查看"
          desc="内间线索按秩级过滤（正兵及以下不可见）；后端对越权访问返回 404，不暴露线索存在性"
        >
          <template #action>
            <NButton size="small" @click="router.push('/c/clues')">返回线索列表</NButton>
          </template>
        </EmptyState>
        <EmptyState
          v-else-if="forbidden"
          type="forbidden"
          title="无操作权限"
          desc="当前会话角色无权访问该线索或执行该处置动作（fail-closed）"
        >
          <template #action>
            <NButton size="small" @click="router.push('/c/clues')">返回线索列表</NButton>
          </template>
        </EmptyState>
        <EmptyState v-else-if="errorMsg" type="error" title="线索加载失败" :desc="errorMsg">
          <template #action><NButton size="small" @click="load">重试</NButton></template>
        </EmptyState>

        <template v-else-if="detail">
          <!-- 头部 -->
          <div class="detail-head">
            <NButton text size="small" class="back" @click="router.push('/c/clues')">
              <NIcon :component="ArrowBackOutline" /> 返回列表
            </NButton>
            <h2 class="clue-title">{{ detail.title }}</h2>
            <div class="badges">
              <StatusBadge variant="status" :value="detail.status" />
              <StatusBadge variant="level" :value="detail.level ?? '观察'" />
              <StatusBadge v-for="d in detail.dimension ?? []" :key="d" variant="room" :value="d" />
            </div>
            <div class="meta-line">
              <code class="cid">{{ detail.clue_id }}</code>
              <span v-if="detail.skill_id" class="meta-item">规则 {{ detail.skill_id }}</span>
              <span class="meta-item">溯源 {{ detail.source_row_count ?? detail.source_rows?.length ?? 0 }} 行</span>
              <span v-if="detail.updated_at" class="meta-item">更新 {{ detail.updated_at }}</span>
              <span v-if="detail.status_source" class="meta-item">状态源：{{ detail.status_source === 'state' ? '活状态' : '产物版本' }}</span>
            </div>
          </div>

          <!-- 处置状态机（含立案四重门禁；降级态写禁用） -->
          <section class="panel">
            <header class="panel-head"><h3>处置</h3></header>
            <ClueStatusMachine
              :status="detail.status"
              :role="auth.role"
              :operator="auth.operator"
              :degraded="health.degraded"
              :loading="submitting"
              :verify-pending="verifyPendingCount"
              @submit="onSubmit"
            />
          </section>

          <!-- REQ-V-006 核查工作区：核查项在裁决过程中累积（202 入队 VERIFY） -->
          <section class="panel">
            <header class="panel-head"><h3>核查工作区</h3></header>
            <VerifyWorkbench
              ref="verifyRef"
              :case-id="cs.currentCaseId"
              :clue-id="detail.clue_id"
              :operator="auth.operator"
              :role="auth.role"
              :degraded="health.degraded"
              :submitting="verifySubmitting"
              @add="onVerifyAdd"
              @transition="onVerifyTransition"
              @loaded="onVerifyLoaded"
            />
          </section>

          <!-- R13 计分依据：分数可复算、来源可审计（旧产物无分解则不渲染） -->
          <section v-if="basisRows.length" class="panel">
            <header class="panel-head">
              <h3>计分依据</h3>
              <span class="score-source dim">{{ detail.score_source ?? '' }}</span>
            </header>
            <p v-if="detail.basis" class="score-basis-text">{{ detail.basis }}</p>
            <table class="basis-table">
              <thead>
                <tr><th>维度</th><th>原始值</th><th>权重</th><th>贡献分</th></tr>
              </thead>
              <tbody>
                <tr v-for="r in basisRows" :key="r.key">
                  <td>{{ r.label }}</td>
                  <td class="mono">{{ r.raw.toFixed(3) }}</td>
                  <td class="mono">{{ r.weight }}</td>
                  <td class="mono contrib">{{ r.contrib.toFixed(3) }}</td>
                </tr>
              </tbody>
            </table>
            <div class="formula-line">
              <code v-if="detail.score_formula" class="mono dim">{{ detail.score_formula }}</code>
              <span class="final-score">
                优先级分 <b class="mono">{{ detail.priority_score ?? '—' }}</b>
                <span v-if="detail.priority_rank" class="dim"> · 序号 {{ detail.priority_rank }}</span>
              </span>
            </div>
          </section>

          <!-- 证据三栏 -->
          <section class="panel">
            <header class="panel-head">
              <h3>证据</h3>
              <NButton size="small" @click="drawerOpen = true">
                <NIcon :component="DocumentTextOutline" />
                溯源抽屉（{{ detail.source_rows?.length ?? 0 }} 行）
              </NButton>
            </header>
            <ThreeColumnEvidence
              :items="evidence"
              interactive
              :item-status-by-text="verifyStatusByText"
              @verify="onPendingVerify"
            />
            <NAlert v-if="suppressedCount > 0" type="warning" class="suppressed" :bordered="false">
              {{ suppressedCount }} 条审计记录因当前会话权限不足已遮蔽（未展示，非不存在）
            </NAlert>
          </section>

          <!-- 审计留痕入口 -->
          <section class="panel">
            <header class="panel-head"><h3>留痕</h3></header>
            <p class="trace-link">
              <NIcon :component="LockClosedOutline" />
              处置动作以「{{ auth.operator }}（{{ auth.role }}）」名义写入审计链，不可篡改、不可删除。
              <RouterLink :to="`/c/audit-chain?clue_id=${encodeURIComponent(detail.clue_id)}`">查看本线索审计链 →</RouterLink>
            </p>
          </section>
        </template>
      </NSpin>

      <!-- FE-C-004 溯源抽屉（z-index 900） -->
      <NDrawer v-model:show="drawerOpen" :z-index="900" :width="640">
        <NDrawerContent title="溯源行（行 URI 可复制 · 命中字段琥珀高亮）" closable>
          <TraceabilityPanel :rows="(detail as any)?.source_row_details ?? detail?.source_rows ?? []" />
        </NDrawerContent>
      </NDrawer>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.detail-head {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.back {
  align-self: flex-start;
}
.clue-title {
  margin: 0;
  font-size: 18px;
  color: var(--sun-text-primary);
}
.badges {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.meta-line {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.cid {
  font-family: var(--sun-font-mono);
  color: var(--sun-border-active);
}
.panel {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 14px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.panel-head h3 {
  margin: 0;
  font-size: 14px;
}
.suppressed {
  margin-top: 12px;
  border-radius: 4px;
}
.trace-link {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.trace-link a {
  color: var(--sun-border-active);
  text-decoration: none;
}
/* R13 计分依据 */
.score-source {
  font-family: var(--sun-font-mono);
  font-size: 11px;
}
.score-basis-text {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.basis-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.basis-table th {
  text-align: left;
  font-weight: 400;
  color: var(--sun-text-tertiary);
  padding: 4px 8px;
  border-bottom: 1px solid var(--sun-border);
}
.basis-table td {
  padding: 5px 8px;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.5);
}
.basis-table .contrib {
  color: var(--sun-warn-text);
  font-weight: 600;
}
.formula-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  flex-wrap: wrap;
}
.final-score b {
  color: var(--sun-warn-text);
  font-size: 14px;
}
</style>
