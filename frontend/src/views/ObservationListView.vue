<script setup lang="ts">
// 观察档案列表（镜头产出的浏览入口）。
//
// 为什么单列一页，不并进线索列表
// --------------------------------
// 镜头产出不是线索：它**摆出数据的某种结构，不下"这是异常"的判断**。
// 分界线不在确定性（规则同样 deterministic、同样可复现），而在
// **有没有常态基线**——规则跟常态中位数比（判定、可证伪），镜头只做
// 结构过滤（观测、不是命题）。观测无假设可证伪，混进线索清单只能空转
// （此前 10 条镜头线索全"查证中"而假设链全空）。
//
// 但也不能没有入口：否则对不上任何已有线索的观察永远没人看见，
// 自动研判"发现未知"的价值会沉没。故单列一页——可浏览、可筛选，
// 但不占处置清单位置、不进看板计数。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NSelect, NInput, NButton, NTag, NEmpty } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import {
  observationsApi,
  type ObservationItem,
  type ObservationDisposition,
} from '../api/endpoints/observations'
import { presentError } from '../api/errors'
import { PAGE_SIZE_DEFAULT, parsePageQuery } from '../domain/pagination'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const route = useRoute()
const router = useRouter()

const loading = ref(false)
const errorMsg = ref('')
const rows = ref<ObservationItem[]>([])
const staleRows = ref<ObservationItem[]>([])
const stats = ref<{
  total: number
  unclaimed: number
  claimed: number
  archived: number
  promoted: number
  by_lens: Record<string, number>
  stale: number
} | null>(null)
const total = ref(0)

// 筛选（均可分享到 URL，与线索列表同纪律）
const skillFilter = ref<string | null>(String(route.query.skill ?? '') || null)
const subjectFilter = ref<string>(String(route.query.subject ?? ''))
const dispositionFilter = ref<string | null>(
  String(route.query.disposition ?? '') || null,
)
const page = ref(parsePageQuery(route.query.page))

/** 处置态选项：未认领是默认待办，排在首位 */
const dispositionOptions = [
  { label: '全部', value: '' },
  { label: '未认领', value: '未认领' },
  { label: '已认领', value: '已认领' },
  { label: '已提升', value: '已提升' },
  { label: '已归档', value: '已归档' },
]

/** 镜头选项：从统计的 by_lens 反推（后端按当前档案动态给，不硬编码） */
const skillOptions = computed(() => {
  const out = [{ label: '全部镜头', value: '' }]
  for (const [name, count] of Object.entries(stats.value?.by_lens ?? {})) {
    out.push({ label: `${name}（${count}）`, value: name })
  }
  return out
})

/** 处置态徽标配色 */
function dispositionType(d: ObservationDisposition): 'default' | 'warning' | 'success' | 'info' {
  if (d === '未认领') return 'warning' // 待办：要人看
  if (d === '已提升') return 'success' // 已成立为线索
  if (d === '已认领') return 'info'
  return 'default'
}

async function load(): Promise<void> {
  if (!cs.currentCaseId) return
  loading.value = true
  errorMsg.value = ''
  try {
    // 后端 skill 参数按 skill_id 过滤，而选项展示的是中文名——
    // 此处按中文名选项时回传空（全部），避免中英文口径不一致导致筛不出。
    const r = await observationsApi.list(cs.currentCaseId, {
      subject: subjectFilter.value || undefined,
      disposition: dispositionFilter.value || undefined,
      page: page.value,
      page_size: PAGE_SIZE_DEFAULT,
    })
    rows.value = r.observations ?? []
    staleRows.value = r.stale ?? []
    stats.value = r.stats ?? null
    total.value = r.total ?? 0
  } catch (e) {
    errorMsg.value = presentError(e).title
    rows.value = []
  } finally {
    loading.value = false
  }
}

