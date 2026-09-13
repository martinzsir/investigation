<script setup lang="ts">
// M4 RC-204 扩展查询弹窗：白名单只读 Function 的业务化参数表单。
// - 仅展示 GET .../canvas/functions 返回的白名单函数（非白名单前端不可达）；
// - integer/decimal 数字输入、boolean 开关、string 仅 enum 下拉（无 enum 不允许自由文本）；
// - 不暴露 sql/impl 等技术细节；
// - DATASOURCE_UNAVAILABLE / DEGRADED 为 200 executed=false，弹窗内告警、不落节点。
// 提交内容上抛 ResearchCanvas，请求与版本处理在父组件。
import { computed, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NInputNumber,
  NModal,
  NSelect,
  NSwitch,
  NTag,
} from 'naive-ui'
import {
  validateFunctionParams,
  type FunctionForm,
  type FunctionQuerySkippedEnvelope,
} from '../../domain/canvas'

const props = defineProps<{
  show: boolean
  /** 白名单函数目录（available=false 时为空） */
  functions: FunctionForm[]
  /** 案件 ontology 是否装载成功 */
  available: boolean
  /** 「查询自」来源节点标签（仅展示；null=不挂载来源） */
  sourceNodeLabel?: string | null
  /** 请求进行中（父组件置位） */
  busy?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (
    e: 'submit',
    payload: { name: string; params: Record<string, unknown> },
  ): void
}>()

const selectedName = ref<string | null>(null)
const values = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})
/** executed=false 告警（数据源未接入/降级） */
const skipAlert = ref<FunctionQuerySkippedEnvelope | null>(null)

const selectedForm = computed<FunctionForm | null>(
  () => props.functions.find((f) => f.name === selectedName.value) ?? null,
)

function initValues(form: FunctionForm | null): void {
  const next: Record<string, unknown> = {}
  if (form) {
    for (const p of form.params) {
      if (p.default !== null && p.default !== undefined) {
        next[p.key] = p.default
      }
    }
  }
  values.value = next
}

const functionOptions = computed(() =>
  props.functions.map((f) => ({ label: f.title, value: f.name })),
)

function onSelect(name: string): void {
  selectedName.value = name
  errors.value = {}
  skipAlert.value = null
  initValues(props.functions.find((f) => f.name === name) ?? null)
}

function setNum(key: string, v: number | null): void {
  values.value = { ...values.value, [key]: v }
}
function setBool(key: string, v: boolean): void {
  values.value = { ...values.value, [key]: v }
}
function setEnum(key: string, v: string | null): void {
  values.value = { ...values.value, [key]: v }
}

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    errors.value = {}
    skipAlert.value = null
    if (!selectedForm.value && props.functions.length) {
      onSelect(props.functions[0].name)
    } else {
      initValues(selectedForm.value)
    }
  },
  { immediate: true },
)

function onSubmit(): void {
  const form = selectedForm.value
  if (!form) return
  const errs = validateFunctionParams(form, values.value)
  errors.value = errs
  if (Object.keys(errs).length > 0) return
  const params: Record<string, unknown> = {}
  for (const p of form.params) {
    const v = values.value[p.key]
    if (v === null || v === undefined) continue
    if (typeof v === 'string' && v.trim() === '') continue
    params[p.key] = v
  }
  skipAlert.value = null
  emit('submit', { name: form.name, params })
}

