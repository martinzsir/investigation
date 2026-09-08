<script setup lang="ts">
// FE-C-010 MaskedField：无权限渲染 ****（fail-closed）；手机前 3 后 4、身份证前 6 后 4。
// 红线 FE-T-021：denied 态组件内永不接触真值渲染路径之外的展示——父级传入
// 真值仅在 policy=visible 时输出；denied/masked 输出不含原始片段。
import { computed } from 'vue'
import { maskField, type MaskPolicy } from '../../domain/clue'

const props = defineProps<{
  value: string
  policy: MaskPolicy
  type?: 'phone' | 'idcard' | 'text'
  /** 拒绝态补充说明（悬停可见） */
  deniedHint?: string
}>()

const text = computed(() => maskField(props.value, props.policy, props.type ?? 'text'))
</script>

<template>
  <span
    class="masked"
    :class="{ 'masked--denied': policy === 'denied', 'masked--masked': policy === 'masked' }"
    :title="policy === 'denied' ? (deniedHint ?? '无字段访问权限') : ''"
  >{{ text }}<span v-if="policy === 'denied'" class="lock" aria-hidden="true">🔒</span></span>
</template>

<style scoped>
.masked {
  font-family: var(--sun-font-mono);
  font-size: 12px;
}
.masked--masked {
  color: var(--sun-warn-text);
  letter-spacing: 0.5px;
}
.masked--denied {
  color: var(--sun-text-tertiary);
  user-select: none;
}
.lock {
  margin-left: 4px;
  font-size: 10px;
}
</style>
