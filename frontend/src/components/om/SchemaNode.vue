<script setup lang="ts">
// S5-F2 通用 schema 驱动表单的递归渲染节点（draft-07 子集，配合 domain/schemaForm）。
// 渲染分支：const/enum 选择、标量、标量数组、对象数组（折叠卡片）、固定属性对象、
// 映射型对象（additionalProperties: schema）、oneOf 文本/列表切换、未知结构 JSON 兜底。
// 组件可按文件名自递归（SFC 递归引用）。
import { computed, ref } from 'vue'
import {
  NButton,
  NCollapse,
  NCollapseItem,
  NInput,
  NInputNumber,
  NSelect,
  NSwitch,
  NTag,
} from 'naive-ui'
import type { JsonSchema } from '../../api/endpoints/ontologyGeneric'
import { buildDefault, coerceScalar, isLongText } from '../../domain/schemaForm'

const props = withDefaults(
  defineProps<{
    schema: JsonSchema
    modelValue: unknown
    fieldKey?: string
    requiredMark?: boolean
    disabled?: boolean
    depth?: number
  }>(),
  { fieldKey: '', requiredMark: false, disabled: false, depth: 0 },
)
const emit = defineEmits<{ 'update:modelValue': [v: unknown] }>()

function emitPatch(v: unknown): void {
  emit('update:modelValue', v)
}

const types = computed<string[]>(() => {
  const t = props.schema.type
  if (!t) return []
  return Array.isArray(t) ? t : [t]
})

const isConst = computed(() => Object.prototype.hasOwnProperty.call(props.schema, 'const'))
const enumOptions = computed<{ label: string; value: string | number }[]>(() =>
  (props.schema.enum ?? []).map((v) => ({
    label: String(v),
    value: (typeof v === 'number' ? v : String(v)) as string | number,
  })),
)

const isBoolean = computed(() => types.value.includes('boolean'))
const isNumber = computed(() =>
  types.value.some((t) => t === 'integer' || t === 'number'),
)
const isString = computed(() => types.value.includes('string'))
// 联合类型且包含 string（如 range.min: number|string）：文本输入 + 提交前 coerce
const isUnionScalar = computed(() => types.value.length > 1 && isString.value)

const longText = computed(() => isLongText(props.fieldKey, props.schema))

// ---- 数组 ----
const isArray = computed(() => types.value.includes('array'))
const itemSchema = computed<JsonSchema | null>(() =>
  props.schema.items ? (props.schema.items as JsonSchema) : null,
)
const itemsAreObject = computed(
  () => itemSchema.value?.type === 'object' || Boolean(itemSchema.value?.properties),
)

function asArray(): unknown[] {
  return Array.isArray(props.modelValue) ? props.modelValue : []
}
function setIndex(i: number, v: unknown): void {
  const next = [...asArray()]
  next[i] = v
  emitPatch(next)
}
function addItem(): void {
  emitPatch([...asArray(), itemSchema.value ? buildDefault(itemSchema.value) : ''])
}
function removeItem(i: number): void {
  const next = asArray().filter((_, idx) => idx !== i)
  emitPatch(next)
}

// ---- 固定属性对象 ----
const propEntries = computed<[string, JsonSchema][]>(() =>
  Object.entries(props.schema.properties ?? {}),
)
const requiredSet = computed(() => new Set(props.schema.required ?? []))
function asObject(): Record<string, unknown> {
  return props.modelValue && typeof props.modelValue === 'object' && !Array.isArray(props.modelValue)
    ? (props.modelValue as Record<string, unknown>)
    : {}
}
function setProp(key: string, v: unknown): void {
  emitPatch({ ...asObject(), [key]: v })
}
function initObject(): void {
  emitPatch(buildDefault(props.schema))
}

// ---- 映射型对象（additionalProperties 为 schema）----
const mapValueSchema = computed<JsonSchema | null>(() =>
  props.schema.additionalProperties &&
  typeof props.schema.additionalProperties === 'object'
    ? (props.schema.additionalProperties as JsonSchema)
    : null,
)
const isMap = computed(
  () => !isArray.value && types.value.includes('object') &&
    mapValueSchema.value !== null && propEntries.value.length === 0,
)
const mapEntries = computed<[string, unknown][]>(() => Object.entries(asObject()))
function addMapEntry(): void {
  const next = { ...asObject() }
  let i = mapEntries.value.length + 1
  while (Object.prototype.hasOwnProperty.call(next, `new_key_${i}`)) i += 1
  next[`new_key_${i}`] = mapValueSchema.value ? buildDefault(mapValueSchema.value) : ''
  emitPatch(next)
}
function setMapKey(oldKey: string, newKey: string): void {
  const next: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(asObject())) {
    if (k === oldKey) next[newKey] = v
    else next[k] = v
  }
  emitPatch(next)
}
function setMapValue(key: string, v: unknown): void {
  emitPatch({ ...asObject(), [key]: v })
}
function removeMapKey(key: string): void {
  const next = { ...asObject() }
  delete next[key]
  emitPatch(next)
}

