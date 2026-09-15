<script setup lang="ts">
// S2 关系定义弹窗（PRD §6 F3 / 线框 E2）：name/title/端点/基数。
// - 端点下拉只列未删除对象 → E3-1（端点不存在）天然拦截；
// - 基数 cardinality 是 S2 新增声明字段（links.schema.json 同步修订由后端批次落地）；
//   （空）= 未声明，兼容历史数据（D5）；
// - link 级 properties / endpoints / jian / jian_source 等不在表单 —— 保存时原样保留（R2）；
// - 已有关系的 name 置灰（同 R1 精神：link 名被 rules / bindings 引用，改名 = 隐式断链）。
import { computed, ref, watch } from 'vue'
import { NModal, NButton, NInput, NSelect, NTooltip } from 'naive-ui'
import { CARDINALITIES, CARDINALITY_LABEL, type Cardinality } from '../../api/endpoints/model'

interface LinkForm {
  name: string
  title: string
  from_obj: string
  to_obj: string
  /** '' = 未声明（兼容历史数据） */
  cardinality: '' | Cardinality
}

const props = withDefaults(
  defineProps<{
    show: boolean
    mode: 'create' | 'edit'
    initial: LinkForm
    /** 已有关系名（不含当前编辑项）——新建/改名唯一性校验用 */
    linkNames: string[]
    objectOptions: { label: string; value: string }[]
    loading?: boolean
  }>(),
  { loading: false },
)

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'confirm', form: LinkForm): void
  (e: 'delete'): void
}>()

const draft = ref<LinkForm>({ ...props.initial })
const error = ref('')

watch(
  () => props.show,
  (v) => {
    if (v) {
      draft.value = { ...props.initial }
      error.value = ''
    }
  },
)

const cardinalityOptions = [
  { label: '（空）= 未声明，兼容历史数据', value: '' },
  ...CARDINALITIES.map((c) => ({ label: `${c}（${CARDINALITY_LABEL[c]}）`, value: c })),
]

const namePattern = /^[a-z_]+$/
const nameMissing = computed(() => !draft.value.name.trim())
const nameBad = computed(
  () => !nameMissing.value && !namePattern.test(draft.value.name),
)
const nameDup = computed(() => props.linkNames.includes(draft.value.name))
const endpointMissing = computed(() => !draft.value.from_obj || !draft.value.to_obj)

function confirm(): void {
  if (props.loading) return
  if (nameMissing.value || nameBad.value) {
    error.value = nameMissing.value ? '关系 name 必填' : 'name 仅允许小写字母与下划线（^[a-z_]+$）'
    return
  }
  if (nameDup.value) {
    error.value = `关系名 ${draft.value.name} 已存在`
    return
  }
  if (endpointMissing.value) {
    error.value = '端点对象不能为空'
    return
  }
  error.value = ''
  emit('confirm', { ...draft.value })
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="mode === 'create' ? '新建关系' : '编辑关系'"
    class="lem-modal"
    :mask-closable="false"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="lem-body">
      <div class="lem-row">
        <label class="lem-label">
          name
          <NTooltip v-if="mode === 'edit'" trigger="hover">
            <template #trigger>
              <span class="lem-lock">🔒</span>
            </template>
            关系名被 rules / bindings 引用，不可修改。如需改名请新建关系再迁移。
          </NTooltip>
        </label>
        <NInput
          v-model:value="draft.name"
          :disabled="mode === 'edit'"
          placeholder="如 works_for（必填 · 全局唯一 · ^[a-z_]+$）"
          class="mono"
          :status="nameMissing || nameBad || nameDup ? 'error' : undefined"
        />
        <div v-if="mode === 'edit'" class="mini">新建后不可改（R1 同款约束）</div>
      </div>

      <div class="lem-row">
        <label class="lem-label">title（显示名）</label>
        <NInput v-model:value="draft.title" placeholder="选填，如「任职于」" />
      </div>

      <div class="lem-pair">
        <div class="lem-row">
          <label class="lem-label">起点 from_obj</label>
          <NSelect v-model:value="draft.from_obj" :options="objectOptions" placeholder="选择对象" />
        </div>
        <div class="lem-row">
          <label class="lem-label">终点 to_obj</label>
          <NSelect v-model:value="draft.to_obj" :options="objectOptions" placeholder="选择对象" />
        </div>
      </div>

      <div class="lem-row">
        <label class="lem-label">基数 cardinality</label>
        <NSelect v-model:value="draft.cardinality" :options="cardinalityOptions" />
        <div class="mini">基数与实际数据不符时仅警告不阻止（取证数据常脏，D5；数据核查由后续批次接线）</div>
      </div>

      <div v-if="error" class="lem-error">{{ error }}</div>
      <div class="lem-note mini">
        link 级 <code>properties / endpoints / jian / jian_source</code> 等不在本表单，保存时原样保留（R2）。
      </div>
    </div>

    <template #footer>
      <div class="lem-footer">
        <NButton v-if="mode === 'edit'" quaternary type="error" :disabled="loading" @click="emit('delete')">
          删除关系
        </NButton>
        <div class="lem-footer-right">
          <NButton :disabled="loading" @click="emit('update:show', false)">取消</NButton>
          <NButton type="primary" :loading="loading" @click="confirm">确定</NButton>
        </div>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.lem-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 430px;
}
.lem-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.lem-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.lem-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.lem-lock {
  cursor: help;
  margin-left: 4px;
}
.lem-error {
  font-size: 12px;
  color: var(--sun-error-text);
}
.lem-note {
  border-top: 1px dashed var(--sun-border);
  padding-top: 8px;
}
.lem-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.lem-footer-right {
  display: flex;
  gap: 10px;
}
.mini {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.mono {
  font-family: var(--sun-font-mono);
}
</style>
