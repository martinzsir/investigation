<script setup lang="ts">
// 三维交汇工作台（人-时-地锚点：关系/时间/空间三类证据的归集视图）。
//
// 为什么单列一页，而不是塞进观察档案
// ------------------------------------
// 观察回答"单个镜头看到了什么"，交汇回答"多个镜头是否在同一锚点上同时看到"。
// 混在一份清单里，"单点发现"和"多维印证"会重新变模糊——而后者才是
// 值得优先核查的部分。
//
// 三条读面红线（本页负责不破坏）
// ------------------------------
// 1. **分数不是结论。** score 只用于排序，且必须与三维各自的
//    count/precision/weight **并列渲染**。只给一个总分就等于把精度加权
//    重新掩盖——正兵看不出"时间维是日期级、只有 0.6 权重"。
//    故本页用**命中矩阵**（三格并列）而非一个分数条。
// 2. **重名不猜。** person_ambiguous 的锚点单列成独立区块，不混进普通排序。
//    它们不是"分数低"，而是**主体指代不明**；混进排序会被当成弱信号跳过，
//    恰恰错了——未消歧的锚点一旦误判，三维证据会错配到另一个人身上。
// 3. **裁决未生效要看得见。** 拿不到语义层连接时裁决不执行、主体按名归一，
//    这是危险的静默失效——故顶部显式告警，不静默降级。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NSpin,
  NSelect,
  NInput,
  NButton,
  NEmpty,
  NAlert,
  NDatePicker,
  NRadioGroup,
  NRadioButton,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import {
  convergenceApi,
  PRECISION_LABEL,
  type ConvergenceDetailResult,
  type ConvergenceItem,
  type ConvergenceListResult,
} from '../api/endpoints/convergence'
import { presentError } from '../api/errors'
import { PAGE_SIZE_DEFAULT, parsePageQuery } from '../domain/pagination'
import EmptyState from '../components/common/EmptyState.vue'
import ConvergenceCard from '../components/research/ConvergenceCard.vue'
import ConvergenceTimeline from '../components/research/ConvergenceTimeline.vue'
import ConvergenceMap from '../components/research/ConvergenceMap.vue'
import { buildConvergenceTimeline } from '../domain/convergence-timeline'

const cs = useCaseStore()
const route = useRoute()
const router = useRouter()

const loading = ref(false)
const errorMsg = ref('')
const data = ref<ConvergenceListResult | null>(null)
const page = ref(parsePageQuery(route.query.page))

// 筛选（均可分享到 URL，与线索/观察列表同纪律）
const dimFilter = ref<string>(String(route.query.dim ?? '') || '')
const personFilter = ref<string>(String(route.query.person ?? ''))
const dateFrom = ref<string | null>(String(route.query.date_from ?? '') || null)
const dateTo = ref<string | null>(String(route.query.date_to ?? '') || null)
const minDims = ref<number>(Number(route.query.min_dims ?? 2) || 2)
const onlyAmbiguous = ref<boolean>(route.query.ambiguous === 'true')

/** 展开的锚点 key → 详情（支撑观察） */
const expanded = ref<string>('')
const detailCache = ref<Record<string, ConvergenceDetailResult>>({})
const detailLoading = ref<string>('')

const rows = computed<ConvergenceItem[]>(() => data.value?.convergences ?? [])
const total = computed(() => data.value?.total ?? 0)
const stats = computed(() => data.value?.stats ?? null)

/** 裁决未生效 → 顶部告警（红线 3） */
const homonymAlert = computed(() => {
  const h = data.value?.diagnostics?.homonym_resolution
  if (!h || h.status === 'ok') return null
  return {
    status: h.status,
    text:
      h.effect ||
      h.detail ||
      '同名异人裁决未执行，主体按名归一；重名将被静默合并',
  }
})

/** 红线 2：重名待裁决单列，不混进普通排序 */
const ambiguousRows = computed(() => rows.value.filter((r) => r.person_ambiguous))
const normalRows = computed(() => rows.value.filter((r) => !r.person_ambiguous))

/**
 * 视图模式：清单（三维矩阵逐条比对）/ 时间轴（看哪些日期段三维密集）/
 * 地图（看聚集在哪个片区）。三者是**同一批数据的三种投影**，切换而非并列——
 * 并排会把每个视图压到看不清，且并排不等于整合。
 */
