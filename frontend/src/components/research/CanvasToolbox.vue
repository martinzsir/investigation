<script setup lang="ts">
// P3 镜头工具箱（PRD V1.0.0 功能 4）：选中画布节点 → 右侧滑出。
//   ① 清单 = 注册表镜头中「画布可用」者（可用性经 domain/canvas-toolbox 纯函数：
//      case_override ?? pack_canvas_enabled，与自动批量跑分离）；
//   ② 靶心自动带入：按 params_schema.auto_from 与选中节点 kind 填参
//      （subject → 主体类参数；place → 坐标/地点类），自动项置灰只读；
//   ③ 一键 202 入队（origin.surface="case_canvas" + node_id）——worker 完成后
//      把观察回写为本画布 analysis_result 节点；轮询与上图脉冲由宿主负责。
// 纯编排组件：不发自由 SQL、不下定性结论；可运行性由后端再兜底（400）。
import { computed, reactive, ref, watch } from 'vue'
import { NButton, NForm, NFormItem, NInput, NInputNumber, NSpin, useMessage } from 'naive-ui'
import { CloseOutline, FlaskOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import {
  lensesApi,
  lensDesc,
  validateLensParams,
  type LensSpecItem,
} from '../../api/endpoints/lenses'
import {
  autoFillParams,
  lensDisabledReason,
  lensReadyHint,
  toolboxLensAvailable,
  type ToolboxNode,
} from '../../domain/canvas-toolbox'
import { presentError } from '../../api/errors'

const props = defineProps<{
  show: boolean
  caseId: string
  node: ToolboxNode | null
}>()

const emit = defineEmits<{
  'update:show': [v: boolean]
  /** 202 入队成功：宿主接管轮询与回写上图（带中文名供状态条展示） */
  submitted: [taskId: string, skillId: string, name: string]
}>()

const message = useMessage()

const loading = ref(false)
const loadFailed = ref('')
const lenses = ref<LensSpecItem[]>([])
const selected = ref<LensSpecItem | null>(null)
const submitting = ref(false)
const form = reactive<Record<string, string | number | null>>({})
const autoKeys = ref<string[]>([])

/** 工具箱清单：只列画布可用镜头（草案/停用列出但置灰，留可见性） */
const toolboxLenses = computed(() =>
  lenses.value.filter((l) => toolboxLensAvailable(l)),
)

async function ensureLoaded(): Promise<void> {
  if (lenses.value.length || loading.value || !props.caseId) return
  loading.value = true
  loadFailed.value = ''
  try {
    const res = await lensesApi.list(props.caseId)
    lenses.value = res.lenses
  } catch (e) {
    loadFailed.value = presentError(e).title
  } finally {
    loading.value = false
  }
}

watch(
  () => props.show,
  (v) => {
    if (v) {
      selected.value = null
      void ensureLoaded()
    }
  },
)

/** 选中镜头 → 靶心自动带入（auto 项置灰；用户清除自动值可手填） */
watch(selected, (lens) => {
  for (const k of Object.keys(form)) delete form[k]
  autoKeys.value = []
  if (!lens) return
  const { values, autoKeys: keys } = autoFillParams(lens, props.node)
  Object.assign(form, values)
  autoKeys.value = keys
})

function disabledReason(l: LensSpecItem): string {
  return lensDisabledReason(l)
}

// 模板内 v-model 只能绑定可写成员表达式（不允许 as/?? 强转），
// form 又是 string|number 混合态 → 用显式 getter/setter 桥接分型。
function numVal(key: string): number | null {
  const v = form[key]
  return typeof v === 'number' ? v : null
}
function setNum(key: string, v: number | null): void {
  form[key] = v
}
function strVal(key: string): string | null {
  const v = form[key]
  return v === null || v === undefined ? null : String(v)
}
function setStr(key: string, v: string): void {
  form[key] = v
}

function readyHint(l: LensSpecItem): string {
  return lensReadyHint(l)
}

/** 提交：缺必填 → auto=true 交后端按 auto_from 推导（与启停面板同语义） */
function onRun(): void {
  const lens = selected.value
  if (!lens || !props.node || submitting.value || !props.caseId) return
  const dr = disabledReason(lens)
  if (dr) {
    message.warning(dr)
    return
  }
  const values: Record<string, unknown> = {}
  for (const k of Object.keys(lens.params_schema)) {
    const v = form[k]
    if (v !== null && v !== undefined && String(v).trim() !== '') values[k] = v
  }
  // 前端先拒：手动填写了非法值时直接拦（缺省空值放行走 auto 推导）
  const filledErrors = validateLensParams(lens, values)
  if (Object.keys(filledErrors).length) {
    const first = Object.entries(filledErrors)[0]
    message.error(`${first[0]}：${first[1]}`)
    return
  }
  const missing = Object.keys(lens.params_schema).filter(
    (k) =>
      lens.params_schema[k]?.required &&
      (values[k] === undefined || values[k] === ''),
  )
  submitting.value = true
  lensesApi
    .run(props.caseId, lens.skill_id, {
      params: values,
      auto: missing.length > 0,
      origin: {
        node_id: props.node.id,
        subject: props.node.label,
        surface: 'case_canvas',
      },
    })
    .then((res) => {
      message.success(`「${lens.name}」已入队，产出将自动回写画布`, {
        duration: 2500,
      })
      emit('submitted', res.task.id, lens.skill_id, lens.name)
      emit('update:show', false)
    })
    .catch((e) => {
      message.error(`入队失败：${presentError(e).title}`)
    })
    .finally(() => {
      submitting.value = false
    })
}

defineExpose({ finish: () => undefined })
</script>

<template>
  <aside v-if="show" class="toolbox" data-testid="lens-toolbox">
    <header class="tb-head">
      <span class="tb-title">
        <NIcon :component="FlaskOutline" :size="14" />
        镜头工具箱
      </span>
      <span v-if="node" class="tb-target" data-testid="toolbox-target">靶心：{{ node.label }}</span>
      <button class="tb-close" data-testid="toolbox-close" @click="emit('update:show', false)">
        <NIcon :component="CloseOutline" :size="14" />
      </button>
    </header>

    <NSpin v-if="loading" size="small" class="tb-spin" />
    <div v-else-if="loadFailed" class="tb-error">
      镜头清单读取失败：{{ loadFailed }}
    </div>

    <template v-else>
      <div v-if="!selected" class="tb-list" data-testid="toolbox-list">
        <div v-if="toolboxLenses.length === 0" class="tb-empty">
          暂无画布可用镜头（pack.json 未声明 canvas_enabled 或已被案件停用）
        </div>
        <button
          v-for="l in toolboxLenses"
          :key="l.skill_id"
          class="tb-item"
          :class="{ 'tb-item-off': !!disabledReason(l) }"
          :disabled="!!disabledReason(l)"
          :data-testid="`toolbox-lens-${l.skill_id}`"
          @click="selected = l"
        >
          <span class="tb-item-name">{{ l.name }}</span>
          <span class="tb-item-stage">{{ l.stage }}</span>
          <span class="tb-item-desc">{{ lensDesc(l) }}</span>
          <span v-if="disabledReason(l)" class="tb-item-warn">{{ disabledReason(l) }}</span>
          <span v-else-if="readyHint(l)" class="tb-item-hint">{{ readyHint(l) }}（仍可试跑）</span>
        </button>
      </div>

      <div v-else class="tb-form">
        <button class="tb-back" @click="selected = null">‹ 返回清单</button>
        <p class="tb-form-desc">{{ lensDesc(selected) }}</p>
        <NForm size="small" label-placement="top">
          <NFormItem
            v-for="(ps, key) in selected.params_schema"
            :key="key"
            :label="`${key}${ps.required ? ' *' : ''}${autoKeys.includes(String(key)) ? '（靶心带入）' : ''}`"
          >
            <NInputNumber
              v-if="ps.type === 'integer' || ps.type === 'decimal'"
              :value="numVal(String(key))"
              @update:value="setNum(String(key), $event)"
              :disabled="autoKeys.includes(String(key))"
              :precision="ps.type === 'integer' ? 0 : undefined"
              placeholder="可选"
              class="tb-num"
            />
            <NInput
              v-else
              :value="strVal(String(key))"
              @update:value="setStr(String(key), $event)"
              :disabled="autoKeys.includes(String(key))"
              :placeholder="autoKeys.includes(String(key)) ? '靶心自动带入' : '可选'"
            />
          </NFormItem>
        </NForm>
        <NButton
          type="primary"
          size="small"
          block
          :loading="submitting"
          data-testid="toolbox-run"
          @click="onRun"
        >
          运行镜头（产出回写画布）
        </NButton>
        <p class="tb-form-note">
          缺必填参数时按 auto_from 自动推导；镜头只摆结构不下结论，
          产出以「研判结论」节点上图并挂到靶心。
        </p>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.toolbox {
  position: absolute;
  top: 12px;
  right: 12px;
  bottom: 44px;
  width: 280px;
  z-index: 30;
  display: flex;
  flex-direction: column;
  background: var(--sun-surface, #fff);
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 10px;
  box-shadow: 0 6px 20px rgba(10, 27, 54, 0.14);
  overflow: hidden;
}
.tb-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--sun-border, #e2e8f2);
  flex: none;
}
.tb-title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-primary);
}
.tb-target {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.tb-close {
  border: 0;
  background: transparent;
  cursor: pointer;
  color: var(--sun-text-tertiary);
  padding: 2px;
  display: inline-flex;
}
.tb-spin { margin: 20px auto; }
.tb-error { padding: 12px; font-size: 12px; color: var(--sun-danger, #d03050); }
.tb-list {
  flex: 1;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
}
.tb-empty { font-size: 12px; color: var(--sun-text-tertiary); padding: 8px; }
.tb-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: left;
  padding: 8px 10px;
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
}
.tb-item:hover { border-color: var(--sun-primary, #2f6fed); }
.tb-item-off { opacity: 0.55; cursor: not-allowed; }
.tb-item-name { font-size: 12px; font-weight: 600; color: var(--sun-text-primary); }
.tb-item-stage {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 4px;
  padding: 0 4px;
  width: fit-content;
}
.tb-item-desc {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tb-item-warn { font-size: 11px; color: #8a5a00; }
.tb-item-hint { font-size: 11px; color: var(--sun-text-tertiary); }
.tb-form {
  flex: 1;
  overflow: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tb-back {
  border: 0;
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  text-align: left;
  padding: 0;
  width: fit-content;
}
.tb-form-desc { margin: 0; font-size: 11px; color: var(--sun-text-tertiary); }
.tb-num { width: 100%; }
.tb-form-note { margin: 0; font-size: 11px; color: var(--sun-text-tertiary); }
</style>
