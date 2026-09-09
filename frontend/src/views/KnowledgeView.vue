<script setup lang="ts">
// 知识包（MVP-4，/c/knowledge）。
// 主体别名 + 关系断言（R5 工商利益关联唯一事实源，functions.json 不写人名）；
// 过期断言（valid_until 早于今天）扫描自动排除，页面置灰明示；
// 断言变更影响 R5 检测输入 → 🔴 危险确认 + 理由必填，保存后需 RESCAN。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NInput, NTag, NSelect, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { knowledgeApi, type RelationAssertion } from '../api/endpoints/knowledge'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const assertions = ref<RelationAssertion[]>([])
const aliases = ref<Record<string, string[]>>({})

const canWrite = computed(() => canWriteConfig(auth.clearance))

const TYPE_OPTIONS = [
  { label: '法定代表人（legal_rep）', value: 'legal_rep' },
  { label: '利益关联（interest）', value: 'interest' },
  { label: '配偶（spouse）', value: 'spouse' },
  { label: '亲属（relative）', value: 'relative' },
  { label: '其他（other）', value: 'other' },
]

const TYPE_LABEL: Record<string, string> = {
  legal_rep: '法定代表人', interest: '利益关联', spouse: '配偶', relative: '亲属', other: '其他',
}

// 追加表单
const form = ref({ from: '', to: '', type: 'interest', source: '', validUntil: '' })
const adding = ref(false)

// 确认框
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
const confirmMode = ref<'add' | 'save'>('add')

