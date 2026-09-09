<script setup lang="ts">
// FE-P-006 审计链：完整性校验常驻条 + 时间线 + 筛选 + 维度切换（案件级/线索级）。
// 红线 FE-T-015：空链 warn 语义（auditBanner 强制），不显示「校验通过」。
// 维度切换（p7 技术债清偿）：线索级经 ?clue_id= query 参数进入（图谱/工作台跳转同参），
// 切换回案件级即清空线索维度并重新加载；verify 完整性校验始终为案件级。
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NButton, NInput, NSelect, NAlert, NIcon, NRadioGroup, NRadioButton } from 'naive-ui'
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
const router = useRouter()

const loading = ref(false)
const errorMsg = ref('')
const page = ref<AuditPage | null>(null)
const verify = ref<AuditVerifyDto | null>(null)

const actionFilter = ref<string | null>(null)
const operatorFilter = ref('')
const clueIdFilter = ref(typeof route.query.clue_id === 'string' ? route.query.clue_id : '')
// 维度：带 ?clue_id= 进入即落线索级（外页跳转直达）
const dimension = ref<'case' | 'clue'>(clueIdFilter.value ? 'clue' : 'case')

const actionOptions = [
  { label: '全部动作', value: '' },
  { label: '线索处置', value: 'disposal' },
  { label: '建议处置', value: 'proposal' },
  { label: '参数调整', value: 'parameter_set' },
  { label: '其他', value: 'generic' },
]

const banner = computed(() => auditBanner(verify.value))
const items = computed(() => page.value?.items ?? [])

const clueQuery = computed(() => (dimension.value === 'clue' ? clueIdFilter.value.trim() : ''))

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    page.value = null
    verify.value = null
    return
  }
  loading.value = true
  errorMsg.value = ''
  // URL 同步：线索级保留 ?clue_id=，案件级移除（可分享/可回跳）
  void router.replace({ query: { ...route.query, clue_id: clueQuery.value || undefined } })
  try {
    const [p, v] = await Promise.all([
      auditApi.timeline(cs.currentCaseId, {
        page: 1,
        page_size: 100,
        action: actionFilter.value ?? undefined,
        operator: operatorFilter.value.trim() || undefined,
        clue_id: clueQuery.value || undefined,
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

// 外页带 ?clue_id= 跳转（图谱/庙算）→ 同步到线索级
watch(
  () => route.query.clue_id,
  (q) => {
    if (typeof q === 'string' && q && q !== clueIdFilter.value) {
      clueIdFilter.value = q
      dimension.value = 'clue'
      void load()
    }
  },
)

function switchDimension(d: 'case' | 'clue'): void {
  if (dimension.value === d) return
  dimension.value = d
  if (d === 'case') {
    clueIdFilter.value = ''
    void load()
  }
}

function reset(): void {
  actionFilter.value = null
  operatorFilter.value = ''
  clueIdFilter.value = ''
  dimension.value = 'case'
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

          <!-- 维度切换：案件级（全案时间线）/ 线索级（?clue_id= 直达） -->
          <div class="dim-bar">
            <NRadioGroup :value="dimension" size="small" @update:value="switchDimension">
              <NRadioButton value="case">案件级</NRadioButton>
              <NRadioButton value="clue">线索级</NRadioButton>
            </NRadioGroup>
            <span class="dim-hint">
              {{ dimension === 'case' ? '全案审计时间线（完整性校验为案件级）' : '仅展示该线索相关审计记录（可从图谱/工作台带 ?clue_id= 直达）' }}
            </span>
          </div>

          <!-- 筛选（动作 + 操作人；线索级额外按线索 ID 过滤） -->
          <div class="filters">
            <NSelect v-model:value="actionFilter" :options="actionOptions" placeholder="全部动作" class="filter-select" />
            <NInput v-model:value="operatorFilter" placeholder="操作人姓名" class="filter-input" />
            <NInput
              v-if="dimension === 'clue'"
              v-model:value="clueIdFilter"
              placeholder="线索 ID，如 CLUE-007"
              class="filter-clue"
              @keyup.enter="load"
            />
            <NButton size="small" type="primary" :disabled="dimension === 'clue' && !clueIdFilter.trim()" @click="load">查询</NButton>
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
.dim-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.dim-hint {
  font-size: 12px;
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
