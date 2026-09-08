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
// 导航形态：左栏（用户决策，2026-09-08）；品牌区对齐 23b 青色 Z 标。
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

const activeKey = computed(() => {
  // 线索详情页高亮「线索列表」
  if (route.path.startsWith('/c/clue/')) return 'clues'
  return String(route.params.section ?? '')
})
</script>

<template>
  <div class="sidenav">
    <div class="brand" :class="{ 'brand--collapsed': collapsed }">
      <!-- 23b 品牌标：青色 Z（渐变填充，纯 SVG 内联，零外链） -->
      <svg class="brand-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <defs>
          <linearGradient id="brandZ" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
            <stop stop-color="#00E6D4" />
            <stop offset="1" stop-color="#1E96FF" />
          </linearGradient>
        </defs>
        <path
          d="M3 3 H29 V9 L12 23 H29 V29 H3 V23 L20 9 H3 Z"
          fill="url(#brandZ)"
        />
      </svg>
      <span v-if="!collapsed" class="brand-text">孙武侦查官</span>
    </div>
    <NMenu
      :collapsed="collapsed"
      :options="options"
      :value="activeKey"
      :collapsed-width="64"
      :collapsed-icon-size="20"
      class="nav-menu"
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
  gap: 10px;
  padding: 0 16px;
  border-bottom: 1px solid var(--sun-border);
}
.brand--collapsed {
  justify-content: center;
  padding: 0;
}
.brand-mark {
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  filter: drop-shadow(0 0 6px rgba(0, 229, 211, 0.55));
}
.brand-text {
  font-weight: 700;
  font-size: 16px;
  letter-spacing: 1px;
  color: var(--sun-border-active);
  white-space: nowrap;
  text-shadow: 0 0 12px rgba(0, 229, 211, 0.35);
}

/* 选中项：左侧青色指示条（对齐原型卡片左侧竖条语言） */
.nav-menu :deep(.n-menu-item-content--selected) {
  position: relative;
}
.nav-menu :deep(.n-menu-item-content--selected)::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  bottom: 7px;
  width: 3px;
  border-radius: 0 2px 2px 0;
  background: var(--sun-border-active);
  box-shadow: 0 0 8px rgba(110, 222, 233, 0.7);
  pointer-events: none;
}
.nav-menu :deep(.n-sub-menu .n-menu-item-content__icon) {
  color: var(--sun-text-secondary);
}
</style>
