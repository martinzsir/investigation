<script setup lang="ts">
// FE-P-005 实体裁决（MVP-2「能批量」）：键盘流 A/R/D 连续裁决 + 无限加载。
// 红线：永不自动合并；逐条人工裁决，驳回理由必填；每条裁决落操作者+理由（审计链）。
// 空态：未发现实体歧义。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  reviewApi,
  type ReviewCandidate,
  type ReviewEvidence,
  type ReviewHistoryItem,
} from '../api/endpoints/review'
import { presentError } from '../api/errors'
import { hasMore, mergeQueuePage, VERDICT_API_ACTION, type VerdictAction } from '../domain/review'
import EntityCompare from '../components/review/EntityCompare.vue'
import LlmInferenceCard from '../components/review/LlmInferenceCard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const PAGE_SIZE = 20

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const busy = ref(false)
const errorMsg = ref('')
const loaded = ref(false)

const queue = ref<ReviewCandidate[]>([])
const total = ref(0)
const serverPage = ref(0)
const cursor = ref(0)
const evidence = ref<ReviewEvidence | null>(null)
const history = ref<ReviewHistoryItem[]>([])
const /** 已跳过（D）的候选 id：游标不再回指 */
deferred = new Set<string>()

const current = computed<ReviewCandidate | null>(() => queue.value[cursor.value] ?? null)
const pendingCount = computed(() => Math.max(0, total.value - history.value.length - deferred.size))
const more = computed(() => hasMore(queue.value.length, total.value))

async function loadFirst(): Promise<void> {
  if (!cs.currentCaseId) {
    queue.value = []
    evidence.value = null
    history.value = []
    loaded.value = false
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    const [q, hist] = await Promise.all([
      reviewApi.queue(cs.currentCaseId, 1, PAGE_SIZE),
      reviewApi.history(cs.currentCaseId).catch(() => ({ items: [], total: 0 })),
    ])
    queue.value = q.items
    total.value = q.total
    serverPage.value = 1
    cursor.value = 0
    history.value = q.history ?? hist.items
    loaded.value = true
    await loadEvidence()
  } catch (e) {
    errorMsg.value = presentError(e).title
    loaded.value = true
  } finally {
    loading.value = false
  }
}

async function loadMore(): Promise<void> {
  if (!cs.currentCaseId || !more.value || loading.value) return
  loading.value = true
  try {
    const q = await reviewApi.queue(cs.currentCaseId, serverPage.value + 1, PAGE_SIZE)
    queue.value = mergeQueuePage(queue.value, q)
    total.value = q.total
    serverPage.value += 1
    // history 由专用端点获取（不再从 queue 返回）
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    loading.value = false
  }
}

async function loadEvidence(): Promise<void> {
  if (!cs.currentCaseId || !current.value) {
    evidence.value = null
    return
  }
  try {
    evidence.value = await reviewApi.evidence(cs.currentCaseId, current.value.entity_id)
  } catch (e) {
    // 单条证据失败不阻断裁决流：提示并跳过该条
    message.error(presentError(e).title)
    evidence.value = null
  }
}

watch(() => cs.currentCaseId, loadFirst, { immediate: true })
watch(current, loadEvidence)

function nowText(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function advance(): Promise<void> {
  const next = cursor.value + 1
  // 无限加载：游标到已加载末尾且服务端还有 → 拉下一页
  if (next >= queue.value.length && more.value) {
    await loadMore()
  }
  cursor.value = next
}

async function onDecide(action: VerdictAction, reason: string): Promise<void> {
  if (!cs.currentCaseId || !current.value) return
  busy.value = true
  try {
    await reviewApi.decide(cs.currentCaseId, current.value.entity_id, {
      action: VERDICT_API_ACTION[action],
      reason: reason || undefined,
    })
    // 乐观落历史记录（操作者取会话；服务端裁决完成后审计链可查）
    const rec: ReviewHistoryItem = {
      candidate_id: current.value.entity_id,
      canonical_name: current.value.canonical_name,
      action,
      confidence: current.value.confidence,
      operator: auth.operator || '当前会话',
      reason: reason || undefined,
      occurred_at: nowText(),
    }
    history.value = [rec, ...history.value]
    message.success(action === 'merge' ? '已裁决：确认为同一人' : '已裁决：确认为不同人')
    await advance()
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    busy.value = false
  }
}

async function onDefer(): Promise<void> {
  if (current.value) deferred.add(current.value.entity_id)
  await advance()
}
</script>

<template>
  <div class="page">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="实体裁决按案件归属，在顶部案件选择器中选择后加载"
    />
    <template v-else>
      <div class="page-head">
        <h2>实体裁决</h2>
        <span class="case-name">{{ cs.currentCase?.name ?? cs.currentCaseId }}</span>
        <NButton size="small" class="refresh" :loading="loading" @click="loadFirst">刷新</NButton>
      </div>

      <NSpin :show="loading && !evidence">
        <EmptyState v-if="errorMsg" type="error" title="裁决队列加载失败" :desc="errorMsg">
          <template #action><NButton size="small" @click="loadFirst">重试</NButton></template>
        </EmptyState>
        <EmptyState
          v-else-if="loaded && !current"
          type="empty"
          title="未发现实体歧义"
          desc="实体归集无待裁决候选（案件尚未 BUILD 或候选均已裁决）"
        />
        <template v-else-if="current">
          <EntityCompare
            v-if="evidence"
            :key="current.entity_id"
            :candidate="evidence"
            :history="history"
            :operator="auth.operator"
            :busy="busy"
            :pending-count="pendingCount"
            @decide="onDecide"
            @defer="onDefer"
          />
          <div v-else class="ev-missing">
            <p>该候选证据详情暂不可取（可能案件尚未 BUILD）。</p>
            <NButton size="small" @click="onDefer">跳过（D）</NButton>
          </div>

          <!-- LLM 判读参考：三件套卡片（FE-C-023），归属间标签即 FE-T-007 同源去重载体 -->
          <section v-if="evidence?.llm_inferences?.length" class="llm-section">
            <h4>LLM 判读参考</h4>
            <p class="llm-note">同一模型的多条判读交叉计数只算一个间（FE-T-007），不构成独立信源。</p>
            <LlmInferenceCard
              v-for="(inf, i) in evidence.llm_inferences"
              :key="i"
              :inference="inf"
            />
          </section>

          <div v-if="more && !loading" class="load-more">
            <NButton size="small" @click="loadMore">加载更多候选（已载 {{ queue.length }} / {{ total }}）</NButton>
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
.ev-missing {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 24px;
  text-align: center;
  color: var(--sun-text-secondary);
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: center;
}
.llm-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.llm-section h4 {
  margin: 0;
  font-size: 14px;
  color: var(--sun-text-primary);
}
.llm-note {
  margin: 0;
  font-size: 12px;
  color: var(--sun-warn-text);
}
.load-more {
  display: flex;
  justify-content: center;
  padding: 8px 0;
}
</style>
