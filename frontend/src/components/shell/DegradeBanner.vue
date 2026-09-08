<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { casesApi } from '../../api/endpoints/cases'
import { useCaseStore } from '../../stores/case'

// FE-C-027：降级通栏——诊断分级计数 + 详情入口。
// 降级时导出/立案按钮禁用且写明原因（FE-T-010）：相关按钮属 MVP-1 业务页，
// 届时经 presentError().degradeWrites + 本组件状态承接，MVP-0 仅承载展示与入口。
const cs = useCaseStore()

const critical = ref(0)
const warning = ref(0)
const info = ref(0)
const loaded = ref(false)

async function refresh(): Promise<void> {
  const cid = cs.currentCaseId
  if (!cid) {
    loaded.value = false
    return
  }
  try {
    const d = await casesApi.dashboard(cid)
    const by = d.by_severity ?? {}
    critical.value = by.critical ?? 0
    warning.value = by.warning ?? 0
    info.value = by.info ?? 0
    loaded.value = true
  } catch {
    // MVP-0：仪表盘读取失败静默（红线交互断言随 MVP-1 业务页落地）
    loaded.value = false
  }
}

onMounted(refresh)
watch(() => cs.currentCaseId, refresh)

const message = computed(
  () => `诊断告警：critical ${critical.value} · warning ${warning.value} · info ${info.value}`,
)

const isDegraded = computed(() => loaded.value && (critical.value > 0 || warning.value > 0))
</script>

<template>
  <NAlert v-if="isDegraded" type="warning" class="banner" :show-icon="true">
    {{ message }}
    <RouterLink to="/tasks">查看详情 →</RouterLink>
  </NAlert>
</template>

<style scoped>
.banner {
  margin-bottom: 12px;
}
</style>
