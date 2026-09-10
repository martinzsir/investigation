<script setup lang="ts">
// FE-P-004 线索详情（MVP-1 闭环核心）：三栏证据 + 溯源抽屉 + 处置状态机。
// 演示路径：仪表盘 → 点线索 → 三栏（青/琥珀/灰虚线）→ 溯源抽屉 → 处置确认 → 审计回执。
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NButton, NDrawer, NDrawerContent, NAlert, NIcon, useMessage } from 'naive-ui'
import { ArrowBackOutline, DocumentTextOutline, LockClosedOutline } from '@vicons/ionicons5'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import { cluesApi, type ClueDetail } from '../api/endpoints/clues'
import { presentError } from '../api/errors'
import { type EvidenceItem, type ClueAction } from '../domain/clue'
import StatusBadge from '../components/common/StatusBadge.vue'
import EmptyState from '../components/common/EmptyState.vue'
import ThreeColumnEvidence from '../components/research/ThreeColumnEvidence.vue'
import TraceabilityPanel from '../components/research/TraceabilityPanel.vue'
import ClueStatusMachine from '../components/research/ClueStatusMachine.vue'

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

const evidence = computed<EvidenceItem[]>(() => {
  const ev = detail.value?.evidence
  return Array.isArray(ev) ? ev : []
})

const suppressedCount = computed(() => (detail.value?.suppressed_log as unknown[] | undefined)?.length ?? 0)

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
    // DISPOSE 异步任务：202 已入队（进度 SSE 属 MVP-2）；审计链稍后可查
    message.success(
      `处置请求已入队（任务 ${res.task_id ?? res.status ?? '已受理'}），状态与审计链将在处置完成后更新`,
      { duration: 5000 },
    )
    await load()
  } catch (e) {
    const p = presentError(e)
    message.error(p.title, { duration: 5000 })
  } finally {
    submitting.value = false
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
              @submit="onSubmit"
            />
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
            <ThreeColumnEvidence :items="evidence" />
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
</style>
