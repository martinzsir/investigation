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
import { presentError, isApiError } from '../api/errors'
import type { TaskRow } from '../domain/task'
import {
  MATCH_META, matchTone, buildWizardMapping, toImportColumnMap,
  missingRequiredColumns, mappedCount, duplicateSourceMessage, classifyIngestError,
  type AnalyzeResult, type ColumnMatch, type DeclaredTable,
  type ElementHint, type SourceColumn,
} from '../domain/mapping'
import Stepper from '../components/common/Stepper.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { etlApi, type PreviewResult, type EtlFixDraft } from '../api/endpoints/etl'

const STEPS = ['上传文件', '列分析与映射', '质量预览与处置', '确认导入', '完成']

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
/** SQLite 选中的库内表（空串=后端默认首表） */
const sqliteTable = ref('')
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

/** 上传列画像（analyze columns[]）按列名索引，供映射表按“映射到的源列”展示画像 */
const columnByName = computed<Map<string, SourceColumn>>(() => {
  const m = new Map<string, SourceColumn>()
  for (const c of analysis.value?.columns ?? []) m.set(c.name, c)
  return m
})

/** 数据元推荐（analyze element_hints[]）按源列名索引 */
const hintBySourceCol = computed<Map<string, ElementHint>>(() => {
  const m = new Map<string, ElementHint>()
  for (const h of analysis.value?.element_hints ?? []) m.set(h.col, h)
  return m
})

/** Step1 已采纳的数据元推荐（声明列 → hint）。本地态：仅向导内汇总展示，
 * 真正回写 bindings.data_element 属后续批次（红线：推荐不自动生效，§6 决策项 6）。 */
const adoptedElements = ref<Record<string, ElementHint>>({})

// P4: 三分类处置常量
const CLASS_A_OPS = new Set(['despace', 'strip_thousands', 'strip_currency',
  'cn_date_norm', 'pad_date', 'to_upper', 'to_lower'])
const CLASS_B_OPS = new Set(['digits_only', 'strip_cc', 'strip_paren',
  'reject_if', 'trim_prefix', 'trim_suffix', 'regex_extract', 'unit_convert'])

/** ETL 处置草稿列表（本会话已创建的 etl_fix_draft） */
const drafts = ref<EtlFixDraft[]>([])
/** B 类预演结果 */
const previewResult = ref<PreviewResult | null>(null)
const previewingProp = ref('')
const previewBusy = ref(false)
const draftBusy = ref(false)

/** 该声明列当前映射到的上传源列名（无则 undefined） */
function sourceColOf(prop: string): string | undefined {
  const v = mapping.value[prop]
  return v && v.trim() ? v : undefined
}

/** 样本值（前 2 个，逗号分隔）——画像随“映射到的源列” */
function sampleOf(prop: string): string {
  const sc = sourceColOf(prop)
  if (!sc) return '—'
  const col = columnByName.value.get(sc)
  const ss = col?.samples ?? []
  return ss.length ? ss.slice(0, 2).join('、') : '—'
}

/** 空值率（%） */
function nullRateOf(prop: string): string {
  const sc = sourceColOf(prop)
  if (!sc) return '—'
  const r = columnByName.value.get(sc)?.null_rate
  return typeof r === 'number' ? `${Math.round(r * 100)}%` : '—'
}

/** 推断类型 */
function inferTypeOf(prop: string): string {
  const sc = sourceColOf(prop)
  if (!sc) return '—'
  return columnByName.value.get(sc)?.inferred_type ?? '—'
}

/** 该声明列对应源列的数据元推荐（element_hints 按源列索引） */
function elementHintOf(prop: string): ElementHint | undefined {
  const sc = sourceColOf(prop)
  if (!sc) return undefined
  return hintBySourceCol.value.get(sc)
}

