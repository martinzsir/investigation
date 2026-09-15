<script setup lang="ts">
// S3-F1/F2 数据元编辑器 + 三层视图（渐进替换整包 JSON 编辑；逃生舱 /c/escape 仍在）。
// 三层：全域 🌐 / 行业 🏭 / 本案件 📁（E3-1 无行业层只显示两层，不报错）；
// R1 已有项编码不可改（防对象属性 data_element 隐式断链）；
// R2 format 正则非法阻止保存（后端 loader re.compile 兜底）；D2 合法不命中允许保存；
// R3 编辑全域/行业层必须「影响所有案件」警示（本体管理员双条件门禁，后端 require_ontology_admin）；
// R5 界面未覆盖字段原样保留 + 提示；E1-3 删除被引用数据元警告并列出引用（不硬阻止）。
import { computed, ref, watch } from 'vue'
import {
  NButton, NCheckbox, NInput, NInputNumber, NSelect, NSpin, NTabPane, NTabs,
  NTag, NTooltip, useMessage,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  dataElementsApi, type DataElementEnums, type DataElementRefLocation,
  type DataElementsDoc, type IndustryDataElementsDoc,
} from '../api/endpoints/dataElements'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import {
  deNameError, diffOverride, formatRegexError, formToSpec, isMultiCleanRule,
  PRESET_SAMPLES, specToForm, testMatch, unknownFields, upsertElement,
  type DataElementSpec, type ElementForm, type FieldDiff,
} from '../domain/dataElementEdit'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

type Layer = 'shared' | 'industry' | 'case'
const LAYER_META: Record<Layer, { label: string; icon: string; file: string }> = {
  shared: { label: '全域', icon: '🌐', file: '_shared/data_elements.json' },
  industry: { label: '行业', icon: '🏭', file: '_industry/<行业>/data_elements.json' },
  case: { label: '本案件', icon: '📁', file: 'data_elements.json' },
}

const loading = ref(false)
const sharedDoc = ref<DataElementsDoc | null>(null)
const industryDoc = ref<IndustryDataElementsDoc | null>(null)
const caseDoc = ref<DataElementsDoc | null>(null)
const enums = ref<DataElementEnums | null>(null)
const refsByElement = ref<Record<string, DataElementRefLocation[]>>({})

const activeLayer = ref<Layer>('case')

// ---- 编辑态 ----
const selectedKey = ref<string | null>(null)
const isNew = ref(false)
const form = ref<ElementForm | null>(null)
const sample = ref('')

// ---- 保存/删除确认 ----
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
const confirmDetail = ref('')
const pendingIsDelete = ref(false)

const industry = computed(() => industryDoc.value?.industry ?? null)

const layerDocs = computed<Record<Layer, DataElementsDoc>>(() => ({
  shared: sharedDoc.value ?? { elements: {} },
  industry: industryDoc.value ?? { elements: {} },
  case: caseDoc.value ?? { elements: {} },
}))

const counts = computed<Record<Layer, number>>(() => ({
  shared: Object.keys(layerDocs.value.shared.elements).length,
  industry: Object.keys(layerDocs.value.industry.elements).length,
  case: Object.keys(layerDocs.value.case.elements).length,
}))

const layerRows = computed<Array<[string, DataElementSpec]>>(() =>
  Object.entries(layerDocs.value[activeLayer.value].elements))

/** 上层同名声明（industry 相对 shared；case 相对 industry+shared） */
function upperSpec(key: string): DataElementSpec | undefined {
  if (activeLayer.value === 'shared') return undefined
  if (activeLayer.value === 'industry') {
    return layerDocs.value.shared.elements[key]
  }
  return layerDocs.value.industry.elements[key] ?? layerDocs.value.shared.elements[key]
}

/** 本案件层覆盖标注（UC-S3-8）：active=shared/industry 时 case 是否同名覆盖 */
const caseOverrides = computed<Record<string, boolean>>(() => {
  const ce = layerDocs.value.case.elements
  const out: Record<string, boolean> = {}
  for (const k of Object.keys(ce)) {
    if (activeLayer.value !== 'case' && (layerDocs.value[activeLayer.value].elements[k] !== undefined)) {
      out[k] = true
    }
  }
  return out
})

/** 编辑中的覆盖差异（§8.2）：本条相对上层的字段差异 */
const overrideDiffs = computed<FieldDiff[]>(() => {
  if (!selectedKey.value || !form.value) return []
  const built = formToSpec(form.value, originalSpec.value)
  return diffOverride(upperSpec(selectedKey.value), built)
})