const viewMode = ref<'list' | 'timeline' | 'map'>('list')
const timelineModel = computed(() => buildConvergenceTimeline(rows.value))

/** 时间轴/地图上点选锚点 → 回清单并展开该锚点的支撑观察（顺 obs_id 可回档案） */
function onSelectFromView(key: string): void {
  viewMode.value = 'list'
  const row = rows.value.find((r) => r.key === key)
  if (row) void toggle(row)
}

const dimOptions = [
  { label: '全部维度', value: '' },
  { label: '空间', value: 'space' },
  { label: '时间', value: 'time' },
  { label: '关系', value: 'relation' },
]

/** min_dims 默认 2：单维不是交汇——只有一个维度命中的锚点，
 *  其观察在原档案里已经能看到，进交汇清单只会稀释真正需要对比看的那些。 */
const minDimsOptions = [
  { label: '≥2 维命中（默认）', value: 2 },
  { label: '≥3 维命中', value: 3 },
  { label: '≥1 维（含单点发现）', value: 1 },
]

/** 权重模型说明：后端声明、前端展示，改权重自动跟随（不硬编码） */
const weightText = computed(() => {
  const w = data.value?.weight_model
  if (!w) return ''
  const pw = Object.entries(w.precision_weight ?? {})
    .map(([k, v]) => `${PRECISION_LABEL[k]?.label ?? k}×${v}`)
    .join(' · ')
  return `${w.formula}；精度权重：${pw}`
})

async function load(): Promise<void> {
  if (!cs.currentCaseId) return
  loading.value = true
  errorMsg.value = ''
  try {
    data.value = await convergenceApi.list(cs.currentCaseId, {
      dim: dimFilter.value || undefined,
      person: personFilter.value || undefined,
      date_from: dateFrom.value || undefined,
      date_to: dateTo.value || undefined,
      ambiguous: onlyAmbiguous.value ? true : undefined,
      min_dims: minDims.value,
      page: page.value,
      page_size: PAGE_SIZE_DEFAULT,
    })
  } catch (e) {
    errorMsg.value = presentError(e).title
    data.value = null
  } finally {
    loading.value = false
  }
}

function syncQuery(): void {
  void router.replace({
    query: {
      ...route.query,
      dim: dimFilter.value || '',
      person: personFilter.value || '',
      date_from: dateFrom.value || '',
      date_to: dateTo.value || '',
      min_dims: String(minDims.value),
      ambiguous: onlyAmbiguous.value ? 'true' : '',
      page: String(page.value),
    },
  })
}

/** 展开/收起：调详情端点取支撑观察（顺 obs_id 可回观察档案） */
async function toggle(row: ConvergenceItem): Promise<void> {
  if (expanded.value === row.key) {
    expanded.value = ''
    return
  }
  expanded.value = row.key
  if (detailCache.value[row.key] || !cs.currentCaseId) return
  detailLoading.value = row.key
  try {
    detailCache.value[row.key] = await convergenceApi.detail(
      cs.currentCaseId,
      row.key,
    )
  } catch (e) {
    // 支撑观察加载失败不该让整条锚点不可看：保留锚点，只是支撑为空
    detailCache.value[row.key] = {
      available: false,
      convergence: row,
      support: { space: [], time: [], relation: [] },
      observations_index: 0,
      weight_model: null,
      note: `支撑观察加载失败：${presentError(e).title}`,
      diagnostics: {},
    }
  } finally {
    detailLoading.value = ''
  }
}

function openObservation(obsId: string): void {
  if (obsId) void router.push(`/c/observations/${encodeURIComponent(obsId)}`)
}

function applyAmbiguous(): void {
  onlyAmbiguous.value = !onlyAmbiguous.value
  page.value = 1
  syncQuery()
  void load()
}

