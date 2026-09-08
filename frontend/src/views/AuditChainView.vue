<script setup lang="ts">
// FE-P-006 审计链（MVP-1 简化版）：完整性校验常驻条 + 时间线 + 筛选。
// 红线 FE-T-015：空链 warn 语义（auditBanner 强制），不显示「校验通过」。
// 06 设计图的节点详情中列/维度切换器属 MVP-2（技术债台账记账）。
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { NSpin, NButton, NInput, NSelect, NAlert, NIcon } from 'naive-ui'
import { ShieldCheckmarkOutline, WarningOutline } from '@vicons/ionicons5'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { auditApi, type AuditPage, type AuditVerifyDto } from '../api/endpoints/audit'
import { auditBanner } from '../domain/clue'
import EmptyState from '../components/common/EmptyState.vue'
import AuditTimeline from '../components/audit/AuditTimeline.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const route = useRoute()

const loading = ref(false)
const errorMsg = ref('')
const page = ref<AuditPage | null>(null)
const verify = ref<AuditVerifyDto | null>(null)

const actionFilter = ref<string | null>(null)
const operatorFilter = ref('')
const clueIdFilter = ref(typeof route.query.clue_id === 'string' ? route.query.clue_id : '')

const actionOptions = [
  { label: '全部动作', value: '' },
  { label: '线索处置', value: 'disposal' },
  { label: '建议处置', value: 'proposal' },
  { label: '参数调整', value: 'parameter_set' },
  { label: '其他', value: 'generic' },
]

const banner = computed(() => auditBanner(verify.value))
const items = computed(() => page.value?.items ?? [])

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    page.value = null
    verify.value = null
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    const [p, v] = await Promise.all([
      auditApi.timeline(cs.currentCaseId, {
        page: 1,
        page_size: 100,
        action: actionFilter.value ?? undefined,
        operator: operatorFilter.value.trim() || undefined,
        clue_id: clueIdFilter.value.trim() || undefined,
      }),
      auditApi.verify(cs.currentCaseId),
    ])
    page.value = p
    verify.value = v
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '审计链加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

function reset(): void {
  actionFilter.value = null
  operatorFilter.value = ''
  clueIdFilter.value = ''
  void load()
}
</script>

<template>
  <div class="page">
    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="审计链按案件归属，在顶部案件选择器中选择后加载"
    />
    <template v-else>
      <div class="page-head">
        <h2>审计链</h2>
        <span class="case-name">{{ cs.currentCase?.name ?? cs.currentCaseId }}</span>
      </div>

      <NSpin :show="loading">
        <EmptyState v-if="errorMsg" type="error" title="审计链加载失败" :desc="errorMsg">
          <template #action><NButton size="small" @click="load">重试</NButton></template>
        </EmptyState>

        <template v-else>
          <!-- 完整性校验常驻条（三徽章：校验结果 / 记录总数 / 链源） -->
          <div class="verify-bar" :class="`verify-bar--${banner.tone}`">
            <span class="verify-badge">
              <NIcon :component="banner.tone === 'ok' ? ShieldCheckmarkOutline : WarningOutline" />
              {{ banner.title }}
            </span>
            <span class="verify-detail">{{ banner.detail }}</span>
            <span class="verify-meta">
              <b>记录总数 {{ verify?.actual_count ?? 0 }}</b>
              <em class="chain-src">链源：{{ verify?.chain_source === 'version' ? 'DuckDB 历史链' : '活状态链' }}</em>
            </span>
          </div>

          <!-- 筛选（简化版：动作 + 操作人 + 线索维度） -->
          <div class="filters">
            <NSelect v-model:value="actionFilter" :options="actionOptions" placeholder="全部动作" class="filter-select" />
            <NInput v-model:value="operatorFilter" placeholder="操作人姓名" class="filter-input" />
            <NInput v-model:value="clueIdFilter" placeholder="线索 ID（维度切换：案件级/线索级）" class="filter-clue" />
            <NButton size="small" type="primary" @click="load">查询</NButton>
            <NButton size="small" @click="reset">重置</NButton>
          </div>

          <NAlert v-if="items.length === 0" type="warning" class="empty-note" :bordered="false">
            审计链为空（0 条记录）。平台案件无合法空链场景：请确认案件已运行 BUILD/RESCAN 或调整筛选条件。
          </NAlert>

          <AuditTimeline :items="items" :self-operator="auth.operator" />
        </template>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.page-head h2 {
  margin: 0;
  font-size: 18px;
}
.case-name {
  font-size: 13px;
  color: var(--sun-text-tertiary);
}
.verify-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  border: 1px solid;
  border-radius: 6px;
  padding: 10px 14px;
  font-size: 13px;
}
.verify-bar--ok {
  border-color: var(--sun-ok-border);
  background: var(--sun-ok-bg);
  color: var(--sun-ok-text);
}
.verify-bar--warn {
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
  color: var(--sun-warn-text);
}
.verify-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 700;
}
.verify-detail {
  color: var(--sun-text-secondary);
  font-size: 12px;
}
.verify-meta {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 12px;
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.chain-src {
  font-style: normal;
  color: var(--sun-text-tertiary);
}
.filters {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.filter-select {
  width: 160px;
}
.filter-input {
  width: 160px;
}
.filter-clue {
  width: 320px;
}
.empty-note {
  border-radius: 6px;
}
</style>