defineExpose({
  /** 父组件请求失败（非 executed=false 的 ApiError）后解锁 */
  unlock: () => {
    /* busy 由 props 驱动，保留钩子与 M3 弹窗一致 */
  },
  /** executed=false：数据源未接入/降级，告警且不落节点 */
  showSkip: (env: FunctionQuerySkippedEnvelope) => {
    skipAlert.value = env
  },
})
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    title="扩展查询（只读 Function）"
    class="function-query-modal"
    :style="{ width: '560px' }"
    :mask-closable="false"
    data-testid="function-query-modal"
    @update:show="emit('update:show', $event)"
  >
    <div class="form">
      <NAlert
        v-if="!available"
        type="warning"
        :show-icon="false"
        :bordered="false"
        data-testid="function-catalog-unavailable"
      >
        案件规则包不可用，暂时无法发起扩展查询。
      </NAlert>

      <NAlert
        v-if="skipAlert?.code === 'DATASOURCE_UNAVAILABLE'"
        type="warning"
        :show-icon="false"
        :bordered="false"
        data-testid="function-datasource-unavailable"
      >
        <div>数据源未接入，请先完成数据构建（BUILD）后再查询；本次不会产生画布节点。</div>
        <div v-if="skipAlert?.message" class="alert-sub">{{ skipAlert.message }}</div>
      </NAlert>
      <NAlert
        v-else-if="skipAlert?.code === 'DEGRADED'"
        type="warning"
        :show-icon="false"
        :bordered="false"
        data-testid="function-degraded"
      >
        <div>查询已降级，未产生结果节点。</div>
        <div v-if="skipAlert?.reason" class="alert-sub">降级原因：{{ skipAlert.reason }}</div>
      </NAlert>
      <NAlert
        v-else-if="skipAlert"
        type="error"
        :show-icon="false"
        :bordered="false"
        data-testid="function-skipped"
      >
        {{ skipAlert.message || '查询未执行' }}
      </NAlert>

      <div class="field">
        <label class="label">查询能力 <span class="req">*</span></label>
        <NSelect
          :value="selectedName"
          :options="functionOptions"
          :disabled="!available || busy"
          placeholder="暂无可选查询能力"
          data-testid="function-select"
          @update:value="onSelect"
        />
      </div>

      <template v-if="selectedForm">
        <p class="dim desc" data-testid="function-description">
          {{ selectedForm.description || '（无说明）' }}
        </p>
        <p class="dim output">
          输出类型：<NTag size="tiny" :bordered="false">{{ selectedForm.output_type }}</NTag>
        </p>

        <div
          v-for="p in selectedForm.params"
          :key="p.key"
          class="field param-field"
          :data-testid="`param-field-${p.key}`"
        >
          <label class="label">
            {{ p.label }}
            <span v-if="p.required" class="req">*</span>
            <span class="param-type">{{ p.type }}</span>
          </label>

          <NInputNumber
            v-if="p.type === 'integer' || p.type === 'decimal' || p.type === 'number'"
            class="param-num"
            :value="(values[p.key] as number | null | undefined) ?? null"
            :show-button="false"
            :disabled="busy"
            :placeholder="p.required ? '必填' : '可选'"
            :data-testid="`param-input-${p.key}`"
            @update:value="(v: number | null) => setNum(p.key, v)"
          />

          <NSwitch
            v-else-if="p.type === 'boolean'"
            :value="values[p.key] === true"
            :disabled="busy"
            :data-testid="`param-input-${p.key}`"
            @update:value="(v: boolean) => setBool(p.key, v)"
          >
            <template #checked>是</template>
            <template #unchecked>否</template>
          </NSwitch>

          <NSelect
            v-else-if="p.type === 'string' && p.enum"
            class="param-enum"
            :value="(values[p.key] as string | null | undefined) ?? null"
            :options="p.enum.map((v) => ({ label: v, value: v }))"
            :disabled="busy"
            :data-testid="`param-input-${p.key}`"
            @update:value="(v: string | null) => setEnum(p.key, v)"
          />

          <NAlert
            v-else-if="p.type === 'string'"
            type="warning"
            :show-icon="false"
            :bordered="false"
          >
            该参数不支持自由文本输入
          </NAlert>
          <p v-else class="dim">暂不支持的参数类型：{{ p.type }}</p>

          <span v-if="errors[p.key]" class="err" :data-testid="`param-err-${p.key}`">
            {{ errors[p.key] }}
          </span>
        </div>

        <p v-if="sourceNodeLabel" class="dim source-line" data-testid="function-source-label">
          查询结果将挂接为「查询自」当前节点：{{ sourceNodeLabel }}
        </p>
        <p v-else class="dim source-line">本次查询不挂接来源节点。</p>
        <p class="dim">查询为只读操作，参数快照随结果节点留痕审计。</p>
      </template>
    </div>

    <template #footer>
      <div class="footer">
        <NButton size="small" :disabled="busy" @click="emit('update:show', false)">
          取消
        </NButton>
        <NButton
          size="small"
          type="primary"
          :loading="busy"
          :disabled="!available || !selectedForm"
          data-testid="function-query-submit"
          @click="onSubmit"
        >
          执行查询
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.param-field {
  gap: 6px;
}
.label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.req {
  color: #d05656;
}
.param-type {
  margin-left: 6px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.param-num {
  width: 220px;
}
.param-enum {
  width: 100%;
}
.err {
  font-size: 11px;
  color: #d05656;
}
.dim {
  color: var(--sun-text-tertiary);
  font-size: 12px;
  margin: 0;
}
.desc {
  line-height: 1.6;
}
.output {
  display: flex;
  align-items: center;
  gap: 6px;
}
.source-line {
  border-top: 1px dashed var(--sun-border);
  padding-top: 8px;
}
.alert-sub {
  margin-top: 4px;
  font-size: 11px;
  opacity: 0.8;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
