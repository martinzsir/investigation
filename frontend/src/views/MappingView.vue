<script setup lang="ts">
// 映射校验（MVP-4，/c/mapping）。
// 红线四：预检只产出冲突诊断 + 出路（A 拆 source_sql / B 整列降级），
// 响应里没有 force/ignore/continue——页面也绝不提供「忽略继续入库」；
// 缺列/CAST 失败降级在此页聚合展示（missing-columns，无分页）。
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { etlApi, type ValidateResult, type MissingColumnItem } from '../api/endpoints/etl'
import { presentError, isApiError } from '../api/errors'
import { groupConflicts, conflictSummary, CONFLICT_LABEL, PATH_GUIDE } from '../domain/etlConfig'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

const targetTable = ref('银行流水')
/** 映射行：源列 → 目标属性 */
const rows = ref<Array<{ col: string; prop: string }>>([
  { col: '付款方名称', prop: 'from_raw' },
  { col: '收款方名称', prop: 'to_raw' },
  { col: '交易金额', prop: 'amount' },
  { col: '交易日期', prop: 'date' },
])

const validating = ref(false)
const result = ref<ValidateResult | null>(null)

const missing = ref<MissingColumnItem[]>([])
const missingTotal = ref(0)

const groups = computed(() => (result.value ? groupConflicts(result.value.conflicts) : []))

function addRow(): void {
  rows.value.push({ col: '', prop: '' })
}
function removeRow(i: number): void {
  rows.value.splice(i, 1)
  result.value = null
}

async function runValidate(): Promise<void> {
  if (!cs.currentCaseId) return
  const filled = rows.value.filter((r) => r.col.trim() && r.prop.trim())
  if (!filled.length) {
    message.error('请至少填写一行源列 → 属性映射')
    return
  }
  if (filled.some((r) => !r.col.trim() || !r.prop.trim())) {
    message.error('存在未填完整的映射行，请补全或删除')
    return
  }
  validating.value = true
  result.value = null
  try {
    const mapping: Record<string, string> = {}
    for (const r of filled) mapping[r.col.trim()] = r.prop.trim()
    result.value = await etlApi.validateMapping(cs.currentCaseId, targetTable.value, mapping)
    if (result.value.valid) message.success('预检通过：无一对多/未知属性/缺列冲突')
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    validating.value = false
  }
}

async function loadMissing(): Promise<void> {
  if (!cs.currentCaseId) {
    missing.value = []
    return
  }
  try {
    const res = await etlApi.missingColumns(cs.currentCaseId)
    missing.value = res.items
    missingTotal.value = res.total_warnings
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  }
}

watch(() => cs.currentCaseId, loadMissing, { immediate: true })

