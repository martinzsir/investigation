<script setup lang="ts">
// RC-202 人工节点（侦查假设/备注）新增/编辑表单。
// 纯展示 + 校验（domain 纯函数，与后端 server/app/canvas_edit.py 同口径），
// 提交内容上抛父组件，请求与版本处理在 ResearchCanvas。
import { computed, reactive, ref, watch } from 'vue'
import { NButton, NInput, NModal } from 'naive-ui'
import {
  LIMITS,
  validateManualNode,
  type CanvasNode,
  type ManualNodeFormErrors,
  type ManualNodeKind,
} from '../../domain/canvas'

const props = defineProps<{
  show: boolean
  /** create=新增（服务端签 id）；edit=编辑（node 必传） */
  mode: 'create' | 'edit'
  kind: ManualNodeKind
  node?: CanvasNode | null
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'submit', payload: { kind: ManualNodeKind; title: string; content: string }): void
}>()

const form = reactive({ title: '', content: '' })
const errors = ref<ManualNodeFormErrors>({})
const submitting = ref(false)

const isHypothesis = computed(() => props.kind === 'hypothesis')
const modalTitle = computed(() => {
  const noun = isHypothesis.value ? '侦查假设' : '备注'
  return props.mode === 'create' ? `添加${noun}` : `编辑${noun}`
})

watch(
  () => [props.show, props.mode, props.kind, props.node?.id] as const,
  ([visible]) => {
    if (!visible) return
    errors.value = {}
    submitting.value = false
    if (props.mode === 'edit' && props.node) {
      const p = props.node.props ?? {}
      form.title = String(p.title ?? '')
      form.content = String(p.content ?? '')
    } else {
      form.title = ''
      form.content = ''
    }
  },
  { immediate: true },
)

function close(): void {
  emit('update:show', false)
}

function onSubmit(): void {
  const errs = validateManualNode(props.kind, {
    title: form.title,
    content: form.content,
  })
  errors.value = errs
  if (Object.keys(errs).length > 0) return
  submitting.value = true
  emit('submit', {
    kind: props.kind,
    title: form.title.trim(),
    content: form.content.trim(),
  })
}

defineExpose({
  /** 请求失败时父组件解锁按钮（成功后弹窗会被父组件关闭） */
  unlock: () => {
    submitting.value = false
  },
})
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="modalTitle"
    class="manual-node-modal"
    :style="{ width: '480px' }"
    :mask-closable="false"
    data-testid="manual-node-modal"
    @update:show="emit('update:show', $event)"
  >
    <div class="form">
      <div v-if="isHypothesis" class="field">
        <label class="label" for="manual-node-title">
          假设标题 <span class="req">*</span>
        </label>
        <NInput
          id="manual-node-title"
          v-model:value="form.title"
          :maxlength="LIMITS.hypothesisTitle + 20"
          placeholder="一句话概括待验证假设（1-50 字）"
          data-testid="manual-node-title"
          :status="errors.title ? 'error' : undefined"
        />
        <div class="hint">
          <span v-if="errors.title" class="err" data-testid="manual-node-title-err">
            {{ errors.title }}
          </span>
          <span v-else class="counter">{{ form.title.length }}/{{ LIMITS.hypothesisTitle }}</span>
        </div>
      </div>

      <div class="field">
        <label class="label" for="manual-node-content">
          {{ isHypothesis ? '假设内容' : '备注内容' }} <span class="req">*</span>
        </label>
        <NInput
          id="manual-node-content"
          v-model:value="form.content"
          type="textarea"
          :rows="5"
          :maxlength="LIMITS.content + 50"
          :placeholder="isHypothesis
            ? '支撑该假设的事实、推断逻辑与待核实点（1-500 字）'
            : '补充说明内容（1-500 字）'"
          data-testid="manual-node-content"
          :status="errors.content ? 'error' : undefined"
        />
        <div class="hint">
          <span v-if="errors.content" class="err" data-testid="manual-node-content-err">
            {{ errors.content }}
          </span>
          <span v-else class="counter">{{ form.content.length }}/{{ LIMITS.content }}</span>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="footer">
        <NButton size="small" @click="close">取消</NButton>
        <NButton
          size="small"
          type="primary"
          :loading="submitting"
          data-testid="manual-node-submit"
          @click="onSubmit"
        >
          {{ mode === 'create' ? '添加' : '保存' }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.req {
  color: #d05656;
}
.hint {
  display: flex;
  justify-content: flex-end;
  min-height: 16px;
}
.err {
  font-size: 11px;
  color: #d05656;
}
.counter {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
