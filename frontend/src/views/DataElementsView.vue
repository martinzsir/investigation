<script setup lang="ts">
// 数据元（MVP-4，/c/data-elements）。
// data_elements.json 整包：编码/名称/值类型/长度/敏感标记/遮蔽/代码表枚举；
// 数据元定义是 ETL 校验与映射的口径，变更影响装载行为 → 🔴 危险确认 + 理由必填。
// reason 为审计留痕字段，由后端剥离、不落数据文件。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NInput, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { dataElementsApi, type DataElementsDoc } from '../api/endpoints/dataElements'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const doc = ref<DataElementsDoc | null>(null)

const editing = ref(false)
const editText = ref('')
const editError = ref('')

const canWrite = computed(() => canWriteConfig(auth.clearance))

interface ElementRow {
  code: string
  name: string
  type: string
  length?: number
  sensitive: boolean
  mask?: string
  enumCount: number
}

const rows = computed<ElementRow[]>(() => {
  const elements = (doc.value?.elements ?? {}) as Record<string, Record<string, unknown>>
  return Object.entries(elements).map(([code, e]) => ({
    code,
    name: String(e.name ?? ''),
    type: String(e.type ?? ''),
    length: typeof e.length === 'number' ? e.length : undefined,
    sensitive: Boolean(e.sensitive),
    mask: e.mask ? String(e.mask) : undefined,
    enumCount: Array.isArray(e.enum) ? (e.enum as unknown[]).length : 0,
  }))
})

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    doc.value = null
    return
  }
  loading.value = true
  try {
    doc.value = await dataElementsApi.get(cs.currentCaseId)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

function startEdit(): void {
  editText.value = JSON.stringify(doc.value, null, 2)
  editError.value = ''
  editing.value = true
}

const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)

function askSave(): void {
  try {
    const parsed = JSON.parse(editText.value)
    if (!parsed.elements || typeof parsed.elements !== 'object') {
      editError.value = '必须包含 elements 对象（编码 → 数据元定义）'
      return
    }
  } catch (e) {
    editError.value = `JSON 格式错误：${(e as Error).message}`
    return
  }
  confirmReason.value = ''
  confirmOpen.value = true
}

async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  let parsed: DataElementsDoc
  try {
    parsed = JSON.parse(editText.value) as DataElementsDoc
  } catch (e) {
    editError.value = `JSON 格式错误：${(e as Error).message}`
    return
  }
  confirmSaving.value = true
  try {
    await dataElementsApi.save(cs.currentCaseId, parsed, confirmReason.value)
    message.success('数据元定义已保存并留痕；新口径在下次装载/RESCAN 生效')
    confirmOpen.value = false
    editing.value = false
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>数据元</h2>
      <p class="dim hint">
        数据元是跨数据源的标准口径（类型/长度/敏感级/代码表枚举）；接入映射与 ETL 校验都以此为准。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="数据元按案件快照归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 数据元定义改变装载校验口径（如长度/枚举/类型），保存后下次装载生效并记入审计链。
      </div>

      <NSpin :show="loading">
        <div class="toolbar">
          <span class="dim">共 {{ rows.length }} 个数据元</span>
          <NButton v-if="!editing" type="primary" size="small" :disabled="!canWrite" @click="startEdit">
            {{ canWrite ? '编辑整包 JSON' : '🔒 需偏将及以上' }}
          </NButton>
        </div>

        <div v-if="!editing" class="grid-wrap">
          <table class="grid">
            <thead>
              <tr><th>编码</th><th>名称</th><th>类型</th><th>长度</th><th>敏感</th><th>遮蔽</th><th>枚举值</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in rows" :key="r.code">
                <td class="mono">{{ r.code }}</td>
                <td>{{ r.name }}</td>
                <td><NTag size="tiny" :bordered="false">{{ r.type }}</NTag></td>
                <td>{{ r.length ?? '—' }}</td>
                <td>
                  <NTag v-if="r.sensitive" size="tiny" type="error" :bordered="false">敏感</NTag>
                  <span v-else class="dim">—</span>
                </td>
                <td>{{ r.mask ?? '—' }}</td>
                <td>{{ r.enumCount ? `${r.enumCount} 项` : '—' }}</td>
              </tr>
              <tr v-if="!rows.length"><td colspan="7" class="dim empty-row">暂无数据元定义</td></tr>
            </tbody>
          </table>
        </div>

        <div v-else class="edit-wrap">
          <NInput v-model:value="editText" type="textarea" :rows="20" class="mono edit-area" />
          <div v-if="editError" class="field-error">{{ editError }}</div>
          <div class="edit-actions">
            <NButton size="small" @click="editing = false">取消</NButton>
            <NButton type="primary" danger size="small" @click="askSave">校验并保存（危险变更）</NButton>
          </div>
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="true"
      detail="data_elements.json 整包更新（类型/长度/敏感/枚举口径），下次装载生效"
      :loading="confirmSaving"
      title="数据元变更确认"
      @confirm="doSave"
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
.toolbar { display: flex; justify-content: space-between; align-items: center; }
.grid-wrap { border: 1px solid var(--sun-border); border-radius: 6px; background: var(--sun-bg-card); overflow: auto; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; white-space: nowrap; }
.empty-row { text-align: center; padding: 16px; }
.edit-wrap { display: flex; flex-direction: column; gap: 8px; }
.edit-area { font-size: 12px; }
.field-error { font-size: 12px; color: var(--sun-error-text); }
.edit-actions { display: flex; justify-content: flex-end; gap: 8px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