const KIND_LABEL: Record<string, string> = {
  source_column_missing: '源列缺失',
  source_value_cast_failed: '值类型转换失败',
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>映射校验</h2>
      <p class="dim hint">
        入库前预检：源列 → 对象属性映射是否一对多/指向未声明属性/源表缺列。预检不写盘、不改配置。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="映射校验按案件进行" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 预检冲突只有两条出路：<b>A 拆分 source_sql</b> 或 <b>B 整列降级 NULL</b>；
        系统不提供「忽略继续入库」——冲突必须先处置。
      </div>

      <!-- 映射编辑 + 预检 -->
      <div class="card">
        <div class="card-title">映射预检</div>
        <div class="target-row">
          <span class="dim">目标源表：</span>
          <NInput v-model:value="targetTable" size="small" class="target-input" placeholder="如：银行流水" />
        </div>
        <div class="grid-wrap">
          <table class="grid">
            <thead><tr><th>源列</th><th></th><th>目标属性（obj.属性）</th><th></th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in rows" :key="i">
                <td><NInput v-model:value="r.col" size="small" placeholder="源列名，如：交易金额" @update:value="result = null" /></td>
                <td class="arrow">→</td>
                <td><NInput v-model:value="r.prop" size="small" placeholder="属性名，如：amount" @update:value="result = null" /></td>
                <td><NButton size="tiny" quaternary type="error" @click="removeRow(i)">删</NButton></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="actions">
          <NButton size="small" dashed @click="addRow">+ 加一行</NButton>
          <NButton type="primary" size="small" :loading="validating" @click="runValidate">预检</NButton>
        </div>

        <!-- 预检结果 -->
        <div v-if="result" class="result" :class="result.valid ? 'result-ok' : 'result-bad'">
          <div v-if="result.valid" class="result-title">✓ 预检通过，无映射冲突</div>
          <template v-else>
            <div class="result-title">⛔ 发现 {{ result.conflicts.length }} 处冲突（{{ conflictSummary(result.conflicts) }}）</div>
            <div v-for="g in groups" :key="g.type" class="conflict-group">
              <div class="conflict-type">{{ CONFLICT_LABEL[g.type] }}（{{ g.items.length }}）</div>
              <ul>
                <li v-for="(c, i) in g.items" :key="i" class="mono">{{ c.message }}</li>
              </ul>
            </div>
            <div class="paths">
              <div class="path-item"><b>出路 A</b>：{{ PATH_GUIDE.A_split_source_sql }}</div>
              <div class="path-item"><b>出路 B</b>：{{ PATH_GUIDE.B_degrade_column }}</div>
            </div>
          </template>
        </div>
      </div>

      <!-- 缺列/降级聚合 -->
      <div class="card">
        <div class="card-title">缺列与降级诊断（{{ missingTotal }}）</div>
        <p class="dim hint">最近一次装载中声明了但源表缺列、或值转换失败的属性（数据已降级 NULL 或隔离）：</p>
        <div class="grid-wrap">
          <table class="grid">
            <thead><tr><th>对象</th><th>属性</th><th>行数</th><th>诊断种类</th><th>样本（已遮蔽）</th></tr></thead>
            <tbody>
              <tr v-for="(m, i) in missing" :key="i">
                <td class="mono">{{ m.object }}</td>
                <td class="mono">{{ m.property }}</td>
                <td>{{ m.count }}</td>
                <td>
                  <NTag v-for="k in m.kinds" :key="k" size="tiny" :bordered="false" :type="k === 'source_value_cast_failed' ? 'warning' : 'default'">
                    {{ KIND_LABEL[k] ?? k }}
                  </NTag>
                </td>
                <td class="dim samples">{{ m.samples.join('；') }}</td>
              </tr>
              <tr v-if="!missing.length"><td colspan="5" class="dim empty-row">无缺列/降级记录（最近一次装载全部声明列均有数据）</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.hint { font-size: 12px; margin: 4px 0 0; }
.notice-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.card-title { font-size: 13px; font-weight: 600; }
.target-row { display: flex; align-items: center; gap: 8px; }
.target-input { max-width: 240px; }
.grid-wrap { border: 1px solid var(--sun-border); border-radius: 4px; overflow: auto; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; }
.arrow { color: var(--sun-text-tertiary); text-align: center; width: 28px; }
.actions { display: flex; gap: 8px; }
.result { border-radius: 4px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
.result-ok { background: var(--sun-ok-bg); border: 1px solid var(--sun-ok-border); }
.result-ok .result-title { color: var(--sun-ok-text); font-weight: 600; font-size: 13px; }
.result-bad { background: var(--sun-error-bg); border: 1px solid var(--sun-error-border); }
.result-title { color: var(--sun-error-text); font-weight: 600; font-size: 13px; }
.conflict-type { font-size: 12px; font-weight: 600; margin-top: 4px; }
.result ul { margin: 4px 0 0; padding-left: 18px; font-size: 12px; }
.paths { display: flex; flex-direction: column; gap: 4px; font-size: 12px; margin-top: 4px; }
.path-item { color: var(--sun-text-secondary); }
.samples { max-width: 360px; word-break: break-all; }
.empty-row { text-align: center; padding: 14px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
