<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NAvatar, NButton, NDropdown, NIcon, NSelect, NTag } from 'naive-ui'
import {
  CloseOutline,
  HomeOutline,
  PersonOutline,
  SearchOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
  TimeOutline,
} from '@vicons/ionicons5'
import { useAuthStore } from '../../stores/auth'
import { useCaseStore } from '../../stores/case'
import { useHealthStore } from '../../stores/health'

// FE-C-025：全局条五元素（FE-C-024）——案件选择器 / 本体版本 / 任务入口 / 用户 / 设置。
// 视觉对齐 23b 案件门户：中部全局检索框，右侧用户名 + 内网涉密徽章 + 头像菜单。
const router = useRouter()
const cs = useCaseStore()
const auth = useAuthStore()
const health = useHealthStore()

const options = computed(() => [
  { label: '全部案件', value: '' },
  ...cs.cases.map((c) => ({ label: c.name, value: c.id })),
])

const versionText = computed(() => {
  const c = cs.currentCase
  if (!c) return '—'
  return c.pack_snapshot_at ? `${c.pack_id} · ${c.pack_snapshot_at.slice(0, 10)}` : c.pack_id
})

/** 全局检索：MVP-0 仅交互壳（可输入/清除），检索结果页随 MVP-1 门户落地 */
const keyword = ref('')

const userOptions = [
  { label: `密级 ${auth.clearance} · ${auth.role === 'human' ? '检察官' : auth.role}`, key: 'role', disabled: true },
  { type: 'divider', key: 'd1' },
  { label: '退出登录', key: 'logout' },
]

function onCaseChange(v: string): void {
  const id = String(v)
  cs.selectCase(id)
  // “全部案件”（id 为空）回案件门户页（FE-C-025 门户语义兑现，平台页 /cases）
  if (!id) void router.push('/cases')
}

async function onUserAction(key: string | number): Promise<void> {
  if (key === 'logout') {
    await auth.logout()
    cs.clearAll()
    await router.push('/login')
  }
}
</script>

<template>
  <div class="header">
    <!-- 左：案件上下文 -->
    <div class="left">
      <NSelect
        class="case-select"
        :value="cs.currentCaseId"
        :options="options"
        size="small"
        placeholder="选择案件"
        :loading="cs.loading"
        @update:value="(v) => onCaseChange(String(v))"
      />
      <NTag size="small" :bordered="true" title="本体快照（pack · 快照日期）">
        <span class="mono">本体 {{ versionText }}</span>
      </NTag>
      <NTag v-if="health.degraded" size="small" type="warning">元数据层降级</NTag>
    </div>

    <!-- 中：全局检索（23b） -->
    <div class="center">
      <div class="global-search">
        <NIcon :component="SearchOutline" class="gs-icon" />
        <input
          v-model="keyword"
          class="gs-input"
          type="text"
          placeholder="搜索案件、线索、人员或证据"
        />
        <button v-if="keyword" type="button" class="gs-clear" title="清空" @click="keyword = ''">
          <NIcon :component="CloseOutline" />
        </button>
      </div>
    </div>

    <!-- 右：门户/任务/设置 + 用户区（23b） -->
    <div class="right">
      <NButton quaternary circle size="small" title="案件门户" @click="router.push('/cases')">
        <NIcon :component="HomeOutline" />
      </NButton>
      <NButton quaternary circle size="small" title="任务中心" @click="router.push('/tasks')">
        <NIcon :component="TimeOutline" />
      </NButton>
      <NButton quaternary circle size="small" title="系统设置" @click="router.push('/settings')">
        <NIcon :component="SettingsOutline" />
      </NButton>
      <span class="user-name">{{ auth.operator || '未登录' }}</span>
      <span class="net-badge" title="本机处于公安内网环境，会话不出网">
        <NIcon :component="ShieldCheckmarkOutline" />
        内网涉密
      </span>
      <NDropdown :options="userOptions" trigger="click" @select="onUserAction">
        <NAvatar round size="small" class="user-avatar">
          <NIcon :component="PersonOutline" />
        </NAvatar>
      </NDropdown>
    </div>
  </div>
</template>

<style scoped>
.header {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 16px;
}
.left,
.right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}
.center {
  flex: 1;
  display: flex;
  justify-content: center;
  min-width: 0;
}
.case-select {
  width: 200px;
}

/* 全局检索框（23b） */
.global-search {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  max-width: 520px;
  height: 34px;
  padding: 0 10px;
  border-radius: 8px;
  background: var(--sun-input-bg);
  border: 1px solid var(--sun-border);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.global-search:focus-within {
  border-color: var(--sun-border-active);
  box-shadow: 0 0 0 3px rgba(110, 222, 233, 0.12);
}
.gs-icon {
  font-size: 16px;
  color: var(--sun-text-tertiary);
  flex: 0 0 auto;
}
.gs-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--sun-text-primary);
  font-size: 13px;
  font-family: inherit;
}
.gs-input::placeholder {
  color: var(--sun-text-tertiary);
}
.gs-clear {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--sun-text-tertiary);
  cursor: pointer;
  flex: 0 0 auto;
}
.gs-clear:hover {
  color: var(--sun-text-primary);
  background: var(--sun-bg-card-hover);
}

/* 用户区（23b） */
.user-name {
  margin-left: 4px;
  font-size: 13px;
  font-weight: 500;
  color: var(--sun-text-primary);
  white-space: nowrap;
}
.net-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 8px;
  border-radius: 12px;
  border: 1px solid rgba(110, 222, 233, 0.45);
  background: rgba(0, 212, 224, 0.08);
  color: var(--sun-border-active);
  font-size: 11px;
  white-space: nowrap;
}
.user-avatar {
  margin-left: 4px;
  background: rgba(110, 222, 233, 0.15);
  color: var(--sun-border-active);
  cursor: pointer;
  border: 1px solid rgba(110, 222, 233, 0.35);
}
</style>
