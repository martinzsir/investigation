<script setup lang="ts">
// FE-P-004/FE-C-003 线索列表（MVP-2 分页化）：DataTable 分页（page_size=50）、
// ?page= 可分享（URL query 同步）、跳页输入框；默认服务端时间倒序。
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NSelect, NButton } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { cluesApi, type ClueListPage, type ClueListItem } from '../api/endpoints/clues'
import { CLUE_STATUS } from '../domain/clue'
import { PAGE_SIZE_DEFAULT, clampPage, parsePageQuery } from '../domain/pagination'
import StatusBadge from '../components/common/StatusBadge.vue'
import EmptyState from '../components/common/EmptyState.vue'
import DataTable, { type DataTableColumn } from '../components/common/DataTable.vue'

const cs = useCaseStore()
const route = useRoute()
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

const columns: DataTableColumn[] = [
  { key: 'priority_rank', title: '#', width: '48px', mono: true },
  { key: 'title', title: '线索' },
  { key: 'level', title: '级别', width: '130px' },
  { key: 'dimension', title: '通道', width: '200px' },
  { key: 'status', title: '状态', width: '110px' },
  { key: 'source_row_count', title: '溯源行数', width: '90px', mono: true },
  { key: 'updated_at', title: '更新时间', width: '160px', mono: true },
]

const curPage = computed(() => parsePageQuery(route.query.page))
const items = computed<ClueListItem[]>(() => page.value?.items ?? [])

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    page.value = null
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await cluesApi.list(cs.currentCaseId, {
      page: curPage.value,
      page_size: PAGE_SIZE_DEFAULT,
      status: statusFilter.value ?? undefined,
      level: levelFilter.value ?? undefined,
    })
    // URL ?page= 超出范围（可分享链接场景）：钳制回合法页并同步 URL
    if (res.items.length === 0 && res.total > 0 && curPage.value > 1) {
      const clamped = clampPage(curPage.value, res.total, PAGE_SIZE_DEFAULT)
      void router.replace({ query: { ...route.query, page: clamped > 1 ? String(clamped) : undefined } })
      return // route.query.page watcher 会重新 load
    }
    page.value = res
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '线索加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })
watch(
  () => route.query.page,
  () => {
    if (cs.currentCaseId) void load()
  },
)

/** 翻页：同步 URL（?page= 可分享），不刷筛选 */
function onPageChange(p: number): void {
  const target = clampPage(p, page.value?.total ?? 0, PAGE_SIZE_DEFAULT)
  void router.replace({
    query: { ...route.query, page: target > 1 ? String(target) : undefined },
  })
}

/** 筛选变化：回第 1 页并清 URL page */
function query(): void {
  if (curPage.value !== 1) {
    void router.replace({ query: { ...route.query, page: undefined } })
  }
  void load()
}

function openClue(item: ClueListItem): void {
  void router.push(`/c/clue/${encodeURIComponent(item.clue_id)}`)
}
function reset(): void {
  statusFilter.value = null
  levelFilter.value = null
  void router.replace({ query: {} })
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
        <NButton size="small" type="primary" @click="query">查询</NButton>
        <NButton size="small" @click="reset">重置</NButton>
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
        <DataTable
          v-else
          :columns="columns"
          :items="(items as unknown as Record<string, unknown>[])"
          :total="page?.total ?? 0"
          :page="curPage"
          :page-size="PAGE_SIZE_DEFAULT"
          :loading="loading"
          :row-key="(item: Record<string, unknown>) => String(item.clue_id)"
          @update:page="onPageChange"
          @row-click="(item: Record<string, unknown>) => openClue(item as unknown as ClueListItem)"
        >
          <template #cell-priority_rank="{ item }">
            <span class="rank">{{ (item as unknown as ClueListItem).priority_rank ?? '—' }}</span>
          </template>
          <template #cell-title="{ item }">
            <span class="title-cell">
              <span class="title">{{ (item as unknown as ClueListItem).title }}</span>
              <span v-if="(item as unknown as ClueListItem).basis" class="basis-sub">{{ (item as unknown as ClueListItem).basis }}</span>
              <code class="cid">{{ (item as unknown as ClueListItem).clue_id }}</code>
            </span>
          </template>
          <template #cell-level="{ item }">
            <StatusBadge variant="level" :value="(item as unknown as ClueListItem).level ?? '观察'" />
          </template>
          <template #cell-dimension="{ item }">
            <StatusBadge
              v-for="d in ((item as unknown as ClueListItem).dimension ?? []).slice(0, 5)"
              :key="d"
              variant="room"
              :value="d"
              class="room-cell"
            />
          </template>
          <template #cell-status="{ item }">
            <StatusBadge variant="status" :value="(item as unknown as ClueListItem).status" />
          </template>
        </DataTable>
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
.room-cell {
  margin-right: 4px;
}
</style>
