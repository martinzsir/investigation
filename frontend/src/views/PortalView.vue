<script setup lang="ts">
// 案件门户（23b，平台页 /cases）：案件卡片网格 + 三态（空库/筛选无结果/有数据）。
// B1：status/q 筛选全部前端客户端做（GET /cases 无查询参数）；
// A3：卡片汇总 Promise.all 并发 summary；归档 clearance≥2 + reason 留痕。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NSpin, NButton, NInput, NModal, NTag, useMessage } from 'naive-ui'
import { casesApi, type CaseDto, type CaseSummaryDto } from '../api/endpoints/cases'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { presentError, isApiError } from '../api/errors'
import {
  CASE_STATUS, canArchive, caseIdError, caseNameError, filterCases,
  healthTone, portalViewState,
} from '../domain/portal'
import EmptyState from '../components/common/EmptyState.vue'
import OntologyVersionTag from '../components/common/OntologyVersionTag.vue'

const router = useRouter()
const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const summaries = ref<Record<string, CaseSummaryDto>>({})

// 筛选
const statusFilter = ref<'all' | string>('all')
const q = ref('')

// 建案对话框
const showCreate = ref(false)
const creating = ref(false)
const newId = ref('')
const newName = ref('')

// 归档确认
const archiving = ref<CaseDto | null>(null)
const archiveReason = ref('')
const archiveBusy = ref(false)

// 结案确认
const closing = ref<CaseDto | null>(null)
const closeReason = ref('')
const closeBusy = ref(false)

const STATUS_TABS = computed(() => [
  { value: 'all', label: '全部' },
  ...Object.values(CASE_STATUS).map((s) => ({ value: s, label: s })),
])

const filtered = computed(() => filterCases(cs.cases, { status: statusFilter.value, q: q.value }))
const viewState = computed(() => portalViewState(cs.cases, filtered.value, loading.value))

function statusCount(status: string): number {
  return cs.cases.filter((c) => c.status === status).length
}

const idErr = computed(() => caseIdError(newId.value))
const nameErr = computed(() => caseNameError(newName.value))
const createDisabled = computed(() => creating.value || Boolean(idErr.value) || Boolean(nameErr.value))