const originalSpec = computed<DataElementSpec | null>(() => {
  if (!selectedKey.value || isNew.value) return null
  return layerDocs.value[activeLayer.value].elements[selectedKey.value] ?? null
})

const builtSpec = computed<DataElementSpec | null>(() =>
  form.value ? formToSpec(form.value, originalSpec.value) : null)

const dirty = computed<boolean>(() => {
  if (!form.value) return false
  return JSON.stringify(builtSpec.value) !== JSON.stringify(originalSpec.value ?? {})
})

/** 层写权限（F2.3）：案件层 偏将+；全域/行业层 本体管理员 且 偏将+（后端双条件门禁） */
function canWriteLayer(layer: Layer): boolean {
  if (layer === 'case') return canWriteConfig(auth.clearance)
  return auth.isOntologyAdmin && auth.clearance >= 2
}

const canWriteActive = computed(() => canWriteLayer(activeLayer.value))

/** 试匹配（F1.2）：null=未输入/非法 */
const matchState = computed<boolean | null>(() =>
  form.value ? testMatch(form.value.format, sample.value) : null)
const formatError = computed<string>(() =>
  form.value ? formatRegexError(form.value.format) : '')

const unknownFieldNames = computed<string[]>(() => {
  if (!selectedKey.value || isNew.value || !originalSpec.value) return []
  return unknownFields(originalSpec.value)
})

const multiClean = computed<boolean>(() => Boolean(originalSpec.value && isMultiCleanRule(originalSpec.value)))

/** 新建编码与上层同名 → 需 override（v1.2 §3.0.7/P2-7 仅追加模式） */
const overrideNeeded = computed<boolean>(() =>
  isNew.value && Boolean(form.value && upperSpec(form.value.key.trim())))

const keyError = computed<string>(() => {
  if (!form.value) return ''
  const k = form.value.key.trim()
  const err = deNameError(k)
  if (err) return err
  if (isNew.value && layerDocs.value[activeLayer.value].elements[k] !== undefined) {
    return `编码 ${k} 在${LAYER_META[activeLayer.value].label}层已存在`
  }
  if (overrideNeeded.value && !form.value.override) {
    return '该编码已存在于上层，需勾选 override 才能覆盖（仅追加模式）'
  }
  return ''
})

const saveBlocked = computed<boolean>(() => Boolean(keyError.value || formatError.value))

// ---- 选项 ----
const typeOptions = computed(() =>
  (enums.value?.types ?? ['string', 'integer', 'decimal', 'date', 'boolean']).map((t) => ({ label: t, value: t })))
const withEmpty = (list: string[], emptyLabel = '未声明') =>
  [{ label: emptyLabel, value: '' }, ...list.map((v) => ({ label: v, value: v }))]
const checksumOptions = computed(() => withEmpty(enums.value?.checksums ?? []))
const cleanRuleOptions = computed(() => withEmpty(enums.value?.clean_rules ?? []))
const maskOptions = computed(() => withEmpty(enums.value?.masks ?? []))

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    sharedDoc.value = industryDoc.value = caseDoc.value = null
    return
  }
  loading.value = true
  const cid = cs.currentCaseId
  try {
    const [sh, ind, cse, en, refs] = await Promise.all([
      dataElementsApi.listShared(cid),
      dataElementsApi.listIndustry(cid),
      dataElementsApi.get(cid),
      dataElementsApi.enums(cid),
      dataElementsApi.references(cid),
    ])
    sharedDoc.value = sh
    industryDoc.value = ind
    caseDoc.value = cse
    enums.value = en
    refsByElement.value = refs.by_element ?? {}
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })
watch(activeLayer, () => {
  selectedKey.value = null
  form.value = null
  isNew.value = false
})

function selectElement(key: string): void {
  selectedKey.value = key
  isNew.value = false
  sample.value = ''
  form.value = specToForm(key, layerDocs.value[activeLayer.value].elements[key] ?? {})
}

function startNew(): void {
  selectedKey.value = null
  isNew.value = true
  sample.value = ''
  form.value = {
    key: 'DE_', name: '', type: 'string', length: null, format: '',
    checksum: '', sensitive: false, mask: '', cleanRule: '', override: false,
  }
}

function refsOf(key: string): DataElementRefLocation[] {
  return refsByElement.value[key] ?? []
}