function syncQuery(): void {
  void router.replace({
    query: {
      ...route.query,
      skill: skillFilter.value ?? '',
      subject: subjectFilter.value || '',
      disposition: dispositionFilter.value ?? '',
      page: String(page.value),
    },
  })
}

/** 跳详情 */
function openDetail(row: ObservationItem): void {
  if (row.stale) return // 已不在当前版本的观察无详情可看
  if (row.promoted_clue_id) {
    void router.push(`/c/clue/${row.promoted_clue_id}`)
    return
  }
  void router.push(`/c/observations/${row.observation_id}`)
}

onMounted(load)
watch(
  () => cs.currentCaseId,
  () => {
    page.value = 1
    void load()
  },
)
watch([skillFilter, dispositionFilter], () => {
  page.value = 1
  syncQuery()
  void load()
})
watch(page, () => {
  syncQuery()
  void load()
})
</script>

<template>
  <EmptyState
    v-if="!cs.currentCaseId"
    type="empty"
    title="请先选择案件"
    desc="观察档案按案件隔离——顶部案件选择器选具体案件后才能查看"
  />
  <div v-else class="obs-view">
    <header class="ov-head">
      <div>
        <h2 class="ov-title">观察档案</h2>
        <p class="ov-sub">
          镜头产出的是<strong>观察</strong>，不是线索：只摆出数据的结构，不下「这是异常」的判断。
          认为某条构成疑点，可提升为线索并指定待验证的假设。
        </p>
      </div>
      <div v-if="stats" class="ov-stats">
        <span class="st"><b>{{ stats.total }}</b> 条</span>
        <span class="st st--warn"><b>{{ stats.unclaimed }}</b> 未认领</span>
        <span class="st st--ok"><b>{{ stats.promoted }}</b> 已提升</span>
        <span v-if="stats.stale" class="st st--dim"><b>{{ stats.stale }}</b> 已不在当前版本</span>
      </div>
    </header>

    <div class="ov-filters">
      <NSelect
        v-model:value="dispositionFilter"
        :options="dispositionOptions"
        size="small"
        class="f-sel"
        data-testid="obs-filter-disposition"
      />
      <NSelect
        v-model:value="skillFilter"
        :options="skillOptions"
        size="small"
        class="f-sel"
        data-testid="obs-filter-skill"
      />
      <NInput
        v-model:value="subjectFilter"
        size="small"
        placeholder="按主体 / 项目筛选"
        class="f-in"
        data-testid="obs-filter-subject"
        @keyup.enter="load"
      />
      <NButton size="small" secondary @click="load" data-testid="obs-search">筛选</NButton>
    </div>

    <NSpin :show="loading">
      <div v-if="errorMsg" class="ov-err">{{ errorMsg }}</div>

      <NEmpty v-else-if="!loading && !rows.length" description="暂无观察档案">
        <template #extra>
          <span class="dim">
            案件尚未产生观察，或当前筛选条件下无结果。镜头在建案/重扫时自动生成观察。
          </span>
        </template>
      </NEmpty>

      <ul v-else class="ov-list" data-testid="obs-list">
        <li
          v-for="o in rows"
          :key="o.observation_id"
          class="ov-card"
          :class="{ 'ov-card--degraded': o.degraded }"
          @click="openDetail(o)"
        >
          <div class="oc-head">
            <NTag size="tiny" round :type="dispositionType(o.disposition)">
              {{ o.disposition }}
            </NTag>
            <NTag
              v-if="o.directed"
              size="tiny"
              round
              type="info"
              :title="`由正兵从线索画布定向发起${o.origin?.clue_id ? `（${o.origin.clue_id}）` : ''}：案件级保留、不随重扫失效；无此标记的同镜头同主体条目是建案/重扫自动批量产出（随版本重算）`"
            >
              定向
            </NTag>
            <span class="oc-lens">{{ o.lens_name }}</span>
            <span v-if="o.subject" class="oc-subject">{{ o.subject }}</span>
            <span v-if="o.project" class="oc-project">{{ o.project }}</span>
            <NTag v-if="o.degraded" size="tiny" round type="error" :title="o.degraded_reason">
              数据未齐
            </NTag>
          </div>

          <p class="oc-title">{{ o.title }}</p>

          <!-- 判据：一句话说明"这说明什么"，避免用户陷在庞杂数据里 -->
          <p v-if="o.basis" class="oc-basis">{{ o.basis }}</p>
          <p v-else class="oc-basis oc-basis--empty">（本条观察未生成判据）</p>

          <p class="oc-meta">
            <span>{{ o.facts_count }} 条事实明细</span>
            <span>· {{ o.evidence_count }} 条证据引用</span>
            <span v-if="o.promoted_hypothesis" class="oc-hyp">
              · 已提升至假设 {{ o.promoted_hypothesis }}
            </span>
          </p>
        </li>
      </ul>

      <!-- 跨版本遗留：认领过的观察不在当前版本了——不隐藏，显式告知去哪了 -->
      <template v-if="staleRows.length">
        <h3 class="ov-stale-title">已不在当前版本（{{ staleRows.length }}）</h3>
        <ul class="ov-list">
          <li v-for="s in staleRows" :key="s.observation_id" class="ov-card ov-card--stale">
            <div class="oc-head">
              <NTag size="tiny" round>{{ s.disposition }}</NTag>
              <span class="oc-lens">{{ s.promoted_hypothesis || '—' }}</span>
            </div>
            <p class="oc-title dim">{{ s.title }}</p>
            <p class="oc-meta">认领人 {{ s.operator || '—' }} · 重扫后靶心或数据已变</p>
          </li>
        </ul>
      </template>

      <div v-if="total > PAGE_SIZE_DEFAULT" class="ov-pager">
        <NButton size="tiny" :disabled="page <= 1" @click="page = Math.max(1, page - 1)">
          上一页
        </NButton>
        <span class="dim">第 {{ page }} 页 / 共 {{ total }} 条</span>
        <NButton size="tiny" @click="page = page + 1">下一页</NButton>
      </div>
    </NSpin>
  </div>
