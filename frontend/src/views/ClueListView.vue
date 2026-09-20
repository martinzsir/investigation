<script setup lang="ts">
// FE-P-004/FE-C-003 线索列表（MVP-2 分页化）：DataTable 分页（page_size=50）、
// ?page= 可分享（URL query 同步）、跳页输入框；默认服务端时间倒序。
// 镜头闭环（启停/定向批次）：?lens= 镜头筛选可分享（画布定向完成横幅深链）、
// 定向线索带「定向」徽标；BUILD/RESCAN/LENS_RUN 任务完成后自动刷新列表。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NSelect, NInput, NButton, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { cluesApi, type ClueListPage, type ClueListItem } from '../api/endpoints/clues'
import { lensesApi, type LensSpecItem } from '../api/endpoints/lenses'
import { tasksApi } from '../api/endpoints/tasks'
import { CLUE_STATUS } from '../domain/clue'
import { PAGE_SIZE_DEFAULT, clampPage, parsePageQuery } from '../domain/pagination'
import StatusBadge from '../components/common/StatusBadge.vue'
import EmptyState from '../components/common/EmptyState.vue'
import DataTable, { type DataTableColumn } from '../components/common/DataTable.vue'

const cs = useCaseStore()
const route = useRoute()
const router = useRouter()
const message = useMessage()

const loading = ref(false)
const errorMsg = ref('')
const page = ref<ClueListPage | null>(null)
const statusFilter = ref<string | null>(null)
const levelFilter = ref<string | null>(null)
/**
 * 关键词（?q= 可分享）：顶部全局检索框深链落到本页。
 * 后端 subject 参数对「线索标题 + detail 全文」做子串匹配，故语义是
 * 「线索关键词检索」，不是全网搜索——placeholder 不得承诺搜案件/人员/证据。
 */
const qFilter = ref(String(route.query.q ?? ''))
/**
 * 镜头筛选（?lens= 可分享）：''=全部；'__run__'=仅定向镜头运行线索；
 * 其余值=按产出技能（skill_id）过滤。画布定向完成横幅按 skill 深链到本页。
 */
const LENS_RUN_ALL = '__run__'
const lensFilter = ref(String(route.query.lens ?? ''))
const lenses = ref<LensSpecItem[]>([])

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
const lensOptions = computed(() => [
  { label: '全部线索', value: '' },
  { label: '仅定向镜头产出', value: LENS_RUN_ALL },
  ...lenses.value.map((l) => ({
    label: l.requires_params ? `${l.name}（定向）` : l.name,
    value: l.skill_id,
  })),
])
/** skill_id → 镜头名（定向徽标旁显示；清单未加载/未收录回落 skill_id） */
function lensName(skillId: string | null | undefined): string {
  if (!skillId) return ''
  return lenses.value.find((l) => l.skill_id === skillId)?.name ?? skillId
}

/** 定向徽标 tooltip：镜头名 + 运行时间/触发人（有则显示） */
function lensBadgeTitle(item: ClueListItem): string {
  const parts = [`定向镜头运行产出：${lensName(item.skill_id)}`]
  if (item.lens_run_at) parts.push(item.lens_run_at)
  if (item.lens_operator) parts.push(`触发人 ${item.lens_operator}`)
  return parts.join(' · ')
}

async function loadLenses(): Promise<void> {
  if (!cs.currentCaseId) {
    lenses.value = []
    return
  }
  try {
    const r = await lensesApi.list(cs.currentCaseId)
    lenses.value = r.lenses ?? []
  } catch {
    lenses.value = [] // 镜头清单失败不阻塞线索列表（徽标回落 skill_id）
  }
}