/** 覆盖标注 tooltip（UC-S3-8）：上层值 → 本层值 */
function overrideTooltip(key: string): string {
  const own = layerDocs.value[activeLayer.value].elements[key]
  const diffs = diffOverride(upperSpec(key), own ?? {})
  if (!diffs.length) return '与上层声明一致'
  const src = activeLayer.value === 'case' ? '本案件' : LAYER_META[activeLayer.value].label
  return diffs.map((d) => `${d.field}：上层 ${d.upper} → ${src} ${d.lower}`).join('\n')
}

// ---- 保存 ----
function buildDoc(): DataElementsDoc {
  const doc = layerDocs.value[activeLayer.value]
  const elements = upsertElement(doc, (form.value as ElementForm).key.trim(), pendingIsDelete.value ? null : builtSpec.value)
  return { ...doc, elements }
}

function askSaveDelete(): void {
  if (!form.value) return
  const errs: string[] = []
  if (keyError.value) errs.push(keyError.value)
  if (formatError.value) errs.push(formatError.value)
  if (errs.length) {
    message.error(errs.join('；'))
    return
  }
  const layerLabel = LAYER_META[activeLayer.value].label
  if (activeLayer.value === 'shared' || activeLayer.value === 'industry') {
    confirmDetail.value = `⚠ ${layerLabel}层是标准层口径，改动将影响所有案件。`
  } else {
    confirmDetail.value = `${layerLabel}层 data_elements.json 变更（类型/长度/敏感/枚举口径），下次装载生效。`
  }
  if (pendingIsDelete.value) {
    const refs = selectedKey.value ? refsOf(selectedKey.value) : []
    if (refs.length) {
      const locs = refs.map((r) => `${r.object}.${r.property}`).join('、')
      confirmDetail.value += ` 🔴 该数据元仍被 ${refs.length} 处对象属性引用（${locs}），删除后装载将失败，确认继续？`
    } else {
      confirmDetail.value += ' 操作：删除该数据元。'
    }
  } else if (selectedKey.value) {
    confirmDetail.value += ` 操作：更新 ${selectedKey.value}。`
  } else {
    confirmDetail.value += ` 操作：新建 ${(form.value as ElementForm).key.trim()}。`
  }
  confirmReason.value = ''
  confirmOpen.value = true
}

function askSave(): void {
  pendingIsDelete.value = false
  askSaveDelete()
}

function askDelete(): void {
  pendingIsDelete.value = true
  askSaveDelete()
}

