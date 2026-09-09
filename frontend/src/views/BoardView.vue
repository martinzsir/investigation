<script setup lang="ts">
// FE-P-009 处置看板（MVP-2「能批量」）：五列卡片 + 超期红框 + 卡内直接迁移状态。
// 演示路径：看板看超期 → 卡片迁移 → 审计链有记录（写操作唯一通道 ActionExecutor，
// 前端走 cluesApi.action + 状态机确认弹窗）。
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NSpin, NButton, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import { cluesApi, type ClueListItem } from '../api/endpoints/clues'
import { presentError } from '../api/errors'
import { toBoardCard, type BoardCard } from '../domain/board'
import type { ClueAction } from '../domain/clue'
import DisposalBoard from '../components/board/DisposalBoard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const health = useHealthStore()
const router = useRouter()
const message = useMessage()

const loading = ref(false)
const errorMsg = ref('')
const cards = ref<BoardCard[]>([])
const busyId = ref('')

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    cards.value = []
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    // 看板按状态分列，取大批量（page_size=200）；默认服务端时间倒序
    const page = await cluesApi.list(cs.currentCaseId, { page: 1, page_size: 200 })
    cards.value = (page.items as ClueListItem[]).map((c) => toBoardCard(c))
  } catch (e) {
    errorMsg.value = presentError(e).title
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

async function onSubmit(clueId: string, payload: { action: ClueAction; note?: string; reason?: string; legal_basis?: string }): Promise<void> {
  if (!cs.currentCaseId) return
  busyId.value = clueId
  try {
    const res = await cluesApi.action(cs.currentCaseId, clueId, payload)
    message.success(`处置请求已入队（任务 ${res.task_id ?? res.status ?? '已受理'}），看板与审计链稍后更新`, { duration: 4000 })
    await load()
  } catch (e) {
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