/** 采纳数据元推荐（本地态标记，按钮切“已采纳”） */
function adoptElement(prop: string): void {
  const h = elementHintOf(prop)
  if (h) adoptedElements.value[prop] = h
}

/** 已采纳数据元列表（确认页汇总） */
const adoptedList = computed(() =>
  Object.entries(adoptedElements.value).map(([prop, h]) => ({
    prop, ...h,
  })),
)

// ---- P3-1: Step 1 质量预览（纯前端计算，消费已有 analyze 画像数据）----
interface QualityIssue {
  severity: 'warn' | 'info'
  prop: string
  message: string
}

const HIGH_NULL_THRESHOLD = 0.5
const LOW_CONF_THRESHOLD = 0.70
const DELIMITER_RE = /[-,，/|]/

/** 数据元预检违规码 → 中文标签（与 core/compliance.py 违规码同源） */
const PRECHECK_LABELS: Record<string, string> = {
  format_mismatch: '格式不符',
  checksum_failed: '校验位不符',
  range_violation: '超出值域',
  enum_unknown: '超出枚举',
}

/** 质量问题清单（基于 analyze 画像 + 映射状态，导入前预检） */
const qualityIssues = computed<QualityIssue[]>(() => {
  const issues: QualityIssue[] = []
  if (!analysis.value) return issues
  // 1. 高空值率（已映射列 null_rate > 50%）
  for (const col of declaredColumns.value) {
    const sc = sourceColOf(col.name)
    if (!sc) continue
    const c = columnByName.value.get(sc)
    if (c && typeof c.null_rate === 'number' && c.null_rate > HIGH_NULL_THRESHOLD) {
      issues.push({
        severity: 'warn', prop: col.name,
        message: `空值率 ${Math.round(c.null_rate * 100)}%（源列 ${sc}）`,
      })
    }
  }
  // 2. 低置信匹配（< 70%）
  for (const m of analysis.value.suggestion.matches ?? []) {
    if (m.confidence < LOW_CONF_THRESHOLD && mapping.value[m.target_prop]) {
      issues.push({
        severity: 'info', prop: m.target_prop,
        message: `匹配置信度 ${Math.round(m.confidence * 100)}%（${MATCH_META[m.match_type]?.label ?? m.match_type}）`,
      })
    }
  }
  // 3. 潜在复合列（样本值含分隔符，可能需拆分）
  for (const col of declaredColumns.value) {
    const sc = sourceColOf(col.name)
    if (!sc) continue
    const c = columnByName.value.get(sc)
    if (c?.samples?.some(s => DELIMITER_RE.test(s))) {
      issues.push({
        severity: 'info', prop: col.name,
        message: `样本含分隔符（源列 ${sc}），可能为复合列`,
      })
    }
  }
  // 4. 数据元合规预检（analyze 样本级 format/checksum/range/enum 违规）
  for (const col of declaredColumns.value) {
    const sc = sourceColOf(col.name)
    if (!sc) continue
    const h = elementHintOf(col.name)
    if (!h?.precheck) continue
    for (const [code, count] of Object.entries(h.precheck.violations)) {
      issues.push({
        severity: 'warn', prop: col.name,
        message: `${PRECHECK_LABELS[code] ?? code} ${count}/${h.precheck.checked}`
          + `（${h.element_id}，源列 ${sc}）`,
      })
    }
  }
  return issues
})

/** 质量摘要统计 */
const qualityStats = computed(() => ({
  total: declaredColumns.value.length,
  mapped: mappedCount(mapping.value),
  unmappedRequired: unmappedRequired.value.length,
  warnings: qualityIssues.value.filter(i => i.severity === 'warn').length,
  infos: qualityIssues.value.filter(i => i.severity === 'info').length,
}))

/** 三分类：依据 op 名判定 A/B/C */
function classifyOp(opName: string): 'A' | 'B' | 'C' {
  if (CLASS_A_OPS.has(opName)) return 'A'
  if (CLASS_B_OPS.has(opName)) return 'B'
  return 'C'
}