function isExpired(a: RelationAssertion): boolean {
  return Boolean(a.valid_until) && a.valid_until! < new Date().toISOString().slice(0, 10)
}

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    assertions.value = []
    aliases.value = {}
    return
  }
  loading.value = true
  try {
    const doc = await knowledgeApi.list(cs.currentCaseId)
    assertions.value = doc.relation_assertions ?? []
    aliases.value = doc.subject_aliases ?? {}
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const formError = computed(() => {
  if (!form.value.from.trim() || !form.value.to.trim()) return '关系双方不能为空'
  if (!form.value.source.trim()) return '请填写来源（工商内档/招投标档案等）'
  if (form.value.validUntil && !/^\d{4}-\d{2}-\d{2}$/.test(form.value.validUntil)) {
    return '有效期格式应为 YYYY-MM-DD（留空=长期有效）'
  }
  return ''
})

function askAdd(): void {
  if (formError.value) {
    message.error(formError.value)
    return
  }
  confirmMode.value = 'add'
  confirmReason.value = ''
  confirmOpen.value = true
}

async function doConfirm(): Promise<void> {
  if (!cs.currentCaseId) return
  confirmSaving.value = true
  try {
    if (confirmMode.value === 'add') {
      const assertion: RelationAssertion = {
        from: form.value.from.trim(),
        to: form.value.to.trim(),
        type: form.value.type,
        source: form.value.source.trim(),
        valid_until: form.value.validUntil || null,
      }
      await knowledgeApi.add(cs.currentCaseId, {
        relation_assertions: [assertion],
        reason: confirmReason.value,
      })
      message.success('断言已追加并留痕；R5 结果需 RESCAN 后刷新')
      form.value = { from: '', to: '', type: 'interest', source: '', validUntil: '' }
    }
    confirmOpen.value = false
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}

const aliasText = ref('')
watch(aliases, (v) => {
  aliasText.value = JSON.stringify(v, null, 2)
}, { immediate: true })

const activeCount = computed(() => assertions.value.filter((a) => !isExpired(a)).length)
const expiredCount = computed(() => assertions.value.length - activeCount.value)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>知识包</h2>
      <p class="dim hint">
        案件主体别名与关系断言——R5 工商利益关联的唯一事实源（检测器代码不写人名）。
        过期断言自动失效、不进扫描。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="知识包按案件归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 关系断言是人工录入的案件知识，不是机器结论；变更影响 R5 检测输入，保存后需 RESCAN 才生效。
      </div>

      <NSpin :show="loading">
        <!-- 追加断言 -->
        <div class="card">
          <div class="card-title">追加关系断言</div>
          <div class="form-grid">
            <NInput v-model:value="form.from" size="small" placeholder="主体（如：蓝海贸易有限公司）" />
            <NInput v-model:value="form.to" size="small" placeholder="关联方（如：张某）" />
            <NSelect v-model:value="form.type" size="small" :options="TYPE_OPTIONS" />
            <NInput v-model:value="form.source" size="small" placeholder="来源（工商内档/招投标档案…）" />
            <NInput v-model:value="form.validUntil" size="small" placeholder="有效期 YYYY-MM-DD（留空=长期）" />
            <NButton type="primary" size="small" :disabled="!canWrite || !!formError" :loading="adding" @click="askAdd">
              {{ canWrite ? '追加断言' : '🔒 需偏将及以上' }}
            </NButton>
          </div>
          <div v-if="formError" class="field-warn">{{ formError }}</div>
        </div>

        <!-- 断言表 -->
        <div class="card">
          <div class="card-title">
            关系断言（{{ activeCount }} 有效 / {{ expiredCount }} 过期）
          </div>
          <div class="grid-wrap">
            <table class="grid">
              <thead>
                <tr><th>主体</th><th>关联方</th><th>关系</th><th>来源</th><th>有效期</th></tr>
              </thead>
              <tbody>
                <tr v-for="(a, i) in assertions" :key="i" :class="{ expired: isExpired(a) }">
                  <td>{{ a.from }}</td>
                  <td>{{ a.to }}</td>
                  <td><NTag size="tiny" :bordered="false">{{ TYPE_LABEL[a.type] ?? a.type }}</NTag></td>
                  <td class="dim">{{ a.source ?? '' }}</td>
                  <td>
                    {{ a.valid_until ?? '长期' }}
                    <span v-if="isExpired(a)" class="expired-tag">已过期·扫描排除</span>
                  </td>
                </tr>
                <tr v-if="!assertions.length">
                  <td colspan="5" class="dim empty-row">暂无关系断言</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 主体别名 -->
        <div class="card">
          <div class="card-title">主体别名（名称书写变体归一）</div>
          <p class="dim alias-hint">别名影响实体归并；改别名同样需要 RESCAN。整包 JSON 编辑：</p>
          <NInput v-model:value="aliasText" type="textarea" :rows="6" class="mono" :disabled="!canWrite" />
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="true"
      :detail="`追加断言：${form.from} → ${form.to}（${TYPE_LABEL[form.type] ?? form.type}），R5 重跑后生效`"
      :loading="confirmSaving"
      title="知识包变更确认"
      @confirm="doConfirm"
    />
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.hint { font-size: 12px; margin: 4px 0 0; }
.notice-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.card-title { font-size: 13px; font-weight: 600; }
.form-grid {
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px;
}
.form-grid :deep(.n-input), .form-grid :deep(.n-select) { width: 100%; }
@media (max-width: 1100px) { .form-grid { grid-template-columns: repeat(2, 1fr); } }
.field-warn { font-size: 12px; color: var(--sun-warn-text); }
.grid-wrap { overflow: auto; border: 1px solid var(--sun-border); border-radius: 4px; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; white-space: nowrap; }
tr.expired { color: var(--sun-text-tertiary); background: var(--sun-border); }
.expired-tag {
  margin-left: 8px; font-size: 11px; color: var(--sun-text-tertiary);
  border: 1px solid var(--sun-border); border-radius: 10px; padding: 0 8px;
}
.empty-row { text-align: center; padding: 16px; }
.alias-hint { font-size: 12px; margin: 0; }
.mono { font-family: var(--sun-font-mono); font-size: 12px; }
.dim { color: var(--sun-text-tertiary); }
</style>