// ---- 固定属性对象（非映射、非数组）----
const isFixedObject = computed(
  () => !isArray.value && !isMap.value && types.value.includes('object') &&
    propEntries.value.length > 0,
)

// ---- oneOf 文本/列表（match.assumption: string | string[]）----
const oneOfSubs = computed(() => props.schema.oneOf ?? [])
const stringListOneOf = computed(
  () =>
    oneOfSubs.value.length === 2 &&
    oneOfSubs.value.some((s) => s.type === 'string') &&
    oneOfSubs.value.some((s) => s.type === 'array' && (s.items as JsonSchema)?.type === 'string'),
)
function isListMode(): boolean {
  return Array.isArray(props.modelValue)
}
function switchMode(toList: boolean): void {
  if (toList) emitPatch(props.modelValue === '' || props.modelValue == null ? [] : [String(props.modelValue)])
  else emitPatch(asArray().join('；'))
}

// ---- 原始 JSON 兜底（schema 未结构化描述的对象）----
const rawText = ref('')
const rawError = ref('')
const editingRaw = computed(
  () =>
    !isArray.value &&
    !isFixedObject.value &&
    !isMap.value &&
    !isBoolean.value &&
    !isNumber.value &&
    !isString.value &&
    !isConst.value &&
    enumOptions.value.length === 0 &&
    oneOfSubs.value.length === 0,
)
function syncRaw(): void {
  try {
    rawText.value = JSON.stringify(props.modelValue, null, 2)
    rawError.value = ''
  } catch {
    rawText.value = ''
    rawError.value = '当前值无法序列化为 JSON'
  }
}
syncRaw()
function commitRaw(): void {
  try {
    emitPatch(JSON.parse(rawText.value))
    rawError.value = ''
  } catch (e) {
    rawError.value = `JSON 解析失败：${(e as Error).message}`
  }
}
</script>

