<script setup lang="ts">
import { computed, h, type Component } from 'vue'
import { NIcon, NMenu } from 'naive-ui'
import { RouterLink, useRoute } from 'vue-router'
import type { MenuOption } from 'naive-ui'
import {
  AnalyticsOutline,
  BuildOutline,
  CloudUploadOutline,
  HardwareChipOutline,
  PulseOutline,
  ShieldCheckmarkOutline,
} from '@vicons/ionicons5'
import { NAV_GROUPS } from '../../nav/sections'

// FE-C-026：六分组导航（研判中心/数据接入/数据治理/研判模型/治理审计/高级工具）；
// 平台页（23b/24/25）不入侧栏。图标经 @vicons 构建期内联（FE-I-003）。
const route = useRoute()

defineProps<{ collapsed?: boolean }>()

const GROUP_ICONS: Record<string, Component> = {
  research: AnalyticsOutline,
  ingest: CloudUploadOutline,
  gov: BuildOutline,
  model: PulseOutline,
  audit: ShieldCheckmarkOutline,
  tools: HardwareChipOutline,
}

function renderIcon(key: string) {
  const icon = GROUP_ICONS[key]
  return () => h(NIcon, null, { default: () => h(icon) })
}

const options: MenuOption[] = NAV_GROUPS.map((g) => ({
  label: g.label,
  key: `g-${g.key}`,
  icon: renderIcon(g.key),
  children: g.items.map((it) => ({
    label: () => h(RouterLink, { to: `/c/${it.key}` }, { default: () => it.label }),
    key: it.key,
  })),
}))

const activeKey = computed(() => String(route.params.section ?? ''))
</script>

<template>
  <div class="sidenav">
    <div class="brand">
      <span v-if="!collapsed" class="brand-text">孙武侦查官</span>
      <span v-else class="brand-text mono">孙</span>
    </div>
    <NMenu
      :collapsed="collapsed"
      :options="options"
      :value="activeKey"
      :collapsed-width="64"
      :collapsed-icon-size="20"
    />
  </div>
</template>

<style scoped>
.sidenav {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.brand {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid var(--sun-border);
}
.brand-text {
  font-weight: 700;
  font-size: 15px;
  color: var(--sun-text-primary);
  white-space: nowrap;
}
</style>
