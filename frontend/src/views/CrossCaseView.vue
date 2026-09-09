<script setup lang="ts">
// 跨案件查询（FE-P-015，/c/cross-case）。
// 红线：全有或全无——所选案件任一不在本租户授权全集即整体拒发（后端 403 双保险）；
// SQL 首词白名单 SELECT/WITH/PRAGMA + READ_ONLY ATTACH 在后端；前端只产出 UNION ALL
// 模板骨架（跨案同对象），自由 SQL 由分析师书写；查询事由必填留痕。
import { computed, onMounted, ref } from 'vue'
import {
  NSpin, NButton, NInput, NCheckbox, NSelect, NCollapse, NCollapseItem,
  NInputNumber, NTag, useMessage,
} from 'naive-ui'
import {
  crossCaseApi, type CrossCaseHistoryItem, type CrossCaseQueryResult,
} from '../api/endpoints/crossCase'
import { useCaseStore } from '../stores/case'
import { presentError, isApiError } from '../api/errors'
import {
  MAX_ROWS, TIMEOUT_MS, TEMPLATE_TABLES, authorizeSelection, buildUnionTemplate,
  canExecute, clampMaxRows, clampTimeoutMs, executeBlockReason, hasSourceCaseColumn,
  reasonError,
} from '../domain/crossCase'
import PolicyGate from '../components/common/PolicyGate.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

const selected = ref<string[]>([])
const reason = ref('')
const sql = ref('')
const templateTable = ref<string | null>(null)
const maxRows = ref(MAX_ROWS.def)
const timeoutMs = ref(TIMEOUT_MS.def)

const running = ref(false)
const result = ref<CrossCaseQueryResult | null>(null)
const resultError = ref('')

