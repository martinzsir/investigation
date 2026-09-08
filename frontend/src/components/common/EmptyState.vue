<script setup lang="ts">
// FE-C-032 EmptyState：三类型视觉可区分——真的没有 / 无权限（锁）/ 出错（警告）。
// 红线：无权限态不得伪装成空态（ui_v1.3 红线一，防资源存在性推断）。
withDefaults(
  defineProps<{
    type: 'empty' | 'forbidden' | 'error'
    title: string
    desc?: string
  }>(),
  { desc: '' },
)
</script>

<template>
  <div class="empty-state" :class="`empty-state--${type}`">
    <div class="empty-icon" aria-hidden="true">
      <span v-if="type === 'empty'">▢</span>
      <span v-else-if="type === 'forbidden'">🔒</span>
      <span v-else>⚠</span>
    </div>
    <div class="empty-title">{{ title }}</div>
    <div v-if="desc" class="empty-desc">{{ desc }}</div>
    <div v-if="$slots.action" class="empty-action">
      <slot name="action" />
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 20px;
  text-align: center;
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
}
.empty-icon {
  font-size: 30px;
  line-height: 1;
}
.empty-state--empty .empty-icon {
  color: var(--sun-text-tertiary);
}
.empty-state--forbidden {
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
}
.empty-state--forbidden .empty-icon {
  color: var(--sun-warn-text);
}
.empty-state--error {
  border-color: var(--sun-error-border);
  background: var(--sun-error-bg);
}
.empty-state--error .empty-icon {
  color: var(--sun-error-text);
}
.empty-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--sun-text-primary);
}
.empty-desc {
  font-size: 12px;
  color: var(--sun-text-secondary);
  max-width: 420px;
}
.empty-action {
  margin-top: 6px;
}
</style>
