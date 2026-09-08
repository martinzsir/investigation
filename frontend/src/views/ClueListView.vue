<script setup lang="ts">
// FE-P-004 线索列表（MVP-1 简版）：状态/级别筛选 + 表格，点击进详情。
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NSpin, NSelect, NButton } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { cluesApi, type ClueListPage } from '../api/endpoints/clues'
import { CLUE_STATUS } from '../domain/clue'
import StatusBadge from '../components/common/StatusBadge.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const router = useRouter()

const loading = ref(false)
const errorMsg = ref('')
const page = ref<ClueListPage | null>(null)
const statusFilter = ref<string | null>(null)
const levelFilter = ref<string | null>(null)

const statusOptions = [
  { label: '全部状态', value: '' },
  ...Object.values(CLUE_STATUS).map((s) => ({ label: s, value: s })),
]
const levelOptions = [
  { label: '全部级别', value: '' },
  { label: '观察', value: '观察' },
  { label: '线索', value: '线索' },
  { label: '可立案依据候选', value: '可立案依据候选' },
  { label: '待核实（异常通道）', value: '待核实' },
]

const items = computed(() => page.value?.items ?? [])

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    page.value = null
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    page.value = await cluesApi.list(cs.currentCaseId, {
      page: 1,
      page_size: 100,
      status: statusFilter.value ?? undefined,
      level: levelFilter.value ?? undefined,
    })
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '线索加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

function openClue(id: string): void {
  void router.push(`/c/clue/${encodeURIComponent(id)}`)
}
function reset(): void {
  statusFilter.value = null
  levelFilter.value = null
  void load()
}
</script>

<template>
  <div class="page">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="线索按案件归属，在顶部案件选择器中选择后加载"
    />
    <template v-else>
      <div class="page-head">
        <h2>线索列表</h2>
        <span class="case-name">{{ cs.currentCase?.name ?? cs.currentCaseId }}</span>
      </div>

      <div class="filters">
        <NSelect v-model:value="statusFilter" :options="statusOptions" placeholder="全部状态" class="filter-select" />
        <NSelect v-model:value="levelFilter" :options="levelOptions" placeholder="全部级别" class="filter-select" />
        <NButton size="small" type="primary" @click="load">查询</NButton>
        <NButton size="small" @click="reset">重置</NButton>
        <span v-if="page" class="total">共 {{ page.total }} 条</span>
      </div>

      <NSpin :show="loading">
        <EmptyState v-if="errorMsg" type="error" title="线索加载失败" :desc="errorMsg">
          <template #action><NButton size="small" @click="load">重试</NButton></template>
        </EmptyState>
        <EmptyState
          v-else-if="!loading && !items.length"
          type="empty"
          title="当前筛选下无线索"
          desc="可能原因：案件尚未运行分析（BUILD/RESCAN），或规则零命中——零命中诊断请回仪表盘查看"
        />
        <table v-else class="clue-table">
          <thead>
            <tr>
              <th style="width:44px">#</th>
              <th>线索</th>
              <th>级别</th>
              <th>通道</th>
              <th>状态</th>
              <th>溯源行数</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in items" :key="c.clue_id" class="clue-row" @click="openClue(c.clue_id)">
              <td class="rank">{{ c.priority_rank ?? '—' }}</td>
              <td class="title-cell">
                <span class="title">{{ c.title }}</span>
                <code class="cid">{{ c.clue_id }}</code>
              </td>
              <td><StatusBadge variant="level" :value="c.level ?? '观察'" /></td>
              <td>
                <StatusBadge v-for="d in (c.dimension ?? []).slice(0, 5)" :key="d" variant="room" :value="d" class="room-cell" />
              </td>
              <td><StatusBadge variant="status" :value="c.status" /></td>
              <td class="num-cell">{{ c.source_row_count ?? 0 }}</td>
              <td class="time-cell">{{ c.updated_at ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
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
.filters {
  display: flex;
  align-items: center;
  gap: 10px;
}
.filter-select {
  width: 200px;
}
.total {
  margin-left: auto;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.clue-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
}
.clue-table th {
  text-align: left;
  font-weight: 400;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  padding: 8px 10px;
  border-bottom: 1px solid var(--sun-border);
}
.clue-table td {
  padding: 9px 10px;
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
}
.title-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 200px;
}
.title {
  color: var(--sun-text-primary);
}
.cid {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.room-cell {
  margin-right: 4px;
}
.num-cell,
.time-cell {
  font-family: var(--sun-font-mono);
  color: var(--sun-text-secondary);
  font-size: 12px;
  white-space: nowrap;
}
</style>