<template>
  <div class="sn-node">
    <!-- 常量（schema_version 等）：只展示不可改 -->
    <NInput v-if="isConst" :value="String(modelValue ?? schema.const)" disabled size="small" />

    <!-- 枚举 -->
    <NSelect
      v-else-if="enumOptions.length"
      :value="(modelValue as string | number | null | undefined) ?? null"
      :options="enumOptions"
      :disabled="disabled"
      size="small"
      @update:value="emitPatch"
    />

    <!-- oneOf 文本/列表 -->
    <div v-else-if="stringListOneOf" class="sn-full">
      <div class="sn-mode-row">
        <NButton size="tiny" :type="!isListMode() ? 'primary' : 'default'" @click="switchMode(false)">文本</NButton>
        <NButton size="tiny" :type="isListMode() ? 'primary' : 'default'" @click="switchMode(true)">列表</NButton>
      </div>
      <NInput
        v-if="!isListMode()"
        :value="String(modelValue ?? '')"
        :disabled="disabled"
        size="small"
        @update:value="emitPatch"
      />
      <div v-else class="sn-list">
        <div v-for="(el, i) in asArray()" :key="i" class="sn-row">
          <NInput :value="String(el)" :disabled="disabled" size="small"
                  @update:value="(v: string) => setIndex(i, v)" />
          <NButton size="tiny" type="error" quaternary :disabled="disabled" @click="removeItem(i)">✕</NButton>
        </div>
        <NButton size="tiny" dashed :disabled="disabled" @click="addItem">＋ 添加</NButton>
      </div>
    </div>

    <!-- 布尔 -->
    <NSwitch v-else-if="isBoolean" :value="Boolean(modelValue)" :disabled="disabled"
             @update:value="emitPatch" />

    <!-- 数字/联合标量 -->
    <NInputNumber
      v-else-if="isNumber && !isUnionScalar"
      :value="modelValue === null || modelValue === undefined ? null : Number(modelValue)"
      :disabled="disabled"
      size="small"
      show-button
      @update:value="emitPatch"
    />
    <NInput
      v-else-if="isUnionScalar"
      :value="modelValue === null || modelValue === undefined ? '' : String(modelValue)"
      :disabled="disabled"
      size="small"
      @update:value="(v: string) => emitPatch(coerceScalar(v, schema))"
    />

    <!-- 字符串 -->
    <NInput
      v-else-if="isString"
      :value="String(modelValue ?? '')"
      :disabled="disabled"
      size="small"
      :type="longText ? 'textarea' : 'text'"
      :autosize="longText ? { minRows: 2, maxRows: 8 } : undefined"
      @update:value="emitPatch"
    />

    <!-- 标量/对象数组 -->
    <div v-else-if="isArray" class="sn-full sn-list">
      <template v-if="itemsAreObject">
        <NCollapse v-if="asArray().length" :default-expanded-names="[]">
          <NCollapseItem
            v-for="(el, i) in asArray()"
            :key="i"
            :name="i"
            :title="`#${i + 1}`"
          >
            <SchemaNode
              :schema="itemSchema as JsonSchema"
              :model-value="el"
              :depth="depth + 1"
              :disabled="disabled"
              @update:model-value="(v: unknown) => setIndex(i, v)"
            />
            <template #header-extra>
              <NButton size="tiny" type="error" quaternary :disabled="disabled"
                       @click.stop="removeItem(i)">删除</NButton>
            </template>
          </NCollapseItem>
        </NCollapse>
      </template>
      <template v-else>
        <div v-for="(el, i) in asArray()" :key="i" class="sn-row">
          <SchemaNode
            :schema="(itemSchema ?? { type: 'string' }) as JsonSchema"
            :model-value="el"
            :depth="depth + 1"
            :disabled="disabled"
            @update:model-value="(v: unknown) => setIndex(i, v)"
          />
          <NButton size="tiny" type="error" quaternary :disabled="disabled" @click="removeItem(i)">✕</NButton>
        </div>
      </template>
      <NButton size="tiny" dashed :disabled="disabled" @click="addItem">＋ 添加一项</NButton>
    </div>

    <!-- 映射型对象 -->
    <div v-else-if="isMap" class="sn-full sn-map">
      <div v-for="[k, v] in mapEntries" :key="k" class="sn-map-row">
        <NInput :value="k" size="small" class="sn-map-key" :disabled="disabled"
                @update:value="(nk: string) => setMapKey(k, nk)" />
        <SchemaNode
          :schema="mapValueSchema as JsonSchema"
          :model-value="v"
          :depth="depth + 1"
          :disabled="disabled"
          @update:model-value="(nv: unknown) => setMapValue(k, nv)"
        />
        <NButton size="tiny" type="error" quaternary :disabled="disabled" @click="removeMapKey(k)">✕</NButton>
      </div>
      <NButton size="tiny" dashed :disabled="disabled" @click="addMapEntry">＋ 添加键</NButton>
    </div>

    <!-- 固定属性对象 -->
    <div v-else-if="isFixedObject" class="sn-full sn-object" :class="{ 'sn-nested': depth > 0 }">
      <template v-if="Object.keys(asObject()).length">
        <div v-for="[key, sub] in propEntries" :key="key" class="sn-field">
          <div class="sn-label">
            <span class="mono">{{ key }}</span>
            <NTag v-if="requiredSet.has(key)" size="tiny" type="error" :bordered="false">必填</NTag>
          </div>
          <div class="sn-control">
            <SchemaNode
              :schema="sub"
              :model-value="asObject()[key]"
              :field-key="key"
              :depth="depth + 1"
              :disabled="disabled"
              @update:model-value="(v: unknown) => setProp(key, v)"
            />
          </div>
        </div>
      </template>
      <NButton v-else size="tiny" dashed :disabled="disabled" @click="initObject">初始化该对象</NButton>
    </div>

    <!-- 兜底：原始 JSON -->
    <div v-else-if="editingRaw" class="sn-full">
      <NInput
        v-model:value="rawText"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 10 }"
        size="small"
        :disabled="disabled"
        :status="rawError ? 'error' : undefined"
        @blur="commitRaw"
      />
      <div v-if="rawError" class="sn-raw-err">{{ rawError }}</div>
    </div>
  </div>
</template>

<style scoped>
.sn-node {
  min-width: 0;
  width: 100%;
}
.sn-full {
  width: 100%;
}
.sn-field {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: 8px;
  align-items: start;
  padding: 4px 0;
}
.sn-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  padding-top: 4px;
  word-break: break-all;
}
.sn-control {
  min-width: 0;
}
.sn-object.sn-nested {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 6px 10px;
  background: var(--sun-bg-card-hover);
}
.sn-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.sn-mode-row {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}
.sn-map-row {
  display: grid;
  grid-template-columns: 160px 1fr auto;
  gap: 6px;
  align-items: start;
  margin-bottom: 6px;
}
.sn-map-key {
  font-family: var(--sun-font-mono);
}
.sn-list,
.sn-map {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.sn-raw-err {
  color: var(--sun-error-text);
  font-size: 11px;
  margin-top: 2px;
}
.mono {
  font-family: var(--sun-font-mono);
}
</style>
