<script setup lang="ts">
// P2 案件级添加节点弹窗（PRD 功能 1 编辑通路；后端校验为兜底，前端先拒）。
// 类型：subject/place/event/hypothesis/note（analysis_result 仅 P3 回写产生）。
// 字段规范与后端 canvas_case.validate_case_node_props 同口径；
// 重名待裁决（person_ambiguous=true）时主键输入禁用并清空（红线 R2）。
import { computed, reactive, ref, watch } from 'vue'
import { NButton, NDatePicker, NForm, NFormItem, NInput, NInputNumber, NModal, NSelect, NSwitch, useMessage } from 'naive-ui'
import {
  CASE_MANUAL_NODE_KINDS,
  KIND_LABELS,
  LIMITS,
  type CaseManualNodeKind,
} from '../../domain/canvas'

const props = defineProps<{
  show: boolean
}>()

const emit = defineEmits<{
  'update:show': [v: boolean]
  /** 校验通过：宿主负责 POST（坐标由宿主按视口中心签发） */
  submit: [kind: CaseManualNodeKind, props: Record<string, unknown>]
}>()

const message = useMessage()

const KIND_OPTIONS = CASE_MANUAL_NODE_KINDS.map((k) => ({
  label: KIND_LABELS[k],
  value: k,
}))

const COORD_PRECISION_OPTIONS = ['门牌级', '区划质心', '无坐标'].map((v) => ({
  label: v,
  value: v,
}))
const TIME_PRECISION_OPTIONS = [
  { label: '时刻级（精确到分）', value: 'minute' },
  { label: '日期级（仅日期）', value: 'date' },
]

const kind = ref<CaseManualNodeKind>('subject')
const form = reactive({
  person_name: '',
  person_pk: '',
  person_ambiguous: false,
  location_id: '',
  std_address: '',
  lng: null as number | null,
  lat: null as number | null,
  coord_precision: null as string | null,
  title: '',
  content: '',
  assumption_id: '',
  time_start: null as string | null,
  time_end: null as string | null,
  time_precision: 'date',
})
const errors = reactive<Record<string, string>>({})
const submitting = ref(false)

watch(
  () => props.show,
  (v) => {
    if (v) {
      kind.value = 'subject'
      resetForm()
    }
  },
)

watch(
  () => kind.value,
  () => {
    resetForm()
  },
)

/** 重名开关：开=主键必空（R2：绝不自裁） */
watch(
  () => form.person_ambiguous,
  (amb) => {
    if (amb) form.person_pk = ''
  },
)

/** 时间精度 → 日期选择器口径：日期级=纯日期面板，时刻级=日期+时刻面板 */
const eventDateType = computed<'date' | 'datetime'>(() =>
  form.time_precision === 'minute' ? 'datetime' : 'date',
)
const eventDateFormat = computed(() =>
  form.time_precision === 'minute' ? 'yyyy-MM-dd HH:mm:ss' : 'yyyy-MM-dd',
)

/** 精度切换后旧格式串与新面板不匹配：清空重选，避免脏格式提交 */
watch(
  () => form.time_precision,
  () => {
    form.time_start = null
    form.time_end = null
    delete errors.time_start
    delete errors.time_end
  },
)

function resetForm(): void {
  for (const k of Object.keys(form)) {
    // @ts-expect-error 动态重置（字段均为响应式字面量类型）
    form[k] = typeof form[k] === 'boolean' ? false : null
  }
  form.person_name = ''
  form.person_pk = ''
  form.location_id = ''
  form.std_address = ''
  form.title = ''
  form.content = ''
  form.assumption_id = ''
  form.time_precision = 'date'
  for (const k of Object.keys(errors)) delete errors[k]
}

