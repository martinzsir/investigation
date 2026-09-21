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
  NTag,
} from 'naive-ui'
import {
  candidateSourceLabel,
  validateLensParams,
  type LensParamCandidates,
  type LensRecommendation,
  type LensSpecItem,
} from '../../api/endpoints/lenses'
import { lensesApi } from '../../api/endpoints/lenses'
import { LENS_PRESETS, presetOfSkill, type LensPreset } from '../../domain/lensPresets'

const props = defineProps<{
  show: boolean
  /** 可定向调度的镜头清单（父组件已按 requires_params/enabled/mode 过滤） */
  lenses: LensSpecItem[]
  /** 请求进行中（父组件置位） */
  busy?: boolean
  /** 画布选中主体预填（按参数名命中才填：target_subject/subject_a/project…） */
  prefill?: Record<string, unknown>
  /** 画布可见主体名（候选规模主力，典型 20-80；不传则只走案件登记/语义层） */
  canvasNodes?: string[]
  /** 画布选中主体（候选最高优先；唯一时静默带入） */
  selectedNode?: string | null
  /** 案件 id（拉取参数候选用） */
  caseId?: string
  /**
   * 针对当前线索假设的镜头贴合度推荐（父组件从
   * /lenses/recommendations 取）。为空表示无假设链（自动发现线索）
   * 或取不到——此时按默认顺序展示，**不硬凑排序**。
   */
  recommendations?: LensRecommendation[]
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

/**
 * 当前镜头清单里有预设覆盖的卡片。
 * 有假设推荐时按贴合度排序（默认落在更贴切的镜头上）——只影响顺序，
 * 不隐藏任何镜头：正兵有权用任何手段验证任何假设。
 */
const presetCards = computed<LensPreset[]>(() => {
  const cards = LENS_PRESETS.filter(
    (p) => props.lenses.some((l) => l.skill_id === p.skill_id))
  const rec = recBySkill.value
  if (!Object.keys(rec).length) return cards
  return [...cards].sort(
    (a, b) => (_rank(rec[b.skill_id]?.level ?? '') -
               _rank(rec[a.skill_id]?.level ?? '')),
  )
})
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

// ---------- 假设贴合度推荐（只影响默认顺序，不排除任何镜头） ----------
/**
 * 把后端推荐拍平成 skill_id → {等级, 理由}。
 * 推荐为空（线索无假设链）→ 全部镜头无标记，按默认顺序展示。
 */
const recBySkill = computed<Record<string, {
  level: 'recommended' | 'possible' | 'unrelated'
  reasons: string[]
  hypId: string
}>>(() => {
  const out: Record<string, {
    level: 'recommended' | 'possible' | 'unrelated'
    reasons: string[]
    hypId: string
  }> = {}
  for (const r of props.recommendations ?? []) {
    for (const l of r.lenses ?? []) {
      const prev = out[l.skill_id]
      // 多假设时取更贴切的那条；同级保留先出现的（后端已按 score 降序）
      if (!prev || _rank(l.recommendation) > _rank(prev.level)) {
        out[l.skill_id] = {
          level: l.recommendation, reasons: l.reasons, hypId: r.hypothesis_id,
        }
      }
    }
  }
  return out
})

function _rank(level: string): number {
  return level === 'recommended' ? 3 : level === 'possible' ? 2 : 1
}

/** 推荐徽标文案；无推荐返回空（不显示徽标） */
function recBadge(skillId: string): string {
  const r = recBySkill.value[skillId]
  if (!r || r.level === 'unrelated') return ''
  return r.level === 'recommended' ? '贴合本假设' : '可能相关'
}

/** 推荐理由（悬浮说明） */
function recTitle(skillId: string): string {
  const r = recBySkill.value[skillId]
  if (!r) return ''
  return `针对假设 ${r.hypId}：${r.reasons.join('；')}`
}

/** 该镜头的数据就绪度（来自镜头清单的 readiness 字段） */
function lensReadiness(skillId: string) {
  return props.lenses.find((l) => l.skill_id === skillId)?.readiness ?? null
}

// ---------- 参数候选（自动推荐） ----------
// 后端按「画布选中 > 画布可见 > 案件登记 > 线索 > 语义层」排序，语义层不做
// 全量返回（真实案件数万主体，全量进下拉会 DOM 爆炸）。
const paramCands = ref<Record<string, LensParamCandidates>>({})
const candLoading = ref(false)

/** 参数候选来源徽标（让正兵看出"系统替我选了谁"，且可随时覆盖） */
function candLabel(param: string): string {
  const c = paramCands.value[param]
  if (!c?.recommended) return ''
  return candidateSourceLabel(c.source)
}

/** 候选唯一 → 静默带入（不展示选择器，只显示已选值） */
function isAutoOnly(param: string): boolean {
  return paramCands.value[param]?.auto_only === true
}

/** 候选可直列（≤100）→ 下拉，默认选推荐值 */
function candOptions(param: string): { label: string; value: string }[] {
  const c = paramCands.value[param]
  if (!c) return []
  return c.candidates.map((x) => ({
    label: x.name,
    value: x.name,
  }))
}

async function fetchCandidates(skillId: string): Promise<void> {
  paramCands.value = {}
  if (!props.caseId || !skillId) return
  candLoading.value = true
  try {
    const r = await lensesApi.paramCandidates(props.caseId, skillId, {
      canvas_nodes: props.canvasNodes ?? [],
      selected_node: props.selectedNode ?? null,
    })
    paramCands.value = r.params ?? {}
    // 推荐值落地：有推荐且当前为空（或预填为空）→ 带入推荐值
    for (const [param, c] of Object.entries(paramCands.value)) {
      const cur = values.value[param]
      const empty = cur === null || cur === undefined || cur === ''
      if (c.recommended && empty) {
        values.value = { ...values.value, [param]: c.recommended.name }
      }
    }
  } catch {
    paramCands.value = {}
  } finally {
    candLoading.value = false
  }
}
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
  void fetchCandidates(p.skill_id)
}

