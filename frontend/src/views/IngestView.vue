<script setup lang="ts">
// FE-P-002 数据接入向导（MVP-3「能接入」，/c/wizard）。
// 四步 Stepper：上传 → 列分析与映射（低置信琥珀高亮 / 缺必选列降级警告非阻断）→ 导入确认 → 完成。
// 长任务导入立即返回 task（不无意义转圈），跳任务中心看 SSE 进度；重复指纹 409 拦截不重复入队。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NSpin, NButton, NSelect, NTag, useMessage,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { sourcesApi, type UploadResult } from '../api/endpoints/sources'
import { recommendationsApi } from '../api/endpoints/recommendations'
import { presentError, isApiError } from '../api/errors'
import type { TaskRow } from '../domain/task'
import {
  MATCH_META, matchTone, buildWizardMapping, toImportColumnMap,
  missingRequiredColumns, mappedCount, duplicateSourceMessage, classifyIngestError,
  type AnalyzeResult, type ColumnMatch, type DeclaredTable,
} from '../domain/mapping'
import Stepper from '../components/common/Stepper.vue'
import EmptyState from '../components/common/EmptyState.vue'

const STEPS = ['上传文件', '列分析与映射', '确认导入', '完成']

const cs = useCaseStore()
const router = useRouter()
const message = useMessage()

const step = ref(0)
const busy = ref(false)
const file = ref<File | null>(null)
const upload = ref<UploadResult | null>(null)
const analysis = ref<AnalyzeResult | null>(null)
const targetTable = ref('')
/** 向导视角映射 {声明列: 上传源列} */
const mapping = ref<Record<string, string>>({})
const importTask = ref<TaskRow | null>(null)
const duplicated = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const toneClass: Record<string, string> = {
  ok: 'tag-ok', info: 'tag-info', warn: 'tag-warn', muted: 'tag-muted',
}

/** 当前目标表的声明列（required + optional） */
const declaredColumns = computed<{ name: string; required: boolean }[]>(() => {
  if (!analysis.value) return []
  const t: DeclaredTable | undefined = analysis.value.declared_tables.find(
    (x) => x.name === targetTable.value,
  )
  if (!t) return []
  return [
    ...t.required_columns.map((name) => ({ name, required: true })),
    ...t.optional_columns.map((name) => ({ name, required: false })),
  ]
})

/** 每个声明列的建议匹配（来自 analyze suggestion） */
const matchByProp = computed<Map<string, ColumnMatch>>(() => {
  const m = new Map<string, ColumnMatch>()
  for (const match of analysis.value?.suggestion.matches ?? []) {
    m.set(match.target_prop, match)
  }
  return m
})

/** 上传源列选项 */
const sourceOptions = computed(() =>
  (analysis.value?.columns ?? []).map((c) => ({ label: c.name, value: c.name })),
)

const tableOptions = computed(() =>
  (analysis.value?.declared_tables ?? []).map((t) => ({
    label: t.title ? `${t.title}（${t.name}）` : t.name,
    value: t.name,
  })),
)

const missingRequired = computed(() =>
  analysis.value ? missingRequiredColumns(analysis.value.suggestion) : [],
)

/** 当前未映射的必选列（编辑后的真实缺失，用于确认页警告） */
const unmappedRequired = computed(() =>
  declaredColumns.value
    .filter((c) => c.required && !mapping.value[c.name])
    .map((c) => c.name),
)

function pickFile(): void {
  fileInput.value?.click()
}

async function onFileChange(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f || !cs.currentCaseId) return
  file.value = f
  busy.value = true
  try {
    upload.value = await sourcesApi.upload(cs.currentCaseId, f)
    message.success(`上传成功：${f.name}（${upload.value.rows} 行），正在分析列…`)
    await runAnalyze(upload.value.upload_id)
    step.value = 1
  } catch (err) {
    const kind = isApiError(err) ? classifyIngestError(err.code, err.message) : 'other'
    if (kind === 'format') {
      message.error(`文件格式不支持或解析失败：${presentError(err).title}`)
    } else {
      message.error(presentError(err).title)
    }
    file.value = null
    upload.value = null
  } finally {
    busy.value = false
    input.value = ''
  }
}