</template>

<style scoped>
.obs-view {
  padding: 16px;
  max-width: 1100px;
}
.ov-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 14px;
}
.ov-title {
  margin: 0 0 4px;
  font-size: 17px;
}
.ov-sub {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  line-height: 1.5;
  max-width: 640px;
}
.ov-stats {
  display: flex;
  gap: 12px;
  font-size: 12px;
  white-space: nowrap;
}
.st b {
  font-size: 14px;
}
.st--warn {
  color: var(--sun-warning, #d89614);
}
.st--ok {
  color: var(--sun-success, #18a058);
}
.st--dim {
  color: var(--sun-text-tertiary);
}
.ov-filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.f-sel {
  width: 150px;
}
.f-in {
  width: 200px;
}
.ov-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ov-card {
  border: 1px solid var(--sun-border, #e5e5e5);
  border-radius: 6px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.ov-card:hover {
  border-color: var(--sun-primary, #2080f0);
}
.ov-card--degraded {
  border-style: dashed;
}
.ov-card--stale {
  cursor: default;
  opacity: 0.7;
  border-style: dotted;
}
.oc-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.oc-lens {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.oc-subject {
  font-size: 12px;
  font-weight: 600;
}
.oc-project {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.oc-title {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 600;
}
.oc-basis {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--sun-text-secondary, #555);
}
.oc-basis--empty {
  color: var(--sun-text-tertiary);
  font-style: italic;
}
.oc-meta {
  margin: 0;
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.oc-hyp {
  color: var(--sun-success, #18a058);
}
.ov-stale-title {
  margin: 18px 0 8px;
  font-size: 13px;
  color: var(--sun-text-tertiary);
}
.ov-pager {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  font-size: 12px;
}
.ov-err {
  color: var(--sun-error, #d03050);
  font-size: 12px;
  padding: 8px 0;
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