const columns: DataTableColumn[] = [
  { key: 'priority_rank', title: '#', width: '48px', mono: true },
  { key: 'priority_score', title: '优先级分', width: '80px', mono: true },
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
      subject: qFilter.value.trim() || undefined,
      // 镜头筛选：'__run__'=仅定向；其余=按产出技能过滤
      skill: lensFilter.value && lensFilter.value !== LENS_RUN_ALL
        ? lensFilter.value
        : undefined,
      lensRun: lensFilter.value === LENS_RUN_ALL || undefined,
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

// ----------------------------------------------------------------------
// 任务完成闭环：BUILD/RESCAN/LENS_RUN 终态后线索读面可能变化（新产物版本
// /定向并线），8s 轻轮询活跃任务，相关任务离开活跃集即刷新 + 提示。
// 只比较任务 id 集合（不拉任务详情），轮询失败静默（下轮再试）。
// 声明先于 currentCaseId watch（immediate 回调同步执行，避免 TDZ）。
// ----------------------------------------------------------------------
const REFRESH_TASK_TYPES = new Set(['BUILD', 'RESCAN', 'LENS_RUN'])
const POLL_MS = 8000
const seenActiveIds = new Set<string>()
let pollTimer: ReturnType<typeof setInterval> | null = null

async function pollActiveTasks(): Promise<void> {
  if (!cs.currentCaseId || document.visibilityState === 'hidden') return
  try {
    const r = await tasksApi.list(cs.currentCaseId, {
      status: 'active',
      pageSize: 50,
    })
    const nowActive = new Set(
      r.items.filter((t) => REFRESH_TASK_TYPES.has(t.task_type)).map((t) => t.id),
    )
    let finished = false
    for (const id of seenActiveIds) {
      if (!nowActive.has(id)) finished = true
    }
    seenActiveIds.clear()
    for (const id of nowActive) seenActiveIds.add(id)
    if (finished) {
      await load()
      message.info('检测/镜头任务已完成，线索列表已刷新', { duration: 4000 })
    }
  } catch {
    /* 轮询失败静默：读面刷新是增强，不弹错误打扰 */
  }
}

watch(() => cs.currentCaseId, () => {
  seenActiveIds.clear() // 换案重置活跃任务快照，避免跨案误判“任务完成”
  void load()
  void loadLenses()
}, { immediate: true })
watch(
  () => route.query.page,
  () => {
    if (cs.currentCaseId) void load()
  },
)
// 画布定向完成横幅等外部深链改 ?lens= 时同步并重载；本页自己改的不重复加载
watch(
  () => route.query.lens,
  (v) => {
    const next = String(v ?? '')
    if (next === lensFilter.value) return
    lensFilter.value = next
    if (cs.currentCaseId) void load()
  },
)

onMounted(() => {
  pollTimer = setInterval(() => void pollActiveTasks(), POLL_MS)
})
onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
// 外部深链（顶部全局检索）改 ?q= 时同步并重载；本页自己改的不重复加载
watch(
  () => route.query.q,
  (v) => {
    const next = String(v ?? '')
    if (next === qFilter.value) return
    qFilter.value = next
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

/** 筛选变化：回第 1 页并清 URL page；关键词/镜头同步进 ?q=/?lens= 保持可分享 */
function query(): void {
  void router.replace({
    query: {
      ...route.query,
      page: undefined,
      q: qFilter.value.trim() || undefined,
      lens: lensFilter.value || undefined,
    },
  })
  void load()
}

/** 带 ?from=（含 ?page= / ?q=）进入详情：返回时回到原分页与关键词，不必重筛 */
function openClue(item: ClueListItem): void {
  void router.push({
    path: `/c/clue/${encodeURIComponent(item.clue_id)}`,
    query: { from: route.fullPath },
  })
}
/** 空态说明按是否带关键词分支：关键词无匹配 ≠ 零命中，两者处置完全不同 */
const emptyDesc = computed(() => {
  const kw = qFilter.value.trim()
  if (kw) {
    return `关键词「${kw}」在已产出线索中无匹配。检索范围是线索标题与详情全文；案件、人员、证据另有专门入口。`
  }
  if (lensFilter.value === LENS_RUN_ALL) {
    return '本版本暂无定向镜头运行产出：定向镜头需从研判画布工具栏带参运行（运行完成后线索自动进本列表）。'
  }
  if (lensFilter.value) {
    const name = lensName(lensFilter.value)
    return `镜头「${name}」在本版本暂无线索：定向镜头需画布带参运行；若刚停用，重建完成后其线索即移除。`
  }
  return '可能原因：案件尚未运行分析（BUILD/RESCAN），或规则零命中——零命中诊断请回仪表盘查看'
})
function reset(): void {
  statusFilter.value = null
  levelFilter.value = null
  lensFilter.value = ''
  qFilter.value = ''
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
        <NSelect
          v-model:value="lensFilter"
          :options="lensOptions"
          placeholder="镜头（全部）"
          class="filter-select filter-lens"
          data-testid="clue-lens-filter"
          @update:value="query"
        />
        <NInput
          v-model:value="qFilter"
          size="small"
          class="filter-keyword"
          placeholder="关键词（线索标题 / 详情）"
          clearable
          @keyup.enter="query"
        />
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
          :desc="emptyDesc"
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
          <template #cell-priority_score="{ item }">
            <span
              class="score"
              :class="{ 'score--link': (item as unknown as ClueListItem).score_basis }"
              :title="(item as unknown as ClueListItem).score_source
                ? `计分来源 ${(item as unknown as ClueListItem).score_source}`
                : ''"
            >{{ (item as unknown as ClueListItem).priority_score ?? '—' }}</span>
          </template>
          <template #cell-title="{ item }">
            <span class="title-cell">
              <span class="title">
                {{ (item as unknown as ClueListItem).title }}
                <NTag
                  v-if="(item as unknown as ClueListItem).lens_run_id"
                  size="tiny"
                  type="info"
                  round
                  class="lens-tag"
                  :title="lensBadgeTitle(item as unknown as ClueListItem)"
                  data-testid="clue-lens-badge"
                >定向</NTag>
              </span>
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
.filter-lens {
  width: 180px;
}
.lens-tag {
  margin-left: 6px;
  vertical-align: 1px;
  cursor: help;
}
.filter-keyword {
  width: 240px;
}
.rank {
  font-family: var(--sun-font-mono);
  color: var(--sun-warn-text);
}
.score {
  font-family: var(--sun-font-mono);
  color: var(--sun-text-secondary);
}
.score--link {
  color: var(--sun-warn-text);
  font-weight: 600;
  cursor: help;
  border-bottom: 1px dotted currentColor;
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
