<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { NCard, NEmpty, NTag } from 'naive-ui'
import { findSection } from '../nav/sections'
import { useCaseStore } from '../stores/case'

// MVP-0 砍掉所有业务页面：占位页标注归属里程碑（债务台账显式记账）
const route = useRoute()
const cs = useCaseStore()

const info = computed(() => {
  if (route.meta.title) {
    return { title: String(route.meta.title), mvp: Number(route.meta.mvp ?? 0) }
  }
  const key = String(route.params.section ?? '')
  const found = findSection(key)
  return found ? { title: found.item.label, mvp: found.item.mvp } : { title: '页面', mvp: 0 }
})
</script>

<template>
  <NCard :title="info.title">
    <template #header-extra>
      <NTag size="small" type="info">MVP-{{ info.mvp }} 交付</NTag>
    </template>
    <NEmpty description="该页面尚未纳入当前里程碑（MVP-0 仅交付外壳与访问层）" />
    <p v-if="cs.currentCase" class="case-hint">
      当前案件：<span class="mono">{{ cs.currentCase.name }}</span>
    </p>
  </NCard>
</template>

<style scoped>
.case-hint {
  margin-top: 12px;
  color: var(--sun-text-secondary);
}
</style>
