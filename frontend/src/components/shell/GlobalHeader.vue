<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NDropdown, NIcon, NSelect, NTag, NText } from 'naive-ui'
import { PersonCircleOutline, SettingsOutline, TimeOutline } from '@vicons/ionicons5'
import { useAuthStore } from '../../stores/auth'
import { useCaseStore } from '../../stores/case'
import { useHealthStore } from '../../stores/health'

// FE-C-025：全局条五元素（FE-C-024）——案件选择器 / 本体版本 / 任务入口 / 用户 / 设置
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

function roleLabel(r: string): string {
  const map: Record<string, string> = { system: '系统', human: '检察官' }
  return map[r] ?? r
}

const userText = computed(() =>
  auth.operator ? `${auth.operator} · ${roleLabel(auth.role)} · 密级 ${auth.clearance}` : '未登录',
)

const userOptions = [{ label: '退出登录', key: 'logout' }]

function onCaseChange(v: string): void {
  cs.selectCase(String(v))
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
    <div class="right">
      <NText class="user-text" depth="3">{{ userText }}</NText>
      <NButton quaternary circle size="small" title="任务中心" @click="router.push('/tasks')">
        <NIcon :component="TimeOutline" />
      </NButton>
      <NButton quaternary circle size="small" title="系统设置" @click="router.push('/settings')">
        <NIcon :component="SettingsOutline" />
      </NButton>
      <NDropdown :options="userOptions" @select="onUserAction">
        <NButton quaternary circle size="small" title="用户菜单">
          <NIcon :component="PersonCircleOutline" />
        </NButton>
      </NDropdown>
    </div>
  </div>
</template>

<style scoped>
.header {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  gap: 12px;
}
.left,
.right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.case-select {
  width: 240px;
}
.user-text {
  font-size: 12px;
  white-space: nowrap;
}
</style>