async function runAnalyze(uploadId: string, table?: string): Promise<void> {
  if (!cs.currentCaseId) return
  busy.value = true
  try {
    analysis.value = await sourcesApi.analyze(cs.currentCaseId, uploadId, table)
    const sug = analysis.value.suggestion
    if (!targetTable.value && sug.target_table) targetTable.value = sug.target_table
    mapping.value = buildWizardMapping(sug)
  } catch (err) {
    message.error(`列分析失败：${presentError(err).title}`)
  } finally {
    busy.value = false
  }
}

async function onTableChange(name: string): Promise<void> {
  targetTable.value = name
  mapping.value = {}
  if (upload.value) await runAnalyze(upload.value.upload_id, name)
}

function matchOf(prop: string): ColumnMatch | undefined {
  return matchByProp.value.get(prop)
}

function goConfirm(): void {
  if (!targetTable.value) {
    message.warning('请先选择目标表')
    return
  }
  step.value = 2
}

async function doImport(): Promise<void> {
  if (!cs.currentCaseId || !upload.value) return
  busy.value = true
  duplicated.value = false
  try {
    const columnMap = toImportColumnMap(mapping.value)
    importTask.value = await sourcesApi.import(cs.currentCaseId, upload.value.upload_id, {
      target_table: targetTable.value,
      column_map: columnMap,
    })
    message.success('导入任务已提交，正在后台运行（可到任务中心查看实时进度）')
    step.value = 3
  } catch (err) {
    if (isApiError(err) && err.code === 'CONFLICT') {
      duplicated.value = true
      message.warning(duplicateSourceMessage(err.message))
      step.value = 3
    } else {
      const kind = isApiError(err) ? classifyIngestError(err.code, err.message) : 'other'
      if (kind === 'permission') message.error('数据导入需偏将及以上权限（clearance≥2）')
      else if (kind === 'mapping') message.error(`列映射失败：${presentError(err).title}`)
      else message.error(presentError(err).title)
    }
  } finally {
    busy.value = false
  }
}

async function genRecommendations(): Promise<void> {
  if (!cs.currentCaseId || !upload.value) return
  try {
    await recommendationsApi.create(cs.currentCaseId, upload.value.upload_id)
    message.success('数据元推荐任务已提交，到「接入建议」查看待核实草案')
    void router.push('/c/suggest')
  } catch (err) {
    message.error(presentError(err).title)
  }
}

function reset(): void {
  step.value = 0
  file.value = null
  upload.value = null
  analysis.value = null
  targetTable.value = ''
  mapping.value = {}
  importTask.value = null
  duplicated.value = false
}
</script>

