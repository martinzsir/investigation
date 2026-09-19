<script setup lang="ts">
// 画布定向镜头调度弹窗（预设卡片版）：
// - 默认「业务模式」：预设卡片（查关系圈层/查共同关系/查围标时间碰撞…）只问
//   1-2 个业务问题，其余参数静默走镜头缺省值；画布选中主体经 prefill 自动带入；
// - 「高级选项」折叠收非业务字段的参数（跳数/窗口/边类别等），值与业务字段同一
//   数据源；预设未覆盖的定向镜头自动回落「完整参数」模式（原表单）；
// - 仅展示 GET /cases/{cid}/lenses 返回的定向镜头（requires_params ∧ enabled
//   ∧ pack_enabled ∧ deterministic，由父组件过滤后传入）；
// - 校验仍以 params_schema 为准（validateLensParams + 后端 _validate_params 兜底）。
import { computed, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NCollapse,
  NCollapseItem,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  NSwitch,
} from 'naive-ui'
import { validateLensParams, type LensSpecItem } from '../../api/endpoints/lenses'
import { LENS_PRESETS, presetOfSkill, type LensPreset } from '../../domain/lensPresets'

const props = defineProps<{
  show: boolean
  /** 可定向调度的镜头清单（父组件已按 requires_params/enabled/mode 过滤） */
  lenses: LensSpecItem[]
  /** 请求进行中（父组件置位） */
  busy?: boolean
  /** 画布选中主体预填（按参数名命中才填：target_subject/subject_a/project…） */
  prefill?: Record<string, unknown>
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'submit', payload: { skill_id: string; params: Record<string, unknown> }): void
}>()

// ---------- 预设（业务模式） ----------
const mode = ref<'preset' | 'advanced'>('preset')
const selectedId = ref<string | null>(null)
const values = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})

/** 当前镜头清单里有预设覆盖的卡片 */
const presetCards = computed<LensPreset[]>(() =>
  LENS_PRESETS.filter((p) => props.lenses.some((l) => l.skill_id === p.skill_id)),
)
/** 有预设镜头存在 → 默认业务模式可用 */
const hasPresets = computed(() => presetCards.value.length > 0)
/** 无预设覆盖的镜头（只在完整参数模式的下拉里出现） */
const rawLensOptions = computed(() =>
  props.lenses
    .filter((l) => !presetOfSkill(l.skill_id))
    .map((l) => ({ label: `${l.name}（${l.skill_id}）`, value: l.skill_id })),
)

const selectedLens = computed<LensSpecItem | null>(
  () => props.lenses.find((l) => l.skill_id === selectedId.value) ?? null,
)
const activePreset = computed<LensPreset | null>(() =>
  selectedLens.value ? presetOfSkill(selectedLens.value.skill_id) : null,
)
/** 高级折叠里只放非业务字段参数（业务字段在上方已同步） */
const advancedEntries = computed(() => {
  const schema = selectedLens.value?.params_schema ?? {}
  const biz = new Set(activePreset.value?.fields.map((f) => f.param) ?? [])
  return Object.entries(schema)
    .map(([key, spec]) => ({ key, spec }))
    .filter((p) => !biz.has(p.key))
})
const fullEntries = computed(() => {
  const schema = selectedLens.value?.params_schema ?? {}
  return Object.entries(schema).map(([key, spec]) => ({ key, spec }))
})

function initValues(lens: LensSpecItem | null): void {
  const next: Record<string, unknown> = {}
  if (lens) {
    for (const [key, spec] of Object.entries(lens.params_schema)) {
      const pre = props.prefill?.[key]
      if (pre !== null && pre !== undefined && pre !== '') {
        next[key] = pre
      } else if (spec.type === 'boolean') {
        next[key] = false
      }
    }
  }
  values.value = next
}

/** 业务字段被画布预填命中 → 展示来源徽标 */
function isPrefilled(param: string): boolean {
  const pre = props.prefill?.[param]
  return pre !== null && pre !== undefined && pre !== ''
}

function selectPreset(p: LensPreset): void {
  mode.value = 'preset'
  selectedId.value = p.skill_id
  errors.value = {}
  initValues(props.lenses.find((l) => l.skill_id === p.skill_id) ?? null)
}

function onSelect(id: string): void {
  selectedId.value = id
  errors.value = {}
  initValues(props.lenses.find((l) => l.skill_id === id) ?? null)
}

function setVal(key: string, v: unknown): void {
  values.value = { ...values.value, [key]: v }
}

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    errors.value = {}
    if (hasPresets.value) {
      // 打开即选中首个预设（画布选中主体 → prefill 直接落在业务字段上）
      if (!activePreset.value || mode.value === 'preset') selectPreset(presetCards.value[0])
      else initValues(selectedLens.value)
    } else {
      mode.value = 'advanced'
      if (!selectedLens.value && props.lenses.length) {
        onSelect(props.lenses[0].skill_id)
      } else {
        initValues(selectedLens.value)
      }
    }
  },
  { immediate: true },
)

