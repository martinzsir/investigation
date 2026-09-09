<script setup lang="ts">
// 系统设置（FE-P-025，平台页 /settings）。
// 纪律（B2 fail-closed）：非 admin 不渲染管理面数值（后端 GET 同样 403，不靠 403 探测）；
// 唯 health 登录可读。写操作 reason 必填（ConfigConfirmDialog 危险项）+ 键白名单/区间
// 后端校验；红线键（llm_enabled/audit_immutable）永不可写，静态锁定行展示。
import { computed, onMounted, ref } from 'vue'
import {
  NSpin, NButton, NInputNumber, NInput, NSelect, NTag, useMessage,
} from 'naive-ui'
import { useAuthStore } from '../stores/auth'
import {
  settingsApi, type FeatureSettings, type HealthSettings,
  type QueueSettings, type ResourceSettings, type SnapshotItem,
  type ThresholdSettings,
} from '../api/endpoints/settings'
import { presentError, isApiError } from '../api/errors'
import {
  FEATURE_KEYS, LOCKED_REDLINES, QUEUE_KEYS, RESOURCE_KEYS, THRESHOLDS_NOTE,
  THRESHOLD_KEYS, canSubmit, canViewAdminSettings, clampKey, diffValues,
  type KeyMeta,
} from '../domain/settingsModel'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'
import EmptyState from '../components/common/EmptyState.vue'

const auth = useAuthStore()
const message = useMessage()

const isAdmin = computed(() => canViewAdminSettings(auth.isAdmin))

const healthLoading = ref(false)
const health = ref<HealthSettings | null>(null)
const adminLoading = ref(false)

const queue = ref<QueueSettings | null>(null)
const resources = ref<ResourceSettings | null>(null)
const thresholds = ref<ThresholdSettings | null>(null)
const features = ref<FeatureSettings | null>(null)
const snapshots = ref<SnapshotItem[]>([])

// 编辑缓冲（NInputNumber 清空时值为 null，提交前经钳制/白名单过滤）
const queueEdit = ref<Record<string, number | null>>({})
const resourcesEdit = ref<Record<string, number | null>>({})
const thresholdsEdit = ref<Record<string, number | null>>({})
const featuresEdit = ref<Record<string, string>>({})

// 确认对话框
const showConfirm = ref(false)
const reason = ref('')
const busy = ref(false)
const section = ref<'queue' | 'resources' | 'thresholds' | 'features'>('queue')

const SECTION_TITLE = {
  queue: '队列设置',
  resources: '资源限额',
  thresholds: '配置中心 · 交叉阈值',
  features: '界面特性',
} as const

async function loadHealth(): Promise<void> {
  healthLoading.value = true
  try {
    health.value = await settingsApi.health()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    healthLoading.value = false
  }
}

async function loadAdmin(): Promise<void> {
  if (!isAdmin.value) return
  adminLoading.value = true
  try {
    const [q, r, t, f, s] = await Promise.all([
      settingsApi.getQueue(),
      settingsApi.getResources(),
      settingsApi.getThresholds(),
      settingsApi.getFeatures(),
      settingsApi.listSnapshots(),
    ])
    queue.value = q
    resources.value = r
    thresholds.value = t.thresholds
    features.value = f
    snapshots.value = s.items
    queueEdit.value = { max_workers: q.max_workers, poll_interval_ms: q.poll_interval_ms }
    resourcesEdit.value = {
      max_rows_default: r.max_rows_default,
      query_timeout_ms: r.query_timeout_ms,
    }
    thresholdsEdit.value = { ...t.thresholds }
    featuresEdit.value = { ui_density: f.ui_density }
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    adminLoading.value = false
  }
}

onMounted(() => {
  void loadHealth()
  void loadAdmin()
})

function changedKeys(sec: typeof section.value): string[] {
  if (sec === 'queue') return diffValues(queue.value ?? {}, queueEdit.value)
  if (sec === 'resources') return diffValues(resources.value ?? {}, resourcesEdit.value)
  if (sec === 'thresholds') return diffValues(thresholds.value ?? {}, thresholdsEdit.value)
  return diffValues(features.value ?? {}, featuresEdit.value)
}

const pendingChanged = computed(() => changedKeys(section.value))
const submitOk = computed(() => canSubmit(pendingChanged.value, reason.value))

function openConfirm(sec: typeof section.value): void {
  if (!changedKeys(sec).length) {
    message.info('没有检测到变更')
    return
  }
  section.value = sec
  reason.value = ''
  showConfirm.value = true
}

function clampEdit(meta: KeyMeta, bucket: 'queue' | 'resources' | 'thresholds', key: string): void {
  const target = bucket === 'queue' ? queueEdit : bucket === 'resources' ? resourcesEdit : thresholdsEdit
  const v = target.value[key]
  if (typeof v === 'number') target.value[key] = clampKey(meta, v)
}