onMounted(load)
watch(
  () => cs.currentCaseId,
  () => {
    page.value = 1
    expanded.value = ''
    detailCache.value = {}
    void load()
  },
)
watch([dimFilter, minDims], () => {
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
    desc="三维交汇按案件隔离——顶部案件选择器选具体案件后才能查看"
  />
  <div v-else class="cv-view">
    <header class="cv-head">
      <div>
        <h2 class="cv-title">三维交汇</h2>
        <p class="cv-sub">
          同一个<strong>人—时—地</strong>锚点上，关系、时间、空间各有几条证据命中。
          交汇强度表示<strong>核查优先级</strong>，不代表风险高低；三维均命中亦不构成定性结论。
        </p>
      </div>
      <div v-if="stats" class="cv-stats">
        <span class="st"><b>{{ total }}</b> 个锚点</span>
        <span class="st st--ok">
          <b>{{ stats.by_hit_count?.['3'] ?? 0 }}</b> 三维命中
        </span>
        <span class="st st--warn">
          <b>{{ stats.ambiguous ?? 0 }}</b> 重名待裁决
        </span>
      </div>
    </header>

    <!-- 红线 3：裁决未生效不可静默 -->
    <NAlert
      v-if="homonymAlert"
      type="error"
      :title="`同名异人裁决未生效（${homonymAlert.status}）`"
      class="cv-alert"
      data-testid="cv-homonym-alert"
    >
      {{ homonymAlert.text }}
    </NAlert>

    <div v-if="data?.note" class="cv-note" data-testid="cv-note">{{ data.note }}</div>

    <!-- 三种投影：切换而非并排 -->
    <div class="cv-views" data-testid="cv-view-switch">
      <NRadioGroup v-model:value="viewMode" size="small">
        <NRadioButton value="list" data-testid="cv-view-list">清单</NRadioButton>
        <NRadioButton value="timeline" data-testid="cv-view-timeline">时间轴</NRadioButton>
        <NRadioButton value="map" data-testid="cv-view-map">地图</NRadioButton>
      </NRadioGroup>
      <span class="dim">
        {{ viewMode === 'list' ? '逐条比对三维矩阵'
          : viewMode === 'timeline' ? '按日期看三维密集段（金条=同人同日三维齐全）'
          : '看聚集片区（空心点=区划质心，非精确位置）' }}
      </span>
    </div>

    <div v-if="viewMode === 'timeline'" class="cv-view-block">
      <ConvergenceTimeline :model="timelineModel" @select="onSelectFromView" />
    </div>
    <div v-else-if="viewMode === 'map'" class="cv-view-block">
      <ConvergenceMap
        :items="rows"
        :coords="data?.diagnostics?.coords"
        @select="onSelectFromView"
      />
    </div>

    <div class="cv-filters">
      <NSelect
        v-model:value="minDims"
        :options="minDimsOptions"
        size="small"
        class="f-sel f-sel--wide"
        data-testid="cv-filter-min-dims"
      />
      <NSelect
        v-model:value="dimFilter"
        :options="dimOptions"
        size="small"
        class="f-sel"
        data-testid="cv-filter-dim"
      />
      <NInput
        v-model:value="personFilter"
        size="small"
        placeholder="按主体 / 同现主体筛选"
        class="f-in"
        data-testid="cv-filter-person"
        @keyup.enter="load"
      />
      <NDatePicker
        v-model:value="dateFrom"
        size="small"
        type="date"
        clearable
        placeholder="起始日期"
        class="f-date"
        value-format="yyyy-MM-dd"
      />
      <NDatePicker
        v-model:value="dateTo"
        size="small"
        type="date"
        clearable
        placeholder="截止日期"
        class="f-date"
        value-format="yyyy-MM-dd"
      />
      <NButton
        size="small"
        :secondary="!onlyAmbiguous"
        :type="onlyAmbiguous ? 'warning' : 'default'"
        data-testid="cv-filter-ambiguous"
        @click="applyAmbiguous"
      >
        仅重名待裁决
      </NButton>
      <NButton size="small" secondary data-testid="cv-search" @click="load">筛选</NButton>
    </div>

    <p v-if="weightText" class="cv-weight" data-testid="cv-weight">
      权重模型：{{ weightText }}
    </p>

    <NSpin :show="loading">
      <div v-if="errorMsg" class="cv-err">{{ errorMsg }}</div>

      <div
        v-else-if="data && !data.available"
        class="cv-err"
        data-testid="cv-unavailable"
      >
        {{ data.note || '交汇不可用（案件尚未 BUILD，无观察档案可归集）' }}
      </div>

      <NEmpty v-else-if="!loading && !rows.length" description="暂无交汇锚点">
        <template #extra>
          <span class="dim">
            当前筛选下无多维命中。可降低「命中维数」下限，或先跑镜头产出观察档案。
          </span>
        </template>
      </NEmpty>

      <template v-else-if="viewMode === 'list'">
        <!-- 红线 2：重名待裁决单列，不混进普通排序 -->
        <template v-if="ambiguousRows.length">
          <h3 class="cv-group-title" data-testid="cv-group-ambiguous">
            重名待裁决（{{ ambiguousRows.length }}）
            <span class="cv-group-hint">
              主体指代不明，消歧前不可当作单一自然人——三个维度的证据可能分属不同的人
            </span>
          </h3>
          <ul class="cv-list" data-testid="cv-list-ambiguous">
            <li v-for="r in ambiguousRows" :key="r.key" class="cv-card cv-card--ambiguous">
              <ConvergenceCard
                :row="r"
                :expanded="expanded === r.key"
                :detail="detailCache[r.key] || null"
                :detail-loading="detailLoading === r.key"
                @toggle="toggle(r)"
                @open-observation="openObservation"
              />
            </li>
          </ul>
        </template>

        <template v-if="normalRows.length">
          <h3 v-if="ambiguousRows.length" class="cv-group-title">
            主体已确定（{{ normalRows.length }}）
          </h3>
          <ul class="cv-list" data-testid="cv-list">
            <li v-for="r in normalRows" :key="r.key" class="cv-card">
              <ConvergenceCard
                :row="r"
                :expanded="expanded === r.key"
                :detail="detailCache[r.key] || null"
                :detail-loading="detailLoading === r.key"
                @toggle="toggle(r)"
                @open-observation="openObservation"
              />
            </li>
          </ul>
        </template>

        <div v-if="total > PAGE_SIZE_DEFAULT" class="cv-pager">
          <NButton size="tiny" :disabled="page <= 1" @click="page = Math.max(1, page - 1)">
            上一页
          </NButton>
          <span class="dim">第 {{ page }} 页 / 共 {{ total }} 条</span>
          <NButton size="tiny" @click="page = page + 1">下一页</NButton>
        </div>
      </template>
    </NSpin>
  </div>
</template>

<style scoped>
.cv-view {
  padding: 16px;
  max-width: 1180px;
}
.cv-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}
.cv-title {
  margin: 0 0 4px;
  font-size: 17px;
}
.cv-sub {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  line-height: 1.5;
  max-width: 700px;
}
.cv-stats {
  display: flex;
  gap: 12px;
  font-size: 12px;
  white-space: nowrap;
}
.st b {
  font-size: 14px;
}
.st--ok {
  color: var(--sun-success, #18a058);
}
.st--warn {
  color: var(--sun-warning, #d89614);
}
.cv-alert {
  margin-bottom: 10px;
  font-size: 12px;
}
.cv-note,
.cv-weight {
  margin: 0 0 10px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  line-height: 1.5;
}
.cv-filters {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.f-sel {
  width: 130px;
}
.f-sel--wide {
  width: 175px;
}
.f-in {
  width: 190px;
}
.f-date {
  width: 150px;
}
.cv-group-title {
  margin: 14px 0 8px;
  font-size: 13px;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.cv-group-hint {
  font-size: 11px;
  font-weight: 400;
  color: var(--sun-text-tertiary);
}
.cv-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cv-card {
  border: 1px solid var(--sun-border, #e5e5e5);
  border-radius: 6px;
  padding: 10px 12px;
}
.cv-card--ambiguous {
  border-style: dashed;
  border-color: var(--sun-warning, #d89614);
}
.cv-pager {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  font-size: 12px;
}
.cv-err {
  color: var(--sun-error, #d03050);
  font-size: 12px;
  padding: 8px 0;
}
.dim {
  color: var(--sun-text-tertiary);
}
.cv-views { display: flex; align-items: center; gap: 12px; margin: 10px 0; }
.cv-view-block { margin-bottom: 14px; }
</style>