function onSubmit(): void {
  const lens = selectedLens.value
  if (!lens) return
  const errs = validateLensParams(lens, values.value)
  errors.value = errs
  if (Object.keys(errs).length > 0) return
  const params: Record<string, unknown> = {}
  for (const [key, spec] of Object.entries(lens.params_schema)) {
    const v = values.value[key]
    if (v === null || v === undefined) continue
    if (typeof v === 'string' && v.trim() === '') continue
    if (spec.type === 'boolean' && v === false) continue
    params[key] = typeof v === 'string' ? v.trim() : v
  }
  emit('submit', { skill_id: lens.skill_id, params })
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    title="定向镜头"
    class="lens-run-modal"
    :style="{ width: '640px' }"
    :mask-closable="false"
    data-testid="lens-run-modal"
    @update:show="emit('update:show', $event)"
  >
    <div class="form">
      <NAlert
        v-if="lenses.length === 0"
        type="info"
        :show-icon="false"
        :bordered="false"
        data-testid="lens-run-empty"
      >
        暂无可定向调度的镜头（需要已启用的确定性镜头且声明必填参数）。
      </NAlert>

      <!-- ===================== 业务模式：预设卡片 ===================== -->
      <template v-else-if="mode === 'preset'">
        <div class="preset-grid">
          <button
            v-for="p in presetCards"
            :key="p.preset_id"
            type="button"
            class="preset-card"
            :class="{ 'preset-card--active': activePreset?.preset_id === p.preset_id }"
            :data-testid="`lens-preset-${p.preset_id}`"
            :disabled="busy"
            @click="selectPreset(p)"
          >
            <span class="pc-title">{{ p.title }}</span>
            <span class="pc-desc">{{ p.desc }}</span>
          </button>
          <button
            v-if="rawLensOptions.length > 0"
            type="button"
            class="preset-card preset-card--raw"
            :disabled="busy"
            data-testid="lens-preset-raw"
            @click="mode = 'advanced'"
          >
            <span class="pc-title">其他镜头</span>
            <span class="pc-desc">按完整参数逐项填写</span>
          </button>
        </div>

        <div v-if="activePreset" class="preset-fields" :data-testid="`lens-preset-form-${activePreset.preset_id}`">
          <div
            v-for="f in activePreset.fields"
            :key="f.param"
            class="field"
            :data-testid="`lens-param-${f.param}`"
          >
            <label class="label">
              {{ f.label }}
              <span v-if="selectedLens?.params_schema[f.param]?.required" class="req">*</span>
              <span v-if="isPrefilled(f.param)" class="prefill-tag">已按画布选中预填</span>
            </label>
            <NInput
              :value="(values[f.param] as string | null | undefined) ?? null"
              :placeholder="f.placeholder ?? '输入名称'"
              :disabled="busy"
              :data-testid="`lens-param-input-${f.param}`"
              @update:value="(v: string) => setVal(f.param, v)"
            />
            <span v-if="errors[f.param]" class="err" :data-testid="`lens-param-err-${f.param}`">
              {{ errors[f.param] }}
            </span>
          </div>

          <NCollapse v-if="advancedEntries.length > 0" class="adv">
            <NCollapseItem title="高级选项（留空即用镜头缺省值）" name="adv">
              <div
                v-for="p in advancedEntries"
                :key="p.key"
                class="field adv-field"
                :data-testid="`lens-param-${p.key}`"
              >
                <label class="label">
                  {{ p.key }}
                  <span class="param-type">{{ p.spec.type }}</span>
                </label>
                <NInputNumber
                  v-if="p.spec.type === 'integer' || p.spec.type === 'decimal'"
                  class="param-num"
                  :value="(values[p.key] as number | null | undefined) ?? null"
                  :precision="p.spec.type === 'integer' ? 0 : undefined"
                  :show-button="false"
                  :disabled="busy"
                  placeholder="可选"
                  :data-testid="`lens-param-input-${p.key}`"
                  @update:value="(v: number | null) => setVal(p.key, v)"
                />
                <NSwitch
                  v-else-if="p.spec.type === 'boolean'"
                  :value="values[p.key] === true"
                  :disabled="busy"
                  :data-testid="`lens-param-input-${p.key}`"
                  @update:value="(v: boolean) => setVal(p.key, v)"
                >
                  <template #checked>是</template>
                  <template #unchecked>否</template>
                </NSwitch>
                <NInput
                  v-else
                  :value="(values[p.key] as string | null | undefined) ?? null"
                  :disabled="busy"
                  :placeholder="p.spec.type === 'date' ? 'YYYY-MM-DD（可选）' : '可选'"
                  :data-testid="`lens-param-input-${p.key}`"
                  @update:value="(v: string) => setVal(p.key, v)"
                />
                <p v-if="p.spec.description" class="dim adv-desc">{{ p.spec.description }}</p>
                <span v-if="errors[p.key]" class="err">{{ errors[p.key] }}</span>
              </div>
            </NCollapseItem>
          </NCollapse>
        </div>

        <p class="dim mode-line">
          懂参数？<NButton text size="tiny" type="primary" :disabled="busy" @click="mode = 'advanced'">
            改填完整参数
          </NButton>
        </p>
      </template>

      <!-- ===================== 完整参数模式（原表单） ===================== -->
      <template v-else>
        <div class="field">
          <label class="label">镜头 <span class="req">*</span></label>
          <NSelect
            :value="selectedId"
            :options="
              rawLensOptions.length > 0
                ? rawLensOptions
                : props.lenses.map((l) => ({ label: `${l.name}（${l.skill_id}）`, value: l.skill_id }))
            "
            :disabled="busy || lenses.length === 0"
            placeholder="暂无可定向调度的镜头"
            data-testid="lens-run-select"
            @update:value="onSelect"
          />
        </div>

        <div
          v-for="p in fullEntries"
          :key="p.key"
          class="field param-field"
          :data-testid="`lens-param-${p.key}`"
        >
          <label class="label">
            {{ p.key }}
            <span v-if="p.spec.required" class="req">*</span>
            <span class="param-type">{{ p.spec.type }}</span>
          </label>
          <NInputNumber
            v-if="p.spec.type === 'integer'"
            class="param-num"
            :value="(values[p.key] as number | null | undefined) ?? null"
            :precision="0"
            :show-button="false"
            :disabled="busy"
            :placeholder="p.spec.required ? '必填' : '可选'"
            :data-testid="`lens-param-input-${p.key}`"
            @update:value="(v: number | null) => setVal(p.key, v)"
          />
          <NInputNumber
            v-else-if="p.spec.type === 'decimal'"
            class="param-num"
            :value="(values[p.key] as number | null | undefined) ?? null"
            :show-button="false"
            :disabled="busy"
            :placeholder="p.spec.required ? '必填' : '可选'"
            :data-testid="`lens-param-input-${p.key}`"
            @update:value="(v: number | null) => setVal(p.key, v)"
          />
          <NSwitch
            v-else-if="p.spec.type === 'boolean'"
            :value="values[p.key] === true"
            :disabled="busy"
            :data-testid="`lens-param-input-${p.key}`"
            @update:value="(v: boolean) => setVal(p.key, v)"
          >
            <template #checked>是</template>
            <template #unchecked>否</template>
          </NSwitch>
          <NInput
            v-else
            :value="(values[p.key] as string | null | undefined) ?? null"
            :disabled="busy"
            :placeholder="p.spec.type === 'date' ? 'YYYY-MM-DD（可选）' : p.spec.required ? '必填' : '可选'"
            :data-testid="`lens-param-input-${p.key}`"
            @update:value="(v: string) => setVal(p.key, v)"
          />
          <p v-if="p.spec.description" class="dim adv-desc">{{ p.spec.description }}</p>
          <span v-if="errors[p.key]" class="err" :data-testid="`lens-param-err-${p.key}`">
            {{ errors[p.key] }}
          </span>
        </div>

        <p v-if="hasPresets" class="dim mode-line">
          参数太技术？<NButton text size="tiny" type="primary" :disabled="busy" @click="selectPreset(presetCards[0])">
            返回业务模式
          </NButton>
        </p>
      </template>

      <p class="dim source-line">
        对当前生效版本只读运行，产出线索进线索列表（挂产生它的版本，不产新版本文件）。
      </p>
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
          :disabled="!selectedLens"
          data-testid="lens-run-submit"
          @click="onSubmit"
        >
          发起定向调度
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
.err {
  font-size: 11px;
  color: #d05656;
}
.dim {
  color: var(--sun-text-tertiary);
  font-size: 12px;
  margin: 0;
}
.adv-desc {
  font-size: 11px;
}
.source-line {
  border-top: 1px dashed var(--sun-border);
  padding-top: 8px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
/* ---- 预设卡片网格 ---- */
.preset-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}
.preset-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 11px;
  text-align: left;
  border: 1px solid var(--sun-border);
  border-radius: 8px;
  background: var(--sun-bg-card);
  cursor: pointer;
  font: inherit;
}
.preset-card:hover:not(:disabled) {
  border-color: var(--sun-border-active);
}
.preset-card--active {
  border-color: var(--sun-border-active);
  background: var(--sun-input-bg);
}
.preset-card--raw {
  border-style: dashed;
}
.pc-title {
  font-size: 13px;
  font-weight: 600;
}
.pc-desc {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.preset-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.prefill-tag {
  margin-left: 6px;
  padding: 0 6px;
  font-size: 10px;
  line-height: 16px;
  border: 1px solid var(--sun-ok-border);
  border-radius: 8px;
  color: var(--sun-ok-text);
}
.adv {
  border-top: 1px dashed var(--sun-border);
  padding-top: 4px;
}
.adv-field {
  gap: 4px;
  padding-bottom: 6px;
}
.mode-line {
  text-align: right;
}
</style>
