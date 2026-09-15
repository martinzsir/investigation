<script setup lang="ts">
// S2 保存危险确认弹窗（PRD §8.1/8.2 / 线框 F1）。
// 刻意不复用 ConfigConfirmDialog：后者固定文案含「保存后自动触发 RESCAN 重跑」——
// 仅 RuleWorkshop 真入队，对本体保存是 R3 红线假承诺。本组件按改动分级给事实文案，
// 绝不出现「已自动触发」字样（R3）。结构性/关系性 → 理由必填；语义性改动不进本弹窗。
import { computed } from 'vue'
import { NModal, NButton, NInput } from 'naive-ui'

const props = withDefaults(
  defineProps<{
    show: boolean
    /** 结构性改动清单（新增/删除对象、pk/kind/name_property、属性增删/type/composite） */
    structural: string[]
    /** 关系性改动清单（新增/删除/修改 link） */
    relational: string[]
    /** 语义性改动清单（title/jian/jian_source/数据元绑定）——仅展示，不需要确认 */
    semantic: string[]
    reason: string
    loading?: boolean
  }>(),
  { loading: false },
)

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'update:reason', v: string): void
  (e: 'confirm'): void
}>()

const dangerous = computed(() => props.structural.length > 0 || props.relational.length > 0)
const reasonMissing = computed(() => dangerous.value && !props.reason.trim())

function close(): void {
  emit('update:show', false)
}
function confirm(): void {
  if (reasonMissing.value || props.loading) return
  emit('confirm')
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="dangerous ? '⚠ 结构性 / 关系性改动确认' : '保存确认'"
    class="scm-modal"
    :mask-closable="false"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="scm-body" :class="{ 'scm-body--danger': dangerous }">
      <div class="scm-section">
        <div v-if="structural.length" class="scm-group">
          <div class="scm-group-title">结构性改动（改变机器语义）</div>
          <div v-for="s in structural" :key="s" class="scm-item">· {{ s }}</div>
        </div>
        <div v-if="relational.length" class="scm-group">
          <div class="scm-group-title">关系性改动</div>
          <div v-for="s in relational" :key="s" class="scm-item">· {{ s }}</div>
        </div>
        <div v-if="semantic.length" class="scm-group">
          <div class="scm-group-title">语义性改动（下次 BUILD 后生效）</div>
          <div v-for="s in semantic" :key="s" class="scm-item scm-item--soft">· {{ s }}</div>
        </div>
      </div>
      <div v-if="dangerous" class="scm-warn">
        结构性 / 关系性改动需重跑 BUILD 才会物化到 <code>obj_* / lnk_*</code>；
        保存本身不触发任何重跑（是否重跑由你在任务中心决定）。
      </div>
      <div class="scm-reason-row">
        <label class="scm-label">
          改动理由<span v-if="dangerous" class="scm-required">*（必填，落审计链）</span>
        </label>
        <NInput
          :value="reason"
          type="textarea"
          :rows="3"
          placeholder="请写明改动依据，例如：新增交易对手方实体以承载过桥账户分析"
          :status="reasonMissing ? 'error' : undefined"
          @update:value="(v: string) => emit('update:reason', v)"
        />
        <div v-if="reasonMissing" class="scm-error">结构性 / 关系性改动必须填写理由（审计留痕）</div>
      </div>
    </div>
    <template #footer>
      <div class="scm-footer">
        <NButton :disabled="loading" @click="close">取消</NButton>
        <NButton type="primary" :danger="dangerous" :loading="loading" :disabled="reasonMissing" @click="confirm">
          确认保存
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.scm-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 480px;
  max-width: 620px;
}
.scm-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 260px;
  overflow: auto;
}
.scm-group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-secondary);
  margin-bottom: 4px;
}
.scm-item {
  font-size: 12.5px;
  color: var(--sun-text-primary);
  padding-left: 8px;
}
.scm-item--soft {
  color: var(--sun-text-secondary);
}
.scm-warn {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.7;
}
.scm-reason-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.scm-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.scm-required {
  color: var(--sun-error-text);
}
.scm-error {
  font-size: 12px;
  color: var(--sun-error-text);
}
.scm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
code {
  background: var(--sun-bg-card);
  padding: 0 3px;
  border-radius: 3px;
}
</style>