/** 处置项：已映射列 + 已采纳数据元 clean_rule 派生的 op 处置 */
interface DisposalItem {
  prop: string
  sourceCol: string
  elementId: string
  elementName?: string
  opToken: string
  opName: string
  opClass: 'A' | 'B' | 'C'
  reason: string
}

const disposalItems = computed<DisposalItem[]>(() => {
  const items: DisposalItem[] = []
  for (const col of declaredColumns.value) {
    const h = elementHintOf(col.name)
    if (!h || !h.clean_rule || h.clean_rule.length === 0) continue
    const sc = sourceColOf(col.name)
    if (!sc) continue
    for (const opToken of h.clean_rule) {
      const opName = opToken.split(':')[0]
      const cls = classifyOp(opName)
      items.push({
        prop: col.name, sourceCol: sc,
        elementId: h.element_id, elementName: h.element_name,
        opToken, opName, opClass: cls,
        reason: cls === 'A'
          ? `无损归一（${opName}）`
          : cls === 'B'
            ? `可能丢信息（${opName}）`
            : `建议处置（${opName}）`,
      })
    }
  }
  return items
})

/** 当前选中目标表的 object 名（草稿 target_object 用） */
const targetObject = computed(() =>
  analysis.value?.declared_tables.find(t => t.name === targetTable.value)?.object ?? '')

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