function onSelect(id: string): void {
  selectedId.value = id
  errors.value = {}
  initValues(props.lenses.find((l) => l.skill_id === id) ?? null)
  void fetchCandidates(id)
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
        if (selectedLens.value) void fetchCandidates(selectedLens.value.skill_id)
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
            <span class="pc-title">
              {{ p.title }}
              <!-- 假设贴合度徽标：有推荐才显示，不排除任何镜头 -->
              <NTag
                v-if="recBadge(p.skill_id)"
                size="tiny"
                round
                :type="recBySkill[p.skill_id]?.level === 'recommended' ? 'success' : 'warning'"
                class="pc-rec"
                :title="recTitle(p.skill_id)"
                data-testid="lens-rec-badge"
              >{{ recBadge(p.skill_id) }}</NTag>
              <!-- 数据就绪度：缺数据 → 跑了也会降级（提前告知，不禁止） -->
              <NTag
                v-if="lensReadiness(p.skill_id)?.ready === false"
                size="tiny"
                round
                type="error"
                class="pc-rec"
                :title="lensReadiness(p.skill_id)?.note || ''"
                data-testid="lens-ready-badge"
              >数据未齐</NTag>
            </span>
            <span class="pc-desc">{{ p.desc }}</span>
            <!-- 降级预告：说清缺什么、会怎样，而不是跑完才发现 -->
            <span
              v-if="lensReadiness(p.skill_id)?.ready === false"
              class="pc-warn"
            >{{ lensReadiness(p.skill_id)?.note }}</span>
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
              <span v-else-if="candLabel(f.param)" class="prefill-tag" :data-testid="`lens-cand-tag-${f.param}`">
                {{ candLabel(f.param) }}
              </span>
            </label>
            <!-- 候选唯一：静默带入，不要求填写 -->
            <div v-if="isAutoOnly(f.param)" class="auto-only" :data-testid="`lens-auto-only-${f.param}`">
              {{ values[f.param] }}
            </div>
            <!-- 候选可直列（≤100）：下拉，默认选推荐值，可覆盖 -->
            <NSelect
              v-else-if="candOptions(f.param).length > 0"
              :value="(values[f.param] as string | null | undefined) ?? null"
              :options="candOptions(f.param)"
              :loading="candLoading"
              filterable
              clearable
              :disabled="busy"
              :placeholder="f.placeholder ?? '选择或输入'"
              :data-testid="`lens-param-select-${f.param}`"
              @update:value="(v: string | null) => setVal(f.param, v ?? '')"
            />
            <!-- 无候选：才需要手填 -->
            <NInput
              v-else
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
/* 贴合度 / 就绪度徽标：跟标题同行，不抢主视觉 */
.pc-rec {
  margin-left: 4px;
  vertical-align: middle;
}
/* 数据未齐的降级预告：说清缺什么、会怎样 */
.pc-warn {
  display: block;
  margin-top: 4px;
  font-size: 10px;
  line-height: 1.4;
  color: var(--sun-warning, #d89614);
}
.preset-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.auto-only {
  padding: 6px 10px;
  border: 1px dashed var(--border-color, #d9d9d9);
  border-radius: 4px;
  color: var(--text-color-2, #666);
  background: var(--action-color, #fafafa);
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