/** 前端先拒（与后端同口径的字段级校验；空对象=通过） */
function validate(): boolean {
  for (const k of Object.keys(errors)) delete errors[k]
  if (kind.value === 'subject') {
    const name = form.person_name.trim()
    if (!name) errors.person_name = '请填写或选择主体名称'
    else if (name.length > 50) errors.person_name = '主体名称不超过 50 字'
    if (form.person_ambiguous && form.person_pk.trim()) {
      errors.person_pk = '重名待裁决，主键留空'
    } else if (form.person_pk.trim().length > 64) {
      errors.person_pk = '主体主键不超过 64 字'
    }
  } else if (kind.value === 'place') {
    const hasLoc = !!form.location_id.trim()
    const hasCoord = form.lng !== null && form.lat !== null
    if (!hasLoc && !hasCoord) errors.location_id = '请选择地点或填写坐标'
    if (form.lng !== null && (form.lng < -180 || form.lng > 180))
      errors.lng = '经度超出范围（-180~180）'
    if (form.lat !== null && (form.lat < -90 || form.lat > 90))
      errors.lat = '纬度超出范围（-90~90）'
    if (hasLoc !== hasCoord && !hasLoc && (form.lng !== null) !== (form.lat !== null))
      errors.lng = '经纬度必须成对填写'
    if (form.std_address.length > 200) errors.std_address = '标准地址不超过 200 字'
  } else if (kind.value === 'event') {
    const t = form.title.trim()
    if (!t) errors.title = '请填写事件名称'
    else if (t.length > 50) errors.title = '事件名称不超过 50 字'
    if (!form.time_start || !form.time_start.trim()) errors.time_start = '请选择开始时间'
    if (
      form.time_start &&
      form.time_end &&
      form.time_end < form.time_start
    )
      errors.time_end = '结束时间不能早于开始时间'
  } else if (kind.value === 'hypothesis') {
    const t = form.title.trim()
    if (!t) errors.title = '请填写假设标题'
    else if (t.length > LIMITS.hypothesisTitle)
      errors.title = `假设标题不超过 ${LIMITS.hypothesisTitle} 字`
    const c = form.content.trim()
    if (!c) errors.content = '请填写假设内容'
    else if (c.length > LIMITS.content)
      errors.content = `假设内容不超过 ${LIMITS.content} 字`
    if (form.assumption_id.trim().length > 16)
      errors.assumption_id = '假设编号不超过 16 字'
  } else {
    const c = form.content.trim()
    if (!c) errors.content = '请填写备注内容'
    else if (c.length > LIMITS.content)
      errors.content = `备注内容不超过 ${LIMITS.content} 字`
  }
  return Object.keys(errors).length === 0
}

/** 组装 props（只带当前类型相关字段） */
function buildProps(): Record<string, unknown> {
  if (kind.value === 'subject') {
    const p: Record<string, unknown> = {
      person_name: form.person_name.trim(),
    }
    if (form.person_ambiguous) p.person_ambiguous = true
    else if (form.person_pk.trim()) p.person_pk = form.person_pk.trim()
    return p
  }
  if (kind.value === 'place') {
    const p: Record<string, unknown> = {}
    if (form.location_id.trim()) p.location_id = form.location_id.trim()
    if (form.lng !== null && form.lat !== null) {
      p.lng = form.lng
      p.lat = form.lat
    }
    if (form.std_address.trim()) p.std_address = form.std_address.trim()
    if (form.coord_precision) p.coord_precision = form.coord_precision
    return p
  }
  if (kind.value === 'event') {
    const p: Record<string, unknown> = {
      title: form.title.trim(),
      time_start: form.time_start ?? '',
      time_precision: form.time_precision,
    }
    if (form.time_end) p.time_end = form.time_end
    return p
  }
  if (kind.value === 'hypothesis') {
    const p: Record<string, unknown> = {
      title: form.title.trim(),
      content: form.content.trim(),
    }
    // 庙算假设编号（可选）：镜头产出回写时按它自动挂「支撑」边（P3）
    if (form.assumption_id.trim()) p.assumption_id = form.assumption_id.trim()
    return p
  }
  return { content: form.content.trim() }
}

function onSubmit(): void {
  if (submitting.value) return
  if (!validate()) return
  submitting.value = true
  emit('submit', kind.value, buildProps())
}

/** 宿主在成功/失败后调用：成功关弹窗，失败保留表单并提示 */
function finish(ok: boolean, err?: string): void {
  submitting.value = false
  if (ok) {
    message.success('节点已添加', { duration: 1500 })
    emit('update:show', false)
  } else {
    message.error(`保存失败：${err ?? '未知原因'}`)
  }
}

defineExpose({ finish })