async function submit(): Promise<void> {
  if (!submitOk.value) return
  const keys = changedKeys(section.value)
  const values: Record<string, unknown> = {}
  const source =
    section.value === 'queue'
      ? queueEdit.value
      : section.value === 'resources'
        ? resourcesEdit.value
        : section.value === 'thresholds'
          ? thresholdsEdit.value
          : featuresEdit.value
  for (const k of keys) {
    if (source[k] === null || source[k] === undefined) continue
    values[k] = source[k]
  }

  busy.value = true
  try {
    if (section.value === 'queue') await settingsApi.putQueue({ reason: reason.value.trim(), values })
    else if (section.value === 'resources') await settingsApi.putResources({ reason: reason.value.trim(), values })
    else if (section.value === 'thresholds') await settingsApi.putThresholds({ reason: reason.value.trim(), values })
    else await settingsApi.putFeatures({ reason: reason.value.trim(), values })
    message.success('设置已更新并留痕')
    showConfirm.value = false
    await loadAdmin()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    busy.value = false
  }
}

const confirmDetail = computed(() => {
  const keys = pendingChanged.value
  const cur = section.value === 'queue'
    ? queue.value
    : section.value === 'resources'
      ? resources.value
      : section.value === 'thresholds'
        ? thresholds.value
        : features.value
  return keys
    .map((k) => {
      const from = (cur as Record<string, unknown> | null)?.[k]
      const to = sourceOf(k)
      return `${k}: ${from} → ${to}`
    })
    .join('；')
  function sourceOf(k: string): unknown {
    const s = section.value === 'queue'
      ? queueEdit.value
      : section.value === 'resources'
        ? resourcesEdit.value
        : section.value === 'thresholds'
          ? thresholdsEdit.value
          : featuresEdit.value
    return s[k]
  }
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>系统设置</h2>
      <p class="dim hint">
        平台级配置：写操作必须填写原因并记入平台审计；阈值平台值仅为新建案件默认，
        在办案件以案件快照为准。
      </p>
    </div>

    <!-- 健康探针：登录可读 -->
    <div class="card">
      <div class="card-title">
        服务健康
        <NTag size="tiny" :bordered="false" :type="health?.meta_ok ? 'success' : 'error'">
          {{ health?.meta_ok ? '正常' : '探测异常' }}
        </NTag>
      </div>
      <NSpin :show="healthLoading">
        <div v-if="health" class="health-grid">
          <div class="health-item"><span class="dim">排队任务</span><b class="mono">{{ health.queue.pending }}</b></div>
          <div class="health-item"><span class="dim">运行任务</span><b class="mono">{{ health.queue.running }}</b></div>
          <div class="health-item"><span class="dim">Worker 并发</span><b class="mono">{{ health.worker.max_workers }}</b></div>
          <div class="health-item"><span class="dim">轮询间隔</span><b class="mono">{{ health.worker.poll_interval_ms }}ms</b></div>
          <div class="health-item"><span class="dim">后端版本</span><b class="mono">{{ health.versions.backend }}</b></div>
          <div class="health-item"><span class="dim">默认案件包</span><b class="mono">{{ health.versions.ontology_default }}</b></div>
        </div>
        <p v-if="health" class="dim worker-note">{{ health.worker.note }}</p>
      </NSpin>
    </div>

    <!-- 非管理员：fail-closed 锁定面板（不渲染任何管理数值） -->
    <EmptyState
      v-if="!isAdmin"
      type="forbidden"
      title="平台设置仅管理员可操作"
      desc="队列/资源/阈值/特性等管理面已锁定；如需调整请联系平台管理员。健康探针对所有登录用户可见。"
    />

    <NSpin v-else :show="adminLoading">
      <template v-if="queue && resources && thresholds && features">
        <!-- 队列设置 -->
        <div class="card">
          <div class="card-title">队列设置</div>
          <div v-for="meta in QUEUE_KEYS" :key="meta.key" class="edit-row">
            <span class="edit-label">{{ meta.label }}</span>
            <NInputNumber
              v-model:value="queueEdit[meta.key]"
              :min="meta.min"
              :max="meta.max"
              size="small"
              @blur="clampEdit(meta, 'queue', meta.key)"
            />
            <span v-if="meta.unit" class="dim">{{ meta.unit }}</span>
            <span v-if="meta.hint" class="dim edit-hint">{{ meta.hint }}</span>
          </div>
          <div class="edit-actions">
            <NButton size="small" type="primary" @click="openConfirm('queue')">保存队列设置</NButton>
          </div>
        </div>

        <!-- 资源限额 -->
        <div class="card">
          <div class="card-title">资源限额</div>
          <div v-for="meta in RESOURCE_KEYS" :key="meta.key" class="edit-row">
            <span class="edit-label">{{ meta.label }}</span>
            <NInput
              v-if="meta.readonly"
              :value="String(resources[meta.key as keyof ResourceSettings] ?? '')"
              size="small"
              class="readonly-input"
              readonly
            />
            <NInputNumber
              v-else
              v-model:value="resourcesEdit[meta.key]"
              :min="meta.min"
              :max="meta.max"
              size="small"
              @blur="clampEdit(meta, 'resources', meta.key)"
            />
            <span v-if="meta.unit" class="dim">{{ meta.unit }}</span>
            <span v-if="meta.hint" class="dim edit-hint">{{ meta.hint }}</span>
          </div>
          <div class="edit-actions">
            <NButton size="small" type="primary" @click="openConfirm('resources')">保存资源限额</NButton>
          </div>
        </div>

        <!-- 配置中心：交叉阈值 -->
        <div class="card">
          <div class="card-title">配置中心 · 交叉阈值</div>
          <p class="dim note">{{ THRESHOLDS_NOTE }}</p>
          <div v-for="meta in THRESHOLD_KEYS" :key="meta.key" class="edit-row">
            <span class="edit-label">{{ meta.label }}</span>
            <NInputNumber
              v-model:value="thresholdsEdit[meta.key]"
              :min="meta.min"
              :max="meta.max"
              size="small"
              @blur="clampEdit(meta, 'thresholds', meta.key)"
            />
            <span v-if="meta.unit" class="dim">{{ meta.unit }}</span>
            <span v-if="meta.hint" class="dim edit-hint">{{ meta.hint }}</span>
          </div>
          <div class="edit-actions">
            <NButton size="small" type="primary" @click="openConfirm('thresholds')">保存阈值默认</NButton>
          </div>
        </div>

        <!-- 界面特性 -->
        <div class="card">
          <div class="card-title">界面特性</div>
          <div v-for="meta in FEATURE_KEYS" :key="meta.key" class="edit-row">
            <span class="edit-label">{{ meta.label }}</span>
            <NSelect
              v-model:value="featuresEdit[meta.key]"
              :options="(meta.options ?? []).map((o) => ({ label: o, value: o }))"
              size="small"
              class="feature-select"
            />
            <span v-if="meta.hint" class="dim edit-hint">{{ meta.hint }}</span>
          </div>
          <div class="edit-actions">
            <NButton size="small" type="primary" @click="openConfirm('features')">保存特性</NButton>
          </div>
        </div>

        <!-- 红线锁定行 -->
        <div class="card locked-card">
          <div class="card-title">红线配置（永不可经界面开放）</div>
          <div v-for="r in LOCKED_REDLINES" :key="r.key" class="locked-row">
            <span class="locked-icon">🔒</span>
            <div>
              <div class="locked-label">{{ r.label }} <span class="mono dim">{{ r.key }}</span></div>
              <div class="dim locked-reason">{{ r.reason }}</div>
            </div>
          </div>
        </div>

        <!-- 案件包快照 -->
        <div class="card">
          <div class="card-title">案件包版本快照（{{ snapshots.length }}）</div>
          <div class="grid-wrap">
            <table class="grid">
              <thead>
                <tr><th>案件</th><th>案件包</th><th>版本</th><th>锁定时间</th></tr>
              </thead>
              <tbody>
                <tr v-for="(s, i) in snapshots" :key="i">
                  <td class="mono">{{ s.case_id ?? '—' }}</td>
                  <td class="mono">{{ s.pack_id ?? '—' }}</td>
                  <td class="mono">{{ s.version ?? '—' }}</td>
                  <td class="mono dim">{{ s.locked_at ?? '—' }}</td>
                </tr>
                <tr v-if="!snapshots.length">
                  <td colspan="4" class="grid-empty">暂无快照记录</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </NSpin>

    <ConfigConfirmDialog
      v-model:show="showConfirm"
      v-model:reason="reason"
      :dangerous="true"
      :title="`变更确认：${SECTION_TITLE[section]}`"
      :detail="confirmDetail"
      :loading="busy"
      reason-placeholder="请写明调整依据（如：案件量增长，worker 并发 2→4 以缩短排队）"
      @confirm="submit"
    />
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
.hint {
  font-size: 12px;
  margin: 4px 0 0;
}
.card {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-title {
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.health-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}
.health-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
}
.health-item b {
  font-size: 15px;
}
.worker-note {
  font-size: 11px;
  margin: 0;
}
.edit-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  flex-wrap: wrap;
}
.edit-label {
  min-width: 160px;
  color: var(--sun-text-secondary);
}
.edit-hint {
  font-size: 11px;
}
.readonly-input {
  max-width: 360px;
  opacity: 0.7;
}
.feature-select {
  width: 200px;
}
.edit-actions {
  display: flex;
  justify-content: flex-end;
}
.note {
  font-size: 12px;
  margin: 0;
}
.locked-card {
  border-color: var(--sun-error-border);
}
.locked-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.locked-icon {
  font-size: 14px;
}
.locked-label {
  font-size: 12px;
  font-weight: 600;
}
.locked-reason {
  font-size: 11px;
}
.grid-wrap {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: auto;
}
.grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.grid th,
.grid td {
  text-align: left;
  padding: 7px 12px;
  border-bottom: 1px solid var(--sun-border);
}
.grid th {
  color: var(--sun-text-tertiary);
  font-weight: 500;
  font-size: 11px;
  white-space: nowrap;
}
.grid-empty {
  text-align: center;
  color: var(--sun-text-tertiary);
  padding: 18px 0;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