async function load(): Promise<void> {
  loading.value = true
  try {
    await cs.loadCases()
    // A3：并发拉取各案汇总（失败的单卡降级无汇总，不拖垮整页）
    const results = await Promise.allSettled(cs.cases.map((c) => casesApi.summary(c.id)))
    const map: Record<string, CaseSummaryDto> = {}
    results.forEach((r, i) => {
      if (r.status === 'fulfilled') map[cs.cases[i].id] = r.value
    })
    summaries.value = map
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openCreate(): void {
  newId.value = ''
  newName.value = ''
  showCreate.value = true
}

async function submitCreate(): Promise<void> {
  if (createDisabled.value) return
  creating.value = true
  try {
    const c = await casesApi.create({ case_id: newId.value.trim(), name: newName.value.trim() })
    message.success(`案件「${c.name}」已创建`)
    showCreate.value = false
    await cs.loadCases()
    cs.selectCase(c.id)
    await router.push('/c/overview')
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    creating.value = false
  }
}

function enterCase(c: CaseDto): void {
  cs.selectCase(c.id)
  void router.push('/c/overview')
}

function closeable(c: CaseDto): boolean {
  return canArchive(auth.clearance) && c.status === CASE_STATUS.active
}

function openClose(c: CaseDto): void {
  closing.value = c
  closeReason.value = ''
}

async function confirmClose(): Promise<void> {
  if (!closing.value) return
  closeBusy.value = true
  try {
    const updated = await casesApi.close(closing.value.id, closeReason.value.trim())
    Object.assign(closing.value, updated)
    message.success('案件已结案')
    closing.value = null
    void load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    closeBusy.value = false
  }
}

function archiveable(c: CaseDto): boolean {
  return canArchive(auth.clearance) && (c.status === CASE_STATUS.active || c.status === CASE_STATUS.closed)
}

function openArchive(c: CaseDto): void {
  archiving.value = c
  archiveReason.value = ''
}

async function confirmArchive(): Promise<void> {
  if (!archiving.value) return
  if (!archiveReason.value.trim()) {
    message.warning('归档须填写原因（审计留痕）')
    return
  }
  archiveBusy.value = true
  try {
    const task = await casesApi.archive(archiving.value.id, archiveReason.value.trim())
    message.success(`归档任务已入队（${task.id}），完成后案件状态迁移`)
    archiving.value = null
    void load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    archiveBusy.value = false
  }
}

function statusTagType(s: string): 'default' | 'info' | 'success' | 'warning' | 'error' {
  if (s === CASE_STATUS.archived) return 'default'
  if (s === CASE_STATUS.closed) return 'warning'
  if (s === CASE_STATUS.active) return 'info'
  return 'default'
}

function summaryOf(c: CaseDto): CaseSummaryDto | undefined {
  return summaries.value[c.id]
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="head-row">
        <h2>案件门户</h2>
        <NButton type="primary" size="small" @click="openCreate">新建案件</NButton>
      </div>
      <p class="dim hint">全部案件的统一入口：卡片汇总待办与审计链健康点；点击卡片进入案件工作台。</p>
    </div>

    <template v-if="viewState !== 'empty'">
      <div class="filter-bar">
        <button
          v-for="t in STATUS_TABS"
          :key="t.value"
          class="chip"
          :class="{ active: statusFilter === t.value }"
          @click="statusFilter = t.value"
        >
          {{ t.label }}
          <span class="n">{{ t.value === 'all' ? cs.cases.length : statusCount(t.value) }}</span>
        </button>
        <NInput
          v-model:value="q"
          size="small"
          class="search"
          placeholder="搜索案件编号 / 名称"
          clearable
        />
      </div>
    </template>

    <NSpin :show="loading">
      <EmptyState
        v-if="viewState === 'empty'"
        type="empty"
        title="还没有案件"
        desc="新建第一个案件，开始确定性侦查推演（语义层版本将在首次 BUILD 后锁定）"
      >
        <template #action>
          <NButton type="primary" size="small" @click="openCreate">新建案件</NButton>
        </template>
      </EmptyState>

      <EmptyState
        v-else-if="viewState === 'no-result'"
        type="empty"
        title="无匹配案件"
        desc="调整状态筛选或搜索关键词"
      />

      <div v-else class="card-grid">
        <div v-for="c in filtered" :key="c.id" class="case-card" @click="enterCase(c)">
          <div class="card-top">
            <span class="case-name">{{ c.name }}</span>
            <NTag size="tiny" :bordered="false" :type="statusTagType(c.status)">{{ c.status }}</NTag>
          </div>
          <div class="card-sub">
            <span class="mono case-id">{{ c.id }}</span>
            <OntologyVersionTag :pack-id="c.pack_id" :snapshot-at="c.pack_snapshot_at" />
          </div>

          <div class="card-metrics">
            <div class="metric">
              <span class="metric-n">{{ summaryOf(c)?.todos.clues_pending ?? '—' }}</span>
              <span class="metric-l dim">线索待办</span>
            </div>
            <div class="metric">
              <span class="metric-n">{{ summaryOf(c)?.todos.review_pending ?? '—' }}</span>
              <span class="metric-l dim">待复核</span>
            </div>
            <div class="metric">
              <span class="metric-n">{{ summaryOf(c)?.todos.anomalies_pending ?? '—' }}</span>
              <span class="metric-l dim">异常待处</span>
            </div>
            <div class="metric metric-health">
              <span
                class="health-dot"
                :class="`health-dot--${healthTone(summaryOf(c))}`"
                :title="summaryOf(c)?.health.chain_ok === false ? '审计链校验未通过' : '健康'"
              />
              <span class="metric-l dim">{{ healthTone(summaryOf(c)) === 'warn' ? '链告警' : '健康' }}</span>
            </div>
          </div>

          <div class="card-actions" @click.stop>
            <NButton size="tiny" type="primary" quaternary @click="enterCase(c)">进入案件</NButton>
            <NButton
              v-if="closeable(c)"
              size="tiny"
              type="info"
              quaternary
              @click="openClose(c)"
            >
              结案
            </NButton>
            <NButton
              v-if="archiveable(c)"
              size="tiny"
              type="warning"
              quaternary
              @click="openArchive(c)"
            >
              归档
            </NButton>
            <span v-else-if="!canArchive(auth.clearance)" class="dim archive-hint">归档需偏将及以上</span>
          </div>
        </div>
      </div>
    </NSpin>

    <!-- 建案对话框 -->
    <NModal v-model:show="showCreate" preset="card" title="新建案件" class="create-modal" :mask-closable="false">
      <div class="form">
        <label class="form-row">
          <span class="form-label">案件编号 <b>*</b></span>
          <NInput v-model:value="newId" placeholder="字母/数字/下划线/中划线，1–64" />
          <span v-if="idErr" class="form-err">{{ idErr }}</span>
        </label>
        <label class="form-row">
          <span class="form-label">案件名称 <b>*</b></span>
          <NInput v-model:value="newName" placeholder="1–128 字" />
          <span v-if="nameErr" class="form-err">{{ nameErr }}</span>
        </label>
        <p class="dim form-hint">案件包默认 default；建案后首次 BUILD 语义层即锁定版本快照。</p>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton :disabled="creating" @click="showCreate = false">取消</NButton>
          <NButton type="primary" :loading="creating" :disabled="createDisabled" @click="submitCreate">
            创建并进入
          </NButton>
        </div>
      </template>
    </NModal>

    <!-- 结案确认 -->
    <NModal
      :show="!!closing"
      preset="card"
      title="结案"
      :mask-closable="false"
      @update:show="(v: boolean) => { if (!v) closing = null }"
    >
      <div class="form">
        <p class="form-line">
          将案件 <b>{{ closing?.name }}</b>（<span class="mono">{{ closing?.id }}</span>）标记为已结案。
          结案后可继续归档，但不可恢复为侦查中。
        </p>
        <label class="form-row">
          <span class="form-label">结案说明</span>
          <NInput v-model:value="closeReason" type="textarea" :rows="3" placeholder="结案依据与结论摘要（审计留痕）" />
        </label>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton :disabled="closeBusy" @click="closing = null">取消</NButton>
          <NButton type="info" :loading="closeBusy" @click="confirmClose">确认结案</NButton>
        </div>
      </template>
    </NModal>

    <!-- 归档确认 -->
    <NModal
      :show="!!archiving"
      preset="card"
      title="归档案件"
      :mask-closable="false"
      @update:show="(v: boolean) => { if (!v) archiving = null }"
    >
      <div class="form">
        <p class="form-line">
          将案件 <b>{{ archiving?.name }}</b>（<span class="mono">{{ archiving?.id }}</span>）归档。
          归档任务入队执行，状态迁移经任务中心可见。
        </p>
        <label class="form-row">
          <span class="form-label">归档原因 <b>*</b></span>
          <NInput v-model:value="archiveReason" type="textarea" :rows="3" placeholder="归档依据与交接说明（审计留痕）" />
        </label>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton :disabled="archiveBusy" @click="archiving = null">取消</NButton>
          <NButton type="warning" :loading="archiveBusy" @click="confirmArchive">确认归档并留痕</NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-head h2 {
  margin: 0;
  font-size: 18px;
}
.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.hint {
  font-size: 12px;
  margin: 4px 0 0;
}
.filter-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.chip {
  border: 1px solid var(--sun-border);
  background: var(--sun-bg-card);
  color: var(--sun-text-secondary);
  border-radius: 16px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
}
.chip.active {
  border-color: var(--sun-info-text, var(--sun-ok-text));
  color: var(--sun-text-primary);
  font-weight: 600;
}
.chip .n {
  color: var(--sun-text-tertiary);
  margin-left: 4px;
  font-family: var(--sun-font-mono);
}
.search {
  width: 220px;
  margin-left: auto;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.case-card {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 8px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.case-card:hover {
  border-color: var(--sun-border-active, var(--sun-info-border));
}
.card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.case-name {
  font-size: 15px;
  font-weight: 600;
}
.card-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.case-id {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.card-metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  border-top: 1px dashed var(--sun-border);
  border-bottom: 1px dashed var(--sun-border);
  padding: 8px 0;
}
.metric {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.metric-n {
  font-size: 18px;
  font-weight: 700;
  font-family: var(--sun-font-mono);
}
.metric-l {
  font-size: 11px;
}
.metric-health {
  flex-direction: row;
  gap: 4px;
  align-items: center;
}
.health-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.health-dot--ok {
  background: var(--sun-ok-text);
}
.health-dot--warn {
  background: var(--sun-warn-text);
}
.health-dot--none {
  background: var(--sun-border);
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.archive-hint {
  font-size: 11px;
  margin-left: auto;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 420px;
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.form-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.form-label b {
  color: var(--sun-error-text);
}
.form-err {
  font-size: 12px;
  color: var(--sun-error-text);
}
.form-hint {
  font-size: 12px;
  margin: 0;
}
.form-line {
  font-size: 13px;
  margin: 0;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