const historyLoading = ref(false)
const history = ref<CrossCaseHistoryItem[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const pageSize = 20

onMounted(async () => {
  if (!cs.cases.length) {
    try {
      await cs.loadCases()
    } catch (e) {
      message.error(isApiError(e) ? e.message : presentError(e).title)
    }
  }
  void loadHistory()
})

const ownedIds = computed(() => cs.cases.map((c) => c.id))
const authz = computed(() => authorizeSelection(selected.value, ownedIds.value))
const executable = computed(() => canExecute(authz.value))
const blockReason = computed(() => executeBlockReason(authz.value))
const reasonErr = computed(() => reasonError(reason.value))

const sqlReady = computed(() => sql.value.trim().length > 0)

function toggleCase(id: string): void {
  const i = selected.value.indexOf(id)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(id)
}

function removeDenied(id: string): void {
  selected.value = selected.value.filter((x) => x !== id)
}

function applyTemplate(): void {
  if (!templateTable.value) return
  if (authz.value.authorized.length < 2) {
    message.warning('请先选择至少 2 个有权案件')
    return
  }
  sql.value = buildUnionTemplate(authz.value.authorized, templateTable.value)
}

const resultColumns = computed(() => {
  const rows = result.value?.rows ?? []
  const keys: string[] = []
  for (const r of rows) {
    for (const k of Object.keys(r)) {
      if (!keys.includes(k)) keys.push(k)
    }
  }
  return keys
})

const resultHasSourceCase = computed(() => hasSourceCaseColumn(result.value?.rows ?? []))

async function runQuery(): Promise<void> {
  if (!executable.value) {
    message.error(blockReason.value || '当前选择不可执行')
    return
  }
  if (reasonErr.value) {
    message.error(reasonErr.value)
    return
  }
  if (!sqlReady.value) {
    message.error('请填写查询 SQL（或选择模板生成）')
    return
  }
  running.value = true
  resultError.value = ''
  result.value = null
  try {
    result.value = await crossCaseApi.query({
      case_ids: authz.value.authorized,
      sql: sql.value,
      reason: reason.value.trim(),
      max_rows: clampMaxRows(maxRows.value),
      timeout_ms: clampTimeoutMs(timeoutMs.value),
    })
    message.success(`查询完成，返回 ${result.value.total} 行`)
    void loadHistory()
  } catch (e) {
    // 全有或全无 403 / SQL 白名单 / 超时等：错误信封直出
    resultError.value = isApiError(e) ? e.message : presentError(e).title
    message.error(resultError.value)
  } finally {
    running.value = false
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    const page = await crossCaseApi.history(historyPage.value, pageSize)
    history.value = page.items
    historyTotal.value = page.total
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    historyLoading.value = false
  }
}

function historyPages(): number {
  return Math.max(1, Math.ceil(historyTotal.value / pageSize))
}

function goHistory(p: number): void {
  historyPage.value = Math.min(historyPages(), Math.max(1, p))
  void loadHistory()
}

function reuseHistory(h: CrossCaseHistoryItem): void {
  selected.value = [...h.case_ids]
  sql.value = h.sql
  reason.value = h.reason
  message.info('已回填该次查询的案件选择与 SQL')
}

function cellText(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>跨案件查询</h2>
      <p class="dim hint">
        只读 ATTACH 多案件库做串并案分析（别名 case_&lt;案件编号&gt;）。全有或全无鉴权：任一案件无权整体拒发；
        查询与事由全程留痕，结果强制行数上限。
      </p>
    </div>

    <!-- 案件多选 -->
    <div class="card">
      <div class="card-title">
        选择案件（{{ authz.m }}/{{ selected.length }} 有权）
        <span v-if="authz.denied.length" class="denied-flag">⚠ {{ authz.denied.length }} 个无权</span>
      </div>
      <EmptyState
        v-if="!cs.cases.length"
        type="empty"
        title="本租户暂无可见案件"
        desc="跨案件查询需要至少 2 个有权案件"
      />
      <div v-else class="case-picker">
        <!-- div 单点切换：label 包 NCheckbox 会触发 label 原生激活 + 组件自身双重 toggle -->
        <div
          v-for="c in cs.cases"
          :key="c.id"
          class="case-chip"
          :class="{
            checked: selected.includes(c.id),
            denied: selected.includes(c.id) && authz.denied.includes(c.id),
          }"
          @click="toggleCase(c.id)"
        >
          <NCheckbox :checked="selected.includes(c.id)" />
          <span class="chip-name">{{ c.name }}</span>
          <span class="chip-id mono dim">{{ c.id }}</span>
          <NTag v-if="c.status" size="tiny" :bordered="false">{{ c.status }}</NTag>
        </div>
      </div>
      <div v-if="authz.denied.length" class="denied-bar">
        <span class="denied-text">无权案件（全有或全无，移除后才能执行）：</span>
        <span v-for="d in authz.denied" :key="d" class="denied-chip">
          {{ d }}
          <button class="denied-x" @click="removeDenied(d)">×</button>
        </span>
      </div>
    </div>

    <!-- SQL + 事由 -->
    <div class="card">
      <div class="card-title">查询</div>
      <div class="sql-row">
        <NSelect
          v-model:value="templateTable"
          class="tpl-select"
          size="small"
          placeholder="选择跨案同对象模板…"
          :options="TEMPLATE_TABLES"
          clearable
          @update:value="applyTemplate"
        />
        <span class="dim tpl-hint">模板生成 UNION ALL 骨架（自带 source_case 来源列）；也可直接书写自由 SQL</span>
      </div>
      <NInput
        v-model:value="sql"
        type="textarea"
        :rows="6"
        class="sql-input mono"
        placeholder="SELECT 'c1' AS source_case, t.* FROM case_c1.obj_person t&#10;UNION ALL&#10;SELECT 'c2' AS source_case, t.* FROM case_c2.obj_person t"
      />
      <div class="reason-row">
        <NInput
          v-model:value="reason"
          type="textarea"
          :rows="2"
          placeholder="查询事由（必填，审计留痕，≤500 字）"
          :status="reasonErr ? 'error' : undefined"
        />
        <span v-if="reasonErr" class="form-err">{{ reasonErr }}</span>
      </div>

      <NCollapse>
        <NCollapseItem title="高级选项（行数上限 / 超时）" name="adv">
          <div class="adv-grid">
            <label class="adv-row">
              <span>行数上限 max_rows（{{ MAX_ROWS.min }}–{{ MAX_ROWS.max }}）</span>
              <NInputNumber v-model:value="maxRows" :min="MAX_ROWS.min" :max="MAX_ROWS.max" size="small" />
            </label>
            <label class="adv-row">
              <span>超时 timeout_ms（{{ TIMEOUT_MS.min }}–{{ TIMEOUT_MS.max }}）</span>
              <NInputNumber v-model:value="timeoutMs" :min="TIMEOUT_MS.min" :max="TIMEOUT_MS.max" size="small" />
            </label>
          </div>
        </NCollapseItem>
      </NCollapse>

      <div class="exec-row">
        <NButton
          type="primary"
          :loading="running"
          :disabled="!executable || !sqlReady || !!reasonErr"
          @click="runQuery"
        >
          执行跨案查询
        </NButton>
        <span v-if="blockReason" class="block-reason">{{ blockReason }}</span>
      </div>
    </div>

    <!-- 结果区（PolicyGate：仅可执行态后才有结果；失败显式报错不伪装空态） -->
    <PolicyGate :allow="executable" show-fallback>
      <div class="card">
        <div class="card-title">查询结果</div>
        <NSpin :show="running">
          <EmptyState v-if="resultError" type="error" title="查询被拒绝/失败" :desc="resultError" />
          <EmptyState v-else-if="!result" type="empty" title="尚未执行查询" desc="执行后在此展示结果（强制 LIMIT 保护）" />
          <EmptyState v-else-if="result.rows.length === 0" type="empty" title="查询完成：0 行" desc="无匹配记录" />
          <template v-else>
            <p v-if="!resultHasSourceCase" class="dim source-note">
              ⚠ 结果未含 source_case 来源列：自由 SQL 请自行 SELECT 案件来源标识（模板已自带）。
            </p>
            <div class="grid-wrap">
              <table class="grid">
                <thead>
                  <tr>
                    <th v-for="col in resultColumns" :key="col">{{ col }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in result.rows" :key="i">
                    <td v-for="col in resultColumns" :key="col" class="mono">{{ cellText(r[col]) || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="dim result-total">共 {{ result.total }} 行（{{ result.case_ids.join('、') }}）</p>
          </template>
        </NSpin>
      </div>

      <template #fallback>
        <EmptyState type="forbidden" title="查询条件未满足" :desc="blockReason || '选择至少 2 个有权案件后可执行'" />
      </template>
    </PolicyGate>

    <!-- 我的查询留痕 -->
    <div class="card">
      <div class="card-title">我的查询留痕（仅本人可见）</div>
      <NSpin :show="historyLoading">
        <div class="grid-wrap">
          <table class="grid">
            <thead>
              <tr><th>时间</th><th>案件</th><th>事由</th><th>行数</th><th>SQL</th><th /></tr>
            </thead>
            <tbody>
              <tr v-for="h in history" :key="h.id">
                <td class="mono dim">{{ h.ts }}</td>
                <td class="mono">{{ h.case_ids.join('、') }}</td>
                <td>{{ h.reason }}</td>
                <td class="mono">{{ h.result_rows }}</td>
                <td class="mono dim sql-cell">{{ h.sql }}</td>
                <td><NButton size="tiny" quaternary @click="reuseHistory(h)">回填</NButton></td>
              </tr>
              <tr v-if="!history.length">
                <td colspan="6" class="grid-empty">暂无跨案查询记录</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="historyTotal > pageSize" class="pager">
          <NButton size="tiny" :disabled="historyPage <= 1" @click="goHistory(historyPage - 1)">上一页</NButton>
          <span class="dim">{{ historyPage }} / {{ historyPages() }}</span>
          <NButton size="tiny" :disabled="historyPage >= historyPages()" @click="goHistory(historyPage + 1)">下一页</NButton>
        </div>
      </NSpin>
    </div>
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
.denied-flag {
  color: var(--sun-error-text);
  font-size: 12px;
  font-weight: 400;
}
.case-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.case-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
}
.case-chip.checked {
  border-color: var(--sun-info-border, var(--sun-ok-border));
  background: var(--sun-info-bg, var(--sun-ok-bg));
}
.case-chip.denied {
  border-color: var(--sun-error-border);
  background: var(--sun-error-bg);
}
.chip-name {
  font-weight: 600;
}
.chip-id {
  font-size: 11px;
}
.denied-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  background: var(--sun-error-bg);
  border: 1px solid var(--sun-error-border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
}
.denied-text {
  color: var(--sun-error-text);
}
.denied-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-error-border);
  color: var(--sun-error-text);
  border-radius: 10px;
  padding: 0 8px;
  font-family: var(--sun-font-mono);
  font-size: 11px;
}
.denied-x {
  background: none;
  border: none;
  color: inherit;
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  padding: 0 0 0 2px;
}
.sql-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.tpl-select {
  width: 260px;
}
.tpl-hint {
  font-size: 11px;
}
.sql-input :deep(textarea) {
  font-family: var(--sun-font-mono);
  font-size: 12px;
}
.reason-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.form-err {
  font-size: 12px;
  color: var(--sun-error-text);
}
.adv-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.adv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  color: var(--sun-text-secondary);
  max-width: 480px;
}
.exec-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.block-reason {
  font-size: 12px;
  color: var(--sun-error-text);
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
  padding: 7px 10px;
  border-bottom: 1px solid var(--sun-border);
  vertical-align: top;
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
  padding: 20px 0;
}
.sql-cell {
  max-width: 320px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.source-note {
  font-size: 12px;
  margin: 0;
  color: var(--sun-warn-text);
}
.result-total {
  font-size: 12px;
  margin: 0;
}
.pager {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: center;
  margin-top: 8px;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