/** SQLite 库内表选项（多表时才需选择；单表默认首表） */
const sqliteTables = computed(() => upload.value?.sqlite_tables ?? [])
const showSqlitePicker = computed(
  () => upload.value?.format === 'sqlite' && sqliteTables.value.length > 1,
)
const sqliteTableOptions = computed(() =>
  sqliteTables.value.map((t) => ({
    label: `${t.name}（${t.rows} 行 / ${t.columns.length} 列）`,
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
    // SQLite：默认读首表（与后端缺省一致）；多表时用户可在向导切换
    const tbls = upload.value.sqlite_tables ?? []
    sqliteTable.value = tbls.length ? tbls[0].name : ''
    if (upload.value.warning) {
      // 塌缩预警（嵌套 JSON 未展平/分隔符不匹配）：仍进入向导，但显著提示
      message.warning(`解析预警：${upload.value.warning}`)
    } else {
      message.success(`上传成功：${f.name}（${upload.value.rows} 行），正在分析列…`)
    }
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
    analysis.value = await sourcesApi.analyze(
      cs.currentCaseId, uploadId, table, sqliteTable.value || undefined,
    )
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
  adoptedElements.value = {}
  if (upload.value) await runAnalyze(upload.value.upload_id, name)
}

/** 切换 SQLite 库内表：重置目标表/映射，按新表重新分析 */
async function onSqliteTableChange(name: string): Promise<void> {
  sqliteTable.value = name
  targetTable.value = ''
  mapping.value = {}
  adoptedElements.value = {}
  if (upload.value) await runAnalyze(upload.value.upload_id)
}

function matchOf(prop: string): ColumnMatch | undefined {
  return matchByProp.value.get(prop)
}

function goConfirm(): void {
  if (!targetTable.value) {
    message.warning('请先选择目标表')
    return
  }
  refreshDrafts()
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
      sqlite_table: sqliteTable.value || undefined,
    })
    message.success('导入任务已提交，正在后台运行（可到任务中心查看实时进度）')
    step.value = 4
  } catch (err) {
    if (isApiError(err) && err.code === 'CONFLICT') {
      duplicated.value = true
      message.warning(duplicateSourceMessage(err.message))
      step.value = 4
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

/** A 类一键应用：创建草稿 → 自动确认 */
async function applyClassA(item: DisposalItem): Promise<void> {
  if (!cs.currentCaseId || !upload.value || !targetObject.value) return
  draftBusy.value = true
  try {
    const created = await etlApi.createDraft(cs.currentCaseId, {
      upload_id: upload.value.upload_id,
      target_object: targetObject.value,
      target_prop: item.prop,
      op_token: item.opToken,
      op_class: 'A',
    })
    const confirmed = await etlApi.confirmDraft(cs.currentCaseId, created.draft_id)
    drafts.value = [confirmed, ...drafts.value.filter(d => d.draft_id !== confirmed.draft_id)]
    message.success(`已应用 A 类处置：${item.opToken}（${item.prop}）`)
  } catch (err) {
    message.error(`A 类处置失败：${presentError(err).title}`)
  } finally {
    draftBusy.value = false
  }
}

/** B 类预演：调用 previewOp 展示 affected_rows + samples */
async function previewClassB(item: DisposalItem): Promise<void> {
  if (!cs.currentCaseId || !upload.value) return
  previewBusy.value = true
  previewingProp.value = item.prop
  try {
    previewResult.value = await etlApi.previewOp(
      cs.currentCaseId, upload.value.upload_id,
      item.sourceCol, item.opToken, sqliteTable.value || undefined,
    )
  } catch (err) {
    message.error(`预演失败：${presentError(err).title}`)
    previewResult.value = null
  } finally {
    previewBusy.value = false
  }
}

/** B 类确认创建草稿（待复核） */
async function confirmClassB(item: DisposalItem): Promise<void> {
  if (!cs.currentCaseId || !upload.value || !previewResult.value || !targetObject.value) return
  draftBusy.value = true
  try {
    const created = await etlApi.createDraft(cs.currentCaseId, {
      upload_id: upload.value.upload_id,
      target_object: targetObject.value,
      target_prop: item.prop,
      op_token: item.opToken,
      op_class: 'B',
      preview_affected_rows: previewResult.value.affected_rows,
      preview_samples: previewResult.value.samples,
    })
    drafts.value = [created, ...drafts.value.filter(d => d.draft_id !== created.draft_id)]
    message.success(`已创建 B 类草稿（待复核）：${item.opToken}（${item.prop}，影响 ${previewResult.value.affected_rows} 行）`)
    previewResult.value = null
    previewingProp.value = ''
  } catch (err) {
    message.error(`B 类草稿创建失败：${presentError(err).title}`)
  } finally {
    draftBusy.value = false
  }
}

function cancelPreview(): void {
  previewResult.value = null
  previewingProp.value = ''
}

async function refreshDrafts(): Promise<void> {
  if (!cs.currentCaseId || !upload.value) return
  try {
    const res = await etlApi.listDrafts(cs.currentCaseId, upload.value.upload_id)
    drafts.value = res.items
  } catch (err) {
    message.error(`草稿列表加载失败：${presentError(err).title}`)
  }
}

function draftStatusTone(s: string): 'success' | 'info' | 'error' | 'warning' {
  if (s === '已发布') return 'success'
  if (s === '已确认') return 'info'
  if (s === '已驳回') return 'error'
  return 'warning'
}

function reset(): void {
  step.value = 0
  file.value = null
  upload.value = null
  analysis.value = null
  targetTable.value = ''
  mapping.value = {}
  adoptedElements.value = {}
  sqliteTable.value = ''
  importTask.value = null
  duplicated.value = false
  drafts.value = []
  previewResult.value = null
  previewingProp.value = ''
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
              <div class="field-row">
                <div v-if="showSqlitePicker" class="field">
                  <label>库内表（SQLite）</label>
                  <NSelect
                    :value="sqliteTable"
                    :options="sqliteTableOptions"
                    placeholder="选择 SQLite 库内要导入的表"
                    style="width: 280px"
                    @update:value="onSqliteTableChange"
                  />
                </div>
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
                  <tr>
                    <th>声明列（目标属性）</th>
                    <th>必选</th>
                    <th>匹配</th>
                    <th>样本值</th>
                    <th>空值率</th>
                    <th>推断类型</th>
                    <th>映射到上传列</th>
                    <th>数据元</th>
                  </tr>
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
                    <td class="dim">{{ sampleOf(col.name) }}</td>
                    <td>{{ nullRateOf(col.name) }}</td>
                    <td>{{ inferTypeOf(col.name) }}</td>
                    <td>
                      <NSelect
                        :value="mapping[col.name] ?? null"
                        :options="[{ label: '（不导入此列）', value: '' }, ...sourceOptions]"
                        placeholder="选择源列"
                        size="small"
                        style="width: 260px"
                        @update:value="(v: string) => { if (v) mapping[col.name] = v; else delete mapping[col.name]; delete adoptedElements[col.name] }"
                      />
                    </td>
                    <td>
                      <span v-if="elementHintOf(col.name)" class="de-cell">
                        <span class="de-name">{{ elementHintOf(col.name)!.element_name }}</span>
                        <span class="dim">（{{ Math.round(elementHintOf(col.name)!.confidence * 100) }}%）</span>
                        <NButton
                          size="tiny"
                          :type="adoptedElements[col.name] ? 'primary' : 'default'"
                          :disabled="!!adoptedElements[col.name]"
                          @click="adoptElement(col.name)"
                        >{{ adoptedElements[col.name] ? '已采纳' : '采纳' }}</NButton>
                      </span>
                      <span v-else class="dim">—</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- P3-1: 质量预览面板 -->
            <div v-if="qualityStats.total > 0" class="quality-panel">
              <div class="quality-head">
                <span class="quality-title">质量预览</span>
                <span class="quality-stats dim">
                  共 {{ qualityStats.total }} 列 · 已映射 {{ qualityStats.mapped }}
                  <template v-if="qualityStats.unmappedRequired"> · 缺必选 {{ qualityStats.unmappedRequired }}</template>
                  <template v-if="qualityStats.warnings"> · {{ qualityStats.warnings }} 项警告</template>
                  <template v-if="qualityStats.infos"> · {{ qualityStats.infos }} 项提示</template>
                </span>
              </div>
              <div v-if="qualityIssues.length" class="quality-list">
                <div
                  v-for="issue in qualityIssues"
                  :key="issue.prop + issue.message"
                  class="quality-item"
                  :class="issue.severity === 'warn' ? 'qi-warn' : 'qi-info'"
                >
                  <span class="qi-prop mono">{{ issue.prop }}</span>
                  <span class="qi-msg">{{ issue.message }}</span>
                </div>
              </div>
              <p v-else class="dim quality-ok">无质量警告</p>
            </div>

            <div class="step-actions">
              <NButton @click="step = 0">上一步</NButton>
              <NButton type="primary" :disabled="!targetTable" @click="goConfirm">
                下一步（已映射 {{ mappedCount(mapping) }} 列）
              </NButton>
            </div>
          </div>

          <!-- 步骤 2：质量预览与处置（P4 三分类） -->
          <div v-else-if="step === 2" class="step-body">
            <div class="row-between">
              <h3>质量预览与处置</h3>
              <NButton size="small" @click="refreshDrafts">刷新草稿</NButton>
            </div>

            <!-- 处置清单（A/B/C） -->
            <div v-if="disposalItems.length" class="disposal-list">
              <div v-for="item in disposalItems" :key="item.prop + item.opToken" class="disposal-item">
                <div class="di-head">
                  <NTag size="small" :type="item.opClass === 'A' ? 'success' : item.opClass === 'B' ? 'warning' : 'default'">
                    {{ item.opClass }} 类
                  </NTag>
                  <span class="mono">{{ item.prop }}</span>
                  <span class="dim">← {{ item.sourceCol }}</span>
                  <span class="mono">{{ item.opToken }}</span>
                  <span class="dim">{{ item.reason }}</span>
                </div>
                <div class="di-actions">
                  <NButton v-if="item.opClass === 'A'" size="tiny" type="primary" :loading="draftBusy"
                           @click="applyClassA(item)">应用</NButton>
                  <template v-else-if="item.opClass === 'B'">
                    <NButton size="tiny" :loading="previewBusy && previewingProp === item.prop"
                             @click="previewClassB(item)">预演</NButton>
                    <NButton v-if="previewResult && previewingProp === item.prop" size="tiny" type="warning"
                             :loading="draftBusy" @click="confirmClassB(item)">确认创建草稿</NButton>
                    <NButton v-if="previewResult && previewingProp === item.prop" size="tiny" quaternary
                             @click="cancelPreview">取消</NButton>
                  </template>
                  <span v-else class="dim">仅建议（不创建草稿）</span>
                </div>
                <div v-if="previewResult && previewingProp === item.prop" class="preview-box">
                  <div class="dim">影响行数：{{ previewResult.affected_rows }} / 总 {{ previewResult.total_rows }}</div>
                  <table class="m-table">
                    <thead><tr><th>前</th><th>后</th><th>剔除</th></tr></thead>
                    <tbody>
                      <tr v-for="(s, i) in previewResult.samples" :key="i">
                        <td class="mono">{{ s.before }}</td>
                        <td class="mono">{{ s.after }}</td>
                        <td>{{ s.rejected ? '✓' : '—' }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
            <EmptyState v-else type="empty" title="无可用处置"
                       desc="当前已映射列对应数据元无 clean_rule 声明，或尚未采纳数据元推荐" />

            <!-- 草稿列表 -->
            <div v-if="drafts.length" class="draft-list">
              <div class="quality-title">草稿列表（{{ drafts.length }}）</div>
              <table class="m-table">
                <thead>
                  <tr><th>属性</th><th>op</th><th>类</th><th>影响行</th><th>状态</th><th>创建</th></tr>
                </thead>
                <tbody>
                  <tr v-for="d in drafts" :key="d.draft_id">
                    <td class="mono">{{ d.target_prop }}</td>
                    <td class="mono">{{ d.op_token }}</td>
                    <td>{{ d.op_class }}</td>
                    <td>{{ d.preview_affected_rows }}</td>
                    <td>
                      <NTag size="small" :type="draftStatusTone(d.status)">{{ d.status }}</NTag>
                    </td>
                    <td class="dim">{{ d.created_by }} · {{ d.created_at }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div class="step-actions">
              <NButton @click="step = 1">上一步</NButton>
              <NButton type="primary" @click="step = 3">下一步（确认导入）</NButton>
            </div>
          </div>

          <!-- 步骤 3：确认导入 -->
          <div v-else-if="step === 3" class="step-body">
            <div class="confirm-grid">
              <div class="cf-item"><span class="dim">文件</span><b>{{ upload?.filename }}</b></div>
              <div class="cf-item"><span class="dim">格式</span><b>{{ upload?.format }}</b></div>
              <div v-if="upload?.format === 'sqlite'" class="cf-item"><span class="dim">库内表</span><b class="mono">{{ sqliteTable || '（首表）' }}</b></div>
              <div class="cf-item"><span class="dim">行数</span><b class="mono">{{ upload?.rows }}</b></div>
              <div class="cf-item"><span class="dim">目标表</span><b class="mono">{{ targetTable }}</b></div>
              <div class="cf-item"><span class="dim">已映射列</span><b class="mono">{{ mappedCount(mapping) }}</b></div>
              <div class="cf-item cf-fp"><span class="dim">指纹（文件名+SHA-256+行数）</span><b class="mono fp">{{ upload?.fingerprint }}</b></div>
            </div>
            <div v-if="unmappedRequired.length" class="warn-bar">
              ⚠ 仍有 {{ unmappedRequired.length }} 个必选列未映射：<span class="mono">{{ unmappedRequired.join('、') }}</span>。这些列将按空值策略降级处理，可继续导入。
            </div>
            <div v-if="adoptedList.length" class="de-summary">
              <div class="de-summary-title">已采纳数据元（{{ adoptedList.length }}）——BUILD 后生效</div>
              <div class="de-summary-list">
                <span v-for="a in adoptedList" :key="a.prop" class="de-chip mono">
                  {{ a.prop }} → {{ a.element_name }}（{{ a.element_id }}）
                </span>
              </div>
              <p class="dim hint">采纳记录为向导内决策；回写 bindings.data_element 属后续批次（红线：推荐不自动生效）。</p>
            </div>
            <div v-if="drafts.length" class="de-summary">
              <div class="de-summary-title">ETL 处置草稿（{{ drafts.length }}）——发布后才写 bindings（后续批次）</div>
              <div class="de-summary-list">
                <span v-for="d in drafts" :key="d.draft_id" class="de-chip mono">
                  {{ d.target_prop }} · {{ d.op_token }}（{{ d.op_class }}类·{{ d.status }}）
                </span>
              </div>
              <p class="dim hint">草稿状态在 state.sqlite；本批次不写 bindings.clean/source_sql，导入提交时草稿不影响实际清洗。</p>
            </div>
            <p class="dim hint">提交后立即返回任务并在后台运行（导入 → 清洗 → 链式构建语义层），可到任务中心查看实时进度。重复文件（指纹一致）将被拦截，不会产生重复数据。</p>
            <div class="step-actions">
              <NButton :disabled="busy" @click="step = 2">上一步</NButton>
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
.field-row { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
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
/* 数据元列 + 已采纳汇总 */
.de-cell { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.de-name { font-weight: 600; }
.de-summary {
  border: 1px dashed var(--sun-border); border-radius: 4px;
  padding: 8px 12px; margin: 10px 0; background: var(--sun-info-bg, var(--sun-ok-bg));
}
.de-summary-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
.de-summary-list { display: flex; flex-wrap: wrap; gap: 6px; }
.de-chip {
  font-size: 11px; padding: 2px 8px; border-radius: 10px;
  border: 1px solid var(--sun-info-border, var(--sun-ok-border));
  background: var(--sun-bg-card);
}
/* P3-1: 质量预览面板 */
.quality-panel {
  border: 1px solid var(--sun-border); border-radius: 4px;
  padding: 8px 12px; margin: 10px 0; background: var(--sun-bg-card);
}
.quality-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.quality-title { font-size: 12px; font-weight: 600; }
.quality-stats { font-size: 11px; }
.quality-list { display: flex; flex-direction: column; gap: 4px; }
.quality-item {
  display: flex; align-items: flex-start; gap: 8px;
  font-size: 11px; padding: 3px 6px; border-radius: 3px;
}
.qi-prop { font-weight: 600; white-space: nowrap; }
.qi-msg { color: var(--sun-text-secondary); }
.qi-warn {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
}
.qi-info {
  background: var(--sun-info-bg, var(--sun-ok-bg));
  border: 1px solid var(--sun-info-border, var(--sun-ok-border));
  color: var(--sun-info-text, var(--sun-ok-text));
}
.quality-ok { font-size: 11px; }
/* P4: 三分类处置清单 */
.disposal-list { display: flex; flex-direction: column; gap: 8px; margin: 10px 0; }
.disposal-item {
  border: 1px solid var(--sun-border); border-radius: 4px;
  padding: 8px 12px; background: var(--sun-bg-card);
}
.di-head { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.di-actions { margin-top: 6px; display: flex; gap: 6px; align-items: center; }
.preview-box {
  margin-top: 8px; padding: 6px 8px; border: 1px dashed var(--sun-border);
  border-radius: 4px; background: var(--sun-info-bg, var(--sun-ok-bg));
}
.draft-list { margin-top: 16px; }
</style>
