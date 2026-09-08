<script setup lang="true">
// ★ FE-C-015 PolicyGate（承重墙，薄）：对象/操作级权限闸门。
// 未声明/不通过 → fail-closed：默认插槽不渲染（或渲染 fallback）。
// 组件本身不做策略判定（判定在 domain 层 + 后端 403/404 双保险），
// 仅作为"不渲染即不存在"的统一载体（FE-C-014 门禁序：不渲染 > 置灰 > 报错）。
withDefaults(
  defineProps<{
    allow: boolean
    /** 不通过时是否渲染 fallback 插槽（默认不渲染任何内容） */
    showFallback?: boolean
  }>(),
  { showFallback: false },
)
</script>

<template>
  <slot v-if="allow" />
  <slot v-else-if="showFallback" name="fallback" />
</template>
