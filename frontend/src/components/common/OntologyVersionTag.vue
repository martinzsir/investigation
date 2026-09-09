<script setup lang="ts">
// FE-C-021 OntologyVersionTag：案件包版本锚点标签。
// pack_snapshot_at 非空 = 建案后已 BUILD 锁定（绿）；空 = 版本未锁定（橙，
// 首次 BUILD 后锁定，锁定后规则/阈值变更不影响在办案件）。
import { computed } from 'vue'
import { NTag, NTooltip } from 'naive-ui'

const props = defineProps<{
  packId: string
  /** CaseDto.pack_snapshot_at（'' = 未锁定） */
  snapshotAt?: string
}>()

const locked = computed(() => Boolean(props.snapshotAt))
const shortAt = computed(() => {
  const v = props.snapshotAt ?? ''
  return v.length > 16 ? v.slice(0, 16).replace('T', ' ') : v.replace('T', ' ')
})
</script>

<template>
  <NTooltip trigger="hover">
    <template #trigger>
      <NTag size="tiny" :bordered="false" :type="locked ? 'success' : 'warning'">
        <span class="ovt">
          <span class="ovt-icon">{{ locked ? '🔒' : '🔓' }}</span>
          案件包 {{ packId }}
          <span v-if="locked" class="ovt-at">· {{ shortAt }}</span>
          <span v-else class="ovt-unlocked">· 版本未锁定</span>
        </span>
      </NTag>
    </template>
    <span v-if="locked">案件包版本已于 {{ snapshotAt }} 锁定；此后规则/阈值修订不影响本案件</span>
    <span v-else>案件包版本尚未锁定：首次 BUILD 语义层后锁定，锁定前规则修订仍会生效</span>
  </NTooltip>
</template>

<style scoped>
.ovt {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--sun-font-mono);
  font-size: 11px;
}
.ovt-at,
.ovt-unlocked {
  font-family: var(--sun-font-mono);
  opacity: 0.85;
}
</style>