<template>
  <div class="page">
    <div class="page-head"><h2>数据接入向导</h2></div>

    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="数据接入按案件归属，请在顶部案件选择器中选择案件"
    />

    <template v-else>
      <Stepper :steps="STEPS" :current="step" />

      <NSpin :show="busy">
        <div class="panel">
          <!-- 步骤 0：上传 -->
          <div v-if="step === 0" class="step-body">
            <div class="upload-zone" @click="pickFile">
              <div class="upload-icon">⬆</div>
              <p class="upload-title">点击选择数据文件</p>
              <p class="upload-hint dim">支持 CSV / Excel / Parquet / JSON / SQLite；上传后自动生成指纹与列画像</p>
              <p v-if="file" class="upload-file">已选：{{ file.name }}</p>
              <input
                ref="fileInput"
                type="file"
                accept=".csv,.tsv,.txt,.xlsx,.xls,.parquet,.pq,.json,.ndjson,.jsonl,.sqlite,.db,.sqlite3"
                style="display:none"
                @change="onFileChange"
              />
            </div>
          </div>

          <!-- 步骤 1：分析与映射 -->
          <div v-else-if="step === 1" class="step-body">
            <div class="row-between">
              <div class="field">
                <label>目标表</label>
                <NSelect
                  :value="targetTable"
                  :options="tableOptions"
                  placeholder="选择数据落地的目标源表"
                  style="width: 320px"
                  @update:value="onTableChange"
                />
              </div>
              <NButton size="small" :disabled="!upload" @click="upload && runAnalyze(upload.upload_id, targetTable)">重新分析</NButton>
            </div>

            <!-- 缺必选列：降级警告，非阻断 -->
            <div v-if="missingRequired.length" class="warn-bar">
              ⚠ 有 {{ missingRequired.length }} 个必选列未自动匹配：
              <span class="mono">{{ missingRequired.join('、') }}</span>
              。可手动在下方选择源列；留空将按空值策略处理，<b>不阻断导入</b>。
            </div>

            <div class="table-scroll">
              <table class="m-table">
                <thead>
                  <tr><th>声明列（目标属性）</th><th>必选</th><th>匹配</th><th>映射到上传列</th></tr>
                </thead>
                <tbody>
                  <tr
                    v-for="col in declaredColumns"
                    :key="col.name"
                    :class="{ 'row--low': matchOf(col.name) && matchTone(matchOf(col.name)!) === 'warn' && !mapping[col.name] }"
                  >
                    <td class="mono">{{ col.name }}</td>
                    <td>
                      <NTag v-if="col.required" size="small" type="warning" :bordered="false">必选</NTag>
                      <span v-else class="dim">可选</span>
                    </td>
                    <td>
                      <span
                        v-if="matchOf(col.name)"
                        class="match-tag"
                        :class="toneClass[matchTone(matchOf(col.name)!)]"
                      >
                        {{ MATCH_META[matchOf(col.name)!.match_type].label }}
                        （{{ Math.round(matchOf(col.name)!.confidence * 100) }}%）
                      </span>
                      <span v-else class="dim">—</span>
                    </td>
                    <td>
                      <NSelect
                        :value="mapping[col.name] ?? null"
                        :options="[{ label: '（不导入此列）', value: '' }, ...sourceOptions]"
                        placeholder="选择源列"
                        size="small"
                        style="width: 260px"
                        @update:value="(v: string) => { if (v) mapping[col.name] = v; else delete mapping[col.name] }"
                      />
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div class="step-actions">
              <NButton @click="step = 0">上一步</NButton>
              <NButton type="primary" :disabled="!targetTable" @click="goConfirm">
                下一步（已映射 {{ mappedCount(mapping) }} 列）
              </NButton>
            </div>
          </div>

          <!-- 步骤 2：确认导入 -->
          <div v-else-if="step === 2" class="step-body">
            <div class="confirm-grid">
              <div class="cf-item"><span class="dim">文件</span><b>{{ upload?.filename }}</b></div>
              <div class="cf-item"><span class="dim">格式</span><b>{{ upload?.format }}</b></div>
              <div class="cf-item"><span class="dim">行数</span><b class="mono">{{ upload?.rows }}</b></div>
              <div class="cf-item"><span class="dim">目标表</span><b class="mono">{{ targetTable }}</b></div>
              <div class="cf-item"><span class="dim">已映射列</span><b class="mono">{{ mappedCount(mapping) }}</b></div>
              <div class="cf-item cf-fp"><span class="dim">指纹（文件名+SHA-256+行数）</span><b class="mono fp">{{ upload?.fingerprint }}</b></div>
            </div>
            <div v-if="unmappedRequired.length" class="warn-bar">
              ⚠ 仍有 {{ unmappedRequired.length }} 个必选列未映射：<span class="mono">{{ unmappedRequired.join('、') }}</span>。这些列将按空值策略降级处理，可继续导入。
            </div>
            <p class="dim hint">提交后立即返回任务并在后台运行（导入 → 清洗 → 链式构建语义层），可到任务中心查看实时进度。重复文件（指纹一致）将被拦截，不会产生重复数据。</p>
            <div class="step-actions">
              <NButton :disabled="busy" @click="step = 1">上一步</NButton>
              <NButton type="primary" :loading="busy" @click="doImport">确认导入</NButton>
            </div>
          </div>

          <!-- 步骤 3：完成 -->
          <div v-else class="step-body done-body">
            <div class="done-icon">{{ duplicated ? '⏭' : '✓' }}</div>
            <h3 v-if="duplicated">文件已存在，已跳过</h3>
            <h3 v-else>导入任务已提交</h3>
            <p v-if="duplicated" class="dim">相同数据源（文件名 + 内容 SHA-256 + 行数 一致）已在导入中，未重复入队。</p>
            <p v-else class="dim">任务 <span class="mono">{{ importTask?.id }}</span> 正在后台运行，进度可在任务中心实时查看。</p>
            <div class="done-actions">
              <NButton type="primary" @click="router.push('/tasks')">前往任务中心</NButton>
              <NButton v-if="!duplicated" @click="genRecommendations">生成数据元建议</NButton>
              <NButton quaternary @click="reset">继续导入其他文件</NButton>
            </div>
          </div>
        </div>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.panel {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 16px 18px;
}
.step-body { min-height: 240px; }
.upload-zone {
  border: 1px dashed var(--sun-border); border-radius: 6px;
  padding: 48px 20px; text-align: center; cursor: pointer;
  transition: border-color 0.15s;
}
.upload-zone:hover { border-color: var(--sun-info-border, var(--sun-ok-border)); }
.upload-icon { font-size: 34px; color: var(--sun-text-tertiary); }
.upload-title { font-size: 14px; font-weight: 600; margin: 10px 0 4px; }
.upload-hint { font-size: 12px; }
.upload-file { font-size: 12px; color: var(--sun-ok-text); font-family: var(--sun-font-mono); }
.row-between { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 12px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; color: var(--sun-text-secondary); }
.warn-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 4px; padding: 8px 12px;
  font-size: 12px; margin: 10px 0;
}
.table-scroll { overflow-x: auto; margin: 10px 0; }
.m-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.m-table th {
  text-align: left; font-weight: 400; color: var(--sun-text-tertiary);
  padding: 6px 8px; border-bottom: 1px solid var(--sun-border);
}
.m-table td { padding: 7px 8px; border-bottom: 1px dashed rgba(16, 49, 74, 0.6); }
.row--low { background: rgba(255, 179, 0, 0.06); }
.match-tag {
  display: inline-block; font-size: 11px; padding: 0 8px; border-radius: 10px; border: 1px solid;
}
.tag-ok { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.tag-info { color: var(--sun-info-text, var(--sun-ok-text)); border-color: var(--sun-info-border, var(--sun-ok-border)); background: var(--sun-info-bg, var(--sun-ok-bg)); }
.tag-warn { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.tag-muted { color: var(--sun-text-tertiary); border-color: var(--sun-border); }
.step-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.confirm-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; margin: 8px 0; }
.cf-item { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.cf-item .dim { font-size: 11px; }
.cf-fp { grid-column: 1 / -1; }
.fp { word-break: break-all; font-size: 11px; color: var(--sun-text-secondary); }
.hint { font-size: 12px; margin-top: 12px; }
.done-body { text-align: center; padding: 30px 20px; }
.done-icon { font-size: 44px; color: var(--sun-ok-text); }
.done-body h3 { margin: 12px 0 6px; font-size: 16px; }
.done-actions { display: flex; gap: 10px; justify-content: center; margin-top: 20px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