async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  const doc = buildDoc()
  const cid = cs.currentCaseId
  confirmSaving.value = true
  try {
    if (activeLayer.value === 'shared') {
      await dataElementsApi.saveShared(cid, doc, confirmReason.value)
    } else if (activeLayer.value === 'industry') {
      await dataElementsApi.saveIndustry(cid, doc, confirmReason.value)
    } else {
      await dataElementsApi.save(cid, doc, confirmReason.value)
    }
    message.success('数据元定义已保存并留痕；新口径在下次装载/RESCAN 生效')
    confirmOpen.value = false
    selectedKey.value = null
    form.value = null
    isNew.value = false
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>数据元</h2>
      <p class="dim hint">
        数据元是跨数据源的标准口径（类型/长度/敏感级/代码表枚举）；接入映射与 ETL 校验都以此为准。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="数据元按案件快照归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 数据元定义改变装载校验口径（如长度/枚举/类型），保存后下次装载生效并记入审计链。
      </div>

      <NSpin :show="loading">
        <!-- F2.1 三层切换（E3-1：无行业层只显示两层，不报错） -->
        <NTabs v-model:value="activeLayer" type="segment" size="small" class="layer-tabs">
          <NTabPane name="shared">
            <template #tab>🌐 全域 {{ counts.shared }}</template>
          </NTabPane>
          <NTabPane v-if="industry" name="industry">
            <template #tab>🏭 行业·{{ industry }} {{ counts.industry }}</template>
          </NTabPane>
          <NTabPane name="case">
            <template #tab>📁 本案件 {{ counts.case }}</template>
          </NTabPane>
        </NTabs>

        <!-- F2.3 只读提示（§8.4） -->
        <div v-if="!canWriteActive" class="hint-banner">
          🔒 {{ LAYER_META[activeLayer].label }}层数据元{{ activeLayer === 'case' ? '需偏将及以上' : '仅本体管理员（且职级 ≥ 偏将）' }}可修改。你正在以只读方式查看。
        </div>

        <div class="body-cols">
          <!-- 左：条目列表 -->
          <div class="list-col">
            <div class="list-head">
              <span class="dim">{{ LAYER_META[activeLayer].icon }} {{ LAYER_META[activeLayer].label }}层 · {{ layerRows.length }} 项</span>
              <NButton
                v-if="canWriteActive"
                type="primary" size="tiny" secondary
                :disabled="isNew"
                @click="startNew"
              >+ 新建</NButton>
            </div>
            <button
              v-for="[key, spec] in layerRows" :key="key"
              type="button" class="elem-item"
              :class="{ active: selectedKey === key && !isNew }"
              @click="selectElement(key)"
            >
              <span class="mono elem-key">{{ key }}</span>
              <span class="elem-name dim">{{ String(spec.name ?? '') }}</span>
              <NTooltip v-if="upperSpec(key) !== undefined" trigger="hover">
                <template #trigger>
                  <NTag size="tiny" type="warning" :bordered="false">覆盖上层</NTag>
                </template>
                <span style="white-space: pre-line">{{ overrideTooltip(key) }}</span>
              </NTooltip>
              <NTag v-if="caseOverrides[key]" size="tiny" :bordered="false">已被案件层覆盖</NTag>
            </button>
            <div v-if="!layerRows.length" class="dim empty-hint">该层暂无数据元声明</div>
          </div>

          <!-- 右：编辑区（F1.1 字段表单） -->
          <div class="editor-col">
            <EmptyState
              v-if="!form"
              type="empty"
              title="选择左侧数据元"
              desc="或点击「+ 新建」在当前层新增数据元；全域/行业层仅本体管理员可改。"
            />
            <template v-else>
              <div v-if="!canWriteActive" class="readonly-mask">
                <div class="hint-banner" style="margin-bottom: 8px">
                  🔒 只读查看：{{ LAYER_META[activeLayer].label }}层 {{ isNew ? '' : form.key }}
                </div>
              </div>

              <!-- §8.2 覆盖关系提示 -->
              <div v-if="overrideDiffs.length && !isNew" class="warn-banner">
                本数据元在<span class="mono">{{ activeLayer === 'case' ? '全域/行业层' : '上层' }}</span>已定义，{{ LAYER_META[activeLayer].label }}层已覆盖其
                {{ overrideDiffs.map((d) => d.field).join('、') }} 字段：
                {{ overrideDiffs.map((d) => `上层 ${d.upper} → ${LAYER_META[activeLayer].label} ${d.lower}`).join('；') }}
              </div>

              <div class="form-grid">
                <label class="field">
                  <span class="field-label">编码 {{ isNew ? '' : '🔒' }}</span>
                  <NInput
                    v-model:value="form.key"
                    size="small" class="mono"
                    :disabled="!isNew || !canWriteActive"
                    placeholder="DE_XXX"
                  />
                  <span v-if="keyError" class="field-error">{{ keyError }}</span>
                </label>
                <label class="field">
                  <span class="field-label">名称（显示名）</span>
                  <NInput v-model:value="form.name" size="small" :disabled="!canWriteActive" placeholder="如：公民身份号码" />
                </label>
                <label class="field">
                  <span class="field-label">类型</span>
                  <NSelect v-model:value="form.type" size="small" :options="typeOptions" :disabled="!canWriteActive" />
                </label>
                <label class="field">
                  <span class="field-label">长度</span>
                  <NInputNumber v-model:value="form.length" size="small" :min="1" :precision="0" :disabled="!canWriteActive" placeholder="正整数，可空" class="w-full" />
                </label>
                <label class="field field-wide">
                  <span class="field-label">format（正则）</span>
                  <NInput
                    v-model:value="form.format" size="small" class="mono"
                    :disabled="!canWriteActive"
                    :status="formatError ? 'error' : undefined"
                    placeholder="如 ^1[3-9]\d{9}$；留空 = 不校验"
                  />
                  <span v-if="formatError" class="field-error">{{ formatError }}</span>
                  <!-- F1.2 试匹配 -->
                  <div class="match-row">
                    <NInput v-model:value="sample" size="small" class="mono" placeholder="试匹配样例值" />
                    <div class="preset-chips">
                      <NTag
                        v-for="p in PRESET_SAMPLES" :key="p.label"
                        size="tiny" :bordered="false" class="preset-chip"
                        @click="sample = p.value"
                      >{{ p.label }}</NTag>
                    </div>
                    <span v-if="matchState === true" class="match-hit">✅ 命中</span>
                    <span v-else-if="matchState === false" class="field-warn">❌ 未命中 —— 请检查正则或样例值</span>
                    <span v-else class="dim match-idle">未输入</span>
                  </div>
                </label>
                <label class="field">
                  <span class="field-label">checksum（校验算法）</span>
                  <NSelect v-model:value="form.checksum" size="small" :options="checksumOptions" :disabled="!canWriteActive" />
                </label>
                <label class="field">
                  <span class="field-label">遮蔽 mask</span>
                  <NSelect v-model:value="form.mask" size="small" :options="maskOptions" :disabled="!canWriteActive" />
                </label>
                <label class="field">
                  <span class="field-label">clean_rule（清洗 op）</span>
                  <NSelect
                    v-model:value="form.cleanRule" size="small"
                    :options="cleanRuleOptions" :disabled="!canWriteActive || multiClean"
                  />
                  <span v-if="multiClean" class="field-warn">
                    多段清洗规则（{{ (originalSpec?.clean_rule as string[]).length }} 段）在表单中只读，保存时原样保留
                  </span>
                </label>
                <label class="field">
                  <span class="field-label">敏感</span>
                  <NCheckbox v-model:checked="form.sensitive" :disabled="!canWriteActive">敏感数据元（需遮蔽口径）</NCheckbox>
                </label>
                <label v-if="overrideNeeded || form.override" class="field">
                  <span class="field-label">覆盖声明</span>
                  <NCheckbox v-model:checked="form.override" :disabled="!canWriteActive">
                    override：显式覆盖上层同名数据元（记入审计）
                  </NCheckbox>
                </label>
              </div>

              <!-- R5/§8.6 未知字段保留提示 -->
              <div v-if="unknownFieldNames.length" class="hint-banner">
                本数据元有 {{ unknownFieldNames.length }} 个界面未覆盖字段（{{ unknownFieldNames.join('、') }}），已原样保留。
              </div>

              <div class="edit-actions">
                <NButton
                  v-if="!isNew && canWriteActive"
                  size="small" type="warning" ghost
                  @click="askDelete"
                >删除</NButton>
                <span class="spacer" />
                <NButton size="small" @click="form = null; selectedKey = null; isNew = false">取消</NButton>
                <NButton
                  type="primary" danger size="small"
                  :disabled="!canWriteActive || !dirty || saveBlocked"
                  @click="askSave"
                >{{ saveBlocked ? '校验未通过' : '校验并保存（危险变更）' }}</NButton>
              </div>
            </template>
          </div>
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="true"
      :detail="confirmDetail"
      :loading="confirmSaving"
      title="数据元变更确认"
      @confirm="doSave"
    />
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
.layer-tabs { max-width: 560px; }
.hint-banner {
  background: var(--sun-bg-card-hover); border: 1px dashed var(--sun-border);
  border-radius: 6px; padding: 8px 12px; font-size: 12px; color: var(--sun-text-secondary);
}
.warn-banner {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.body-cols { display: flex; gap: 12px; align-items: flex-start; }
.list-col {
  flex: 0 0 300px; display: flex; flex-direction: column; gap: 4px;
  border: 1px solid var(--sun-border); border-radius: 6px; background: var(--sun-bg-card);
  padding: 10px; max-height: 60vh; overflow: auto;
}
.list-head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding-bottom: 6px; }
.elem-item {
  display: flex; align-items: center; gap: 6px; width: 100%; text-align: left;
  border: none; background: transparent; font: inherit; color: inherit;
  border-radius: 6px; padding: 6px 8px; cursor: pointer;
}
.elem-item:hover { background: var(--sun-bg-card-hover); }
.elem-item.active { background: var(--sun-input-bg); outline: 1px solid var(--sun-border-active); }
.elem-key { font-size: 13px; font-weight: 500; }
.elem-name { flex: 1; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-hint { font-size: 12px; padding: 12px 6px; }
.editor-col {
  flex: 1; min-width: 0; border: 1px solid var(--sun-border); border-radius: 6px;
  background: var(--sun-bg-card); padding: 12px;
  display: flex; flex-direction: column; gap: 10px;
}
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field-wide { grid-column: 1 / -1; }
.field-label { font-size: 12px; color: var(--sun-text-secondary); }
.field-error { font-size: 12px; color: var(--sun-error-text); }
.field-warn { font-size: 12px; color: var(--sun-warn-text); }
.w-full { width: 100%; }
.match-row { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
.match-row > :first-child { flex: 0 0 240px; }
.preset-chips { display: flex; gap: 4px; }
.preset-chip { cursor: pointer; }
.match-hit { font-size: 12px; color: var(--sun-success-text, #2e7d32); }
.match-idle { font-size: 12px; }
.readonly-mask { font-size: 12px; }
.edit-actions { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
