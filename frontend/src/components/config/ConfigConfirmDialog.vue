<script setup lang="ts">
// 配置写统一确认入口（FE-T-012）：
// - 普通变更（如纯判据文本）：普通确认，理由可选；
// - 危险变更（params/enabled/权限/模型结构等改变机器行为）：🔴 红框警示，
//   变更理由必填（落案件审计链 note），未填禁用确认；
// - 对话框只承载确认与理由采集；是否危险由 domain 纯函数判定后传入。
import { computed } from 'vue'
import { NModal, NButton, NInput } from 'naive-ui'

const props = withDefaults(
  defineProps<{
    show: boolean
    dangerous: boolean
    title: string
    /** 变更摘要（展示用，如「R6 · 阈值/启停变更：window_minutes 60→30」） */
    detail?: string
    reason: string
    loading?: boolean
    /** 危险项理由输入框占位提示 */
    reasonPlaceholder?: string
  }>(),
  { detail: '', loading: false, reasonPlaceholder: '' },
)

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'update:reason', v: string): void
  (e: 'confirm'): void
}>()

const reasonMissing = computed(() => props.dangerous && !props.reason.trim())

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
    :title="title"
    class="ccd-modal"
    :mask-closable="false"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="ccd-body" :class="{ 'ccd-body--danger': dangerous }">
      <div v-if="dangerous" class="ccd-warn">
        ⚠ 本次变更将改变机器行为（阈值/启停/权限/模型结构），保存后自动触发 RESCAN 重跑，
        且变更将记入案件审计链（谁/何时/改了什么/理由，不可删除）。
      </div>
      <div v-else class="ccd-note">
        本次为说明性文本修订，不改变机器行为、不触发重跑；仍会记入审计链。
      </div>
      <div v-if="detail" class="ccd-detail">变更内容：{{ detail }}</div>
      <div class="ccd-reason-row">
        <label class="ccd-label">
          变更理由<span v-if="dangerous" class="ccd-required">*（必填）</span><span v-else>（可选）</span>
        </label>
        <NInput
          :value="reason"
          type="textarea"
          :rows="3"
          :placeholder="reasonPlaceholder || (dangerous ? '请写明变更依据与预期效果，例如：近 90 日基线复核后收紧时间窗至 30 分钟' : '可填写修订说明')"
          :status="reasonMissing ? 'error' : undefined"
          @update:value="(v: string) => emit('update:reason', v)"
        />
        <div v-if="reasonMissing" class="ccd-error">机器行为变更必须填写变更理由（审计留痕）</div>
      </div>
    </div>
    <template #footer>
      <div class="ccd-footer">
        <NButton :disabled="loading" @click="close">取消</NButton>
        <NButton type="primary" :danger="dangerous" :loading="loading" :disabled="reasonMissing" @click="confirm">
          {{ dangerous ? '确认变更并留痕' : '确认保存' }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.ccd-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 420px;
}
.ccd-warn {
  background: var(--sun-error-bg);
  border: 1px solid var(--sun-error-border);
  color: var(--sun-error-text);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.7;
}
.ccd-note {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  color: var(--sun-text-secondary);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
}
.ccd-detail {
  font-size: 13px;
  font-weight: 600;
}
.ccd-reason-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ccd-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.ccd-required {
  color: var(--sun-error-text);
}
.ccd-error {
  font-size: 12px;
  color: var(--sun-error-text);
}
.ccd-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
