<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NResult } from 'naive-ui'
import DegradeBanner from './DegradeBanner.vue'
import GlobalHeader from './GlobalHeader.vue'
import SideNav from './SideNav.vue'
import { NETWORK_DOWN_TEXT } from '../../api/errors'
import { useAuthStore } from '../../stores/auth'
import { useCaseStore } from '../../stores/case'
import { useHealthStore } from '../../stores/health'

const health = useHealthStore()
const auth = useAuthStore()
const cs = useCaseStore()
const siderCollapsed = ref(false)

function probe(): void {
  void health.probe()
}

onMounted(() => {
  // FE-I-015：启动探针 + 网络恢复重探
  probe()
  window.addEventListener('online', probe)
  if (auth.isAuthenticated) void cs.loadCases()
})
</script>

<template>
  <NLayout position="absolute" has-sider>
    <NLayoutSider
      v-model:collapsed="siderCollapsed"
      bordered
      collapse-mode="width"
      :collapsed-width="64"
      :width="220"
      show-trigger
    >
      <SideNav :collapsed="siderCollapsed" />
    </NLayoutSider>
    <NLayout>
      <NLayoutHeader bordered>
        <GlobalHeader />
      </NLayoutHeader>
      <NLayoutContent content-style="padding: 12px; height: calc(100vh - 56px); overflow: auto;">
        <template v-if="health.state === 'unreachable'">
          <!-- FE-I-015：服务端不可达 → 全局异常态，不发业务请求 -->
          <NResult status="500" :title="NETWORK_DOWN_TEXT" desc="请检查服务端是否启动，网络恢复后将自动重试" />
        </template>
        <template v-else>
          <DegradeBanner v-if="cs.currentCaseId" />
          <RouterView />
        </template>
      </NLayoutContent>
    </NLayout>
  </NLayout>
</template>