const isSubject = computed(() => kind.value === 'subject')
const isPlace = computed(() => kind.value === 'place')
const isEvent = computed(() => kind.value === 'event')
const isHypothesis = computed(() => kind.value === 'hypothesis')
const isNote = computed(() => kind.value === 'note')
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    title="添加研判节点"
    class="cnm"
    data-testid="case-node-modal"
    @update:show="emit('update:show', $event)"
  >
    <NForm label-placement="left" label-width="88" size="small">
      <NSelect
        v-model:value="kind"
        :options="KIND_OPTIONS"
        data-testid="node-kind-select"
      />

      <template v-if="isSubject">
        <NFormItem label="主体名称" :validation-status="errors.person_name ? 'error' : undefined" :feedback="errors.person_name">
          <NInput
            v-model:value="form.person_name"
            placeholder="姓名（可从主体候选选择）"
            maxlength="50"
            data-testid="subject-name"
          />
        </NFormItem>
        <NFormItem label="重名待裁决">
          <NSwitch v-model:value="form.person_ambiguous" data-testid="subject-ambiguous" />
          <span class="cnm-hint">同名异号未裁决时开启；开启后主键留空（绝不自裁）</span>
        </NFormItem>
        <NFormItem
          v-if="!form.person_ambiguous"
          label="主体主键"
          :validation-status="errors.person_pk ? 'error' : undefined"
          :feedback="errors.person_pk"
        >
          <NInput
            v-model:value="form.person_pk"
            placeholder="obj_person 主键（可选）"
            maxlength="64"
            data-testid="subject-pk"
          />
        </NFormItem>
      </template>

      <template v-if="isPlace">
        <NFormItem label="地点标识" :validation-status="errors.location_id ? 'error' : undefined" :feedback="errors.location_id">
          <NInput
            v-model:value="form.location_id"
            placeholder="obj_location 主键（与坐标二选一）"
            maxlength="64"
            data-testid="place-location"
          />
        </NFormItem>
        <NFormItem label="经纬度" :validation-status="errors.lng ? 'error' : undefined" :feedback="errors.lng || errors.lat">
          <div class="cnm-row">
            <NInputNumber
              v-model:value="form.lng"
              placeholder="经度"
              :min="-180"
              :max="180"
              :precision="6"
              class="cnm-coord"
              data-testid="place-lng"
            />
            <NInputNumber
              v-model:value="form.lat"
              placeholder="纬度"
              :min="-90"
              :max="90"
              :precision="6"
              class="cnm-coord"
              data-testid="place-lat"
            />
          </div>
        </NFormItem>
        <NFormItem label="标准地址" :validation-status="errors.std_address ? 'error' : undefined" :feedback="errors.std_address">
          <NInput v-model:value="form.std_address" placeholder="可选；作为节点显示名兜底" maxlength="200" data-testid="place-address" />
        </NFormItem>
        <NFormItem label="坐标精度">
          <NSelect
            v-model:value="form.coord_precision"
            :options="COORD_PRECISION_OPTIONS"
            clearable
            placeholder="可选"
            data-testid="place-precision"
          />
        </NFormItem>
      </template>

      <template v-if="isEvent">
        <NFormItem label="事件名称" :validation-status="errors.title ? 'error' : undefined" :feedback="errors.title">
          <NInput v-model:value="form.title" placeholder="如：第三次转账见面" maxlength="50" data-testid="event-title" />
        </NFormItem>
        <NFormItem label="开始时间" :validation-status="errors.time_start ? 'error' : undefined" :feedback="errors.time_start">
          <NDatePicker
            v-model:formatted-value="form.time_start"
            :type="eventDateType"
            :value-format="eventDateFormat"
            placeholder="必填"
            clearable
            data-testid="event-start"
          />
        </NFormItem>
        <NFormItem label="结束时间" :validation-status="errors.time_end ? 'error' : undefined" :feedback="errors.time_end">
          <NDatePicker
            v-model:formatted-value="form.time_end"
            :type="eventDateType"
            :value-format="eventDateFormat"
            placeholder="可选"
            clearable
            data-testid="event-end"
          />
        </NFormItem>
        <NFormItem label="时间精度">
          <NSelect v-model:value="form.time_precision" :options="TIME_PRECISION_OPTIONS" data-testid="event-precision" />
        </NFormItem>
      </template>

      <template v-if="isHypothesis">
        <NFormItem label="假设标题" :validation-status="errors.title ? 'error' : undefined" :feedback="errors.title">
          <NInput v-model:value="form.title" maxlength="50" data-testid="hyp-title" />
        </NFormItem>
        <NFormItem label="假设内容" :validation-status="errors.content ? 'error' : undefined" :feedback="errors.content">
          <NInput v-model:value="form.content" type="textarea" :rows="3" maxlength="500" data-testid="hyp-content" />
        </NFormItem>
        <NFormItem
          label="假设编号"
          :validation-status="errors.assumption_id ? 'error' : undefined"
          :feedback="errors.assumption_id"
        >
          <NInput
            v-model:value="form.assumption_id"
            placeholder="可选，如 H6；镜头产出回写时自动挂支撑边"
            maxlength="16"
            data-testid="hyp-assumption"
          />
        </NFormItem>
      </template>

      <template v-if="isNote">
        <NFormItem label="备注内容" :validation-status="errors.content ? 'error' : undefined" :feedback="errors.content">
          <NInput v-model:value="form.content" type="textarea" :rows="3" maxlength="500" data-testid="note-content" />
        </NFormItem>
      </template>
    </NForm>

    <template #footer>
      <div class="cnm-footer">
        <NButton size="small" @click="emit('update:show', false)">取消</NButton>
        <NButton
          size="small"
          type="primary"
          :loading="submitting"
          data-testid="node-submit"
          @click="onSubmit"
        >
          添加节点
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.cnm { width: 460px; }
.cnm-row { display: flex; gap: 8px; width: 100%; }
.cnm-coord { flex: 1; }
.cnm-hint {
  margin-left: 8px;
  font-size: 11px;
  color: var(--sun-text-tertiary, #7c8aa5);
}
.cnm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
