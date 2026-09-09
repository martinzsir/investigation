<script setup lang="ts">
// 案件包（FE-P-016，/c/package）：导出 / 导入两 tab。
// 纪律：verify 七步清单（A1）——chain 不完整为橙色告警不阻断，ok=false 禁止导入；
// 敏感文件（case_knowledge.json 等）红框明示；导出/导入均为任务（SSE 进度）；
// 下载走 Blob 二进制流；导入预校验失败（422/409）错误信封直出。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  NTabs, NTabPane, NSpin, NButton, NSelect, NInput, NTag, useMessage,
} from 'naive-ui'
import { packageApi, type VerifyResult } from '../api/endpoints/packageCase'
import { tasksApi, type TaskRow } from '../api/endpoints/tasks'
import { casesApi } from '../api/endpoints/cases'
import type { StreamHandle } from '../api/transport/types'
import { useCaseStore } from '../stores/case'
import { presentError, isApiError } from '../api/errors'
import {
  canImport, chainWarning, downloadZipName, sensitiveRedList, taskPhase,
} from '../domain/packageFlow'
import { caseIdError, caseNameError } from '../domain/portal'
import StepChecklist from '../components/tools/StepChecklist.vue'
import TaskProgressCard from '../components/task/TaskProgressCard.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

const tab = ref<'export' | 'import'>('export')

// ---- 导出 ----
const exportCaseId = ref<string | null>(cs.currentCaseId || null)
const exportBusy = ref(false)
const exportTask = ref<TaskRow | null>(null)
const chainOk = ref<boolean | null>(null)
const chainLoading = ref(false)
let stream: StreamHandle | null = null

const caseOptions = computed(() =>
  cs.cases.map((c) => ({ label: `${c.name}（${c.id}）`, value: c.id })),
)

const exportReady = computed(() => Boolean(exportCaseId.value))
const exportDone = computed(() => taskPhase(exportTask.value?.status) === 'done')
const exportFailed = computed(() => taskPhase(exportTask.value?.status) === 'failed')
const chainWarn = computed(() => (chainOk.value === false ? chainWarning(false) : ''))

async function loadChainState(cid: string): Promise<void> {
  chainLoading.value = true
  chainOk.value = null
  try {
    const s = await casesApi.summary(cid)
    chainOk.value = s.health.chain_ok
  } catch {
    // 汇总失败不阻断导出（任务侧仍有校验）
    chainOk.value = null
  } finally {
    chainLoading.value = false
  }
}

watch(exportCaseId, (cid) => {
  exportTask.value = null
  if (cid) void loadChainState(cid)
})

function subscribe(taskId: string): void {
  stream?.close()
  stream = tasksApi.stream(taskId, {
    onTask: (t) => {
      exportTask.value = t
      if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(t.status)) {
        stream?.close()
        stream = null
        if (t.status === 'SUCCEEDED') message.success('案件包导出完成，可下载')
      }
    },
  })
}

async function doExport(): Promise<void> {
  if (!exportCaseId.value) return
  exportBusy.value = true
  exportTask.value = null
  try {
    const taskId = await packageApi.exportCase(exportCaseId.value)
    message.success('导出任务已入队')
    subscribe(taskId)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    exportBusy.value = false
  }
}

async function doDownload(): Promise<void> {
  if (!exportTask.value || !exportCaseId.value) return
  try {
    const blob = await packageApi.download(exportTask.value.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = downloadZipName(exportCaseId.value)
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  }
}

// ---- 导入 ----
const fileInput = ref<HTMLInputElement | null>(null)
const importFile = ref<File | null>(null)
const verifying = ref(false)
const verifyResult = ref<VerifyResult | null>(null)
const newCaseId = ref('')
const newCaseName = ref('')
const importBusy = ref(false)
const importTask = ref<TaskRow | null>(null)

function pickFile(): void {
  fileInput.value?.click()
}

function onFileChange(e: Event): void {
  const input = e.target as HTMLInputElement
  verifyResult.value = null
  importTask.value = null
  importFile.value = input.files?.[0] ?? null
}

async function doVerify(): Promise<void> {
  if (!importFile.value) return
  verifying.value = true
  verifyResult.value = null
  try {
    verifyResult.value = await packageApi.verify(importFile.value)
    if (verifyResult.value.ok) message.success('校验通过，可以导入')
    else message.warning(`校验未通过：${verifyResult.value.errors.length} 个问题`)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    verifying.value = false
  }
}

const importIdErr = computed(() => caseIdError(newCaseId.value))
const importNameErr = computed(() => caseNameError(newCaseName.value))
const importAllowed = computed(() => canImport(verifyResult.value))
const sensitiveFiles = computed(() => sensitiveRedList(verifyResult.value?.sensitive_files))
const importChainWarn = computed(() =>
  verifyResult.value && !verifyResult.value.chain_ok ? chainWarning(false) : '',
)

async function doImport(): Promise<void> {
  if (!importFile.value || !importAllowed.value) return
  if (importIdErr.value || importNameErr.value) {
    message.error('请修正案件编号/名称')
    return
  }
  importBusy.value = true
  try {
    const taskId = await packageApi.importPackage(
      importFile.value,
      newCaseId.value.trim(),
      newCaseName.value.trim(),
    )
    message.success('导入任务已入队')
    stream?.close()
    stream = tasksApi.stream(taskId, {
      onTask: (t) => {
        importTask.value = t
        if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(t.status)) {
          stream?.close()
          stream = null
          if (t.status === 'SUCCEEDED') {
            message.success('案件包导入完成')
            void cs.loadCases()
          }
        }
      },
    })
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    importBusy.value = false
  }
}

onBeforeUnmount(() => {
  stream?.close()
  stream = null
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>案件包</h2>
      <p class="dim hint">
        跨环境交接的可审计单元：导出 zip（声明 + 数据 + 审计链）→ 对端七步校验 → 导入为新案件。
        审计链不完整为橙色告警不阻断；校验失败禁止导入。
      </p>
    </div>

    <NTabs v-model:value="tab" type="line" animated>
      <!-- 导出 -->
      <NTabPane name="export" tab="导出案件包">
        <div class="card">
          <div class="card-title">选择导出案件</div>
          <div class="row">
            <NSelect
              v-model:value="exportCaseId"
              :options="caseOptions"
              class="case-select"
              placeholder="选择案件"
              filterable
            />
            <NButton type="primary" :loading="exportBusy" :disabled="!exportReady" @click="doExport">
              导出案件包
            </NButton>
          </div>

          <NSpin :show="chainLoading">
            <div v-if="chainWarn" class="warn-bar">⚠ {{ chainWarn }}</div>
            <p v-else-if="chainOk === true" class="dim ok-line">审计链校验通过。</p>
          </NSpin>

          <EmptyState
            v-if="!exportReady"
            type="empty"
            title="请先选择案件"
            desc="可在案件门户选择当前案件，或在此下拉指定"
          />

          <div v-if="exportTask" class="task-wrap">
            <TaskProgressCard :task="exportTask" />
            <div v-if="exportDone" class="row">
              <NButton type="primary" size="small" @click="doDownload">
                下载 {{ exportCaseId }}_package.zip
              </NButton>
            </div>
            <p v-else-if="exportFailed" class="dim fail-line">
              导出失败：{{ exportTask.error_message || exportTask.error_code || '见任务中心明细' }}
            </p>
          </div>
        </div>
      </NTabPane>

      <!-- 导入 -->
      <NTabPane name="import" tab="导入案件包">
        <div class="card">
          <div class="card-title">1. 选择案件包文件（.zip）</div>
          <div class="row">
            <input ref="fileInput" type="file" accept=".zip" class="file-input" @change="onFileChange" />
            <NButton @click="pickFile">{{ importFile ? '重新选择' : '选择文件' }}</NButton>
            <span v-if="importFile" class="mono dim file-name">{{ importFile.name }}（{{ Math.round(importFile.size / 1024) }} KB）</span>
          </div>
          <div class="row">
            <NButton :loading="verifying" :disabled="!importFile" @click="doVerify">2. 校验案件包（七步）</NButton>
          </div>

          <NSpin :show="verifying">
            <template v-if="verifyResult">
              <StepChecklist :steps="verifyResult.steps" />

              <div v-if="verifyResult.errors.length" class="error-list">
                <div class="error-title">阻断问题：</div>
                <ul>
                  <li v-for="(err, i) in verifyResult.errors" :key="i" class="mono">{{ err }}</li>
                </ul>
              </div>

              <div v-if="importChainWarn" class="warn-bar">⚠ {{ importChainWarn }}</div>

              <div v-if="sensitiveFiles.length" class="sensitive-box">
                <div class="sensitive-title">敏感文件（交接须当面说明，红框留痕）：</div>
                <ul>
                  <li v-for="(f, i) in sensitiveFiles" :key="i" class="mono">{{ f }}</li>
                </ul>
              </div>

              <div v-if="!importAllowed" class="deny-line">
                校验未通过，禁止导入（修复上述阻断问题后重新校验）。
              </div>

              <div v-else class="import-form">
                <div class="card-title">3. 指定导入为新案件</div>
                <label class="form-row">
                  <span class="form-label">新案件编号 <b>*</b></span>
                  <NInput v-model:value="newCaseId" placeholder="字母/数字/下划线/中划线，1–64" />
                  <span v-if="importIdErr" class="form-err">{{ importIdErr }}</span>
                </label>
                <label class="form-row">
                  <span class="form-label">新案件名称 <b>*</b></span>
                  <NInput v-model:value="newCaseName" placeholder="1–128 字" />
                  <span v-if="importNameErr" class="form-err">{{ importNameErr }}</span>
                </label>
                <NTag size="small" :bordered="false" type="info">
                  包内 {{ verifyResult.file_count }} 个文件
                </NTag>
                <NButton type="primary" :loading="importBusy" @click="doImport">4. 导入为新案件</NButton>
              </div>
            </template>
          </NSpin>

          <div v-if="importTask" class="task-wrap">
            <TaskProgressCard :task="importTask" />
          </div>
        </div>
      </NTabPane>
    </NTabs>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-head h2 {
  margin: 0;
  font-size: 18px;
}
.hint {
  font-size: 12px;
  margin: 4px 0 0;
}
.card {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-title {
  font-size: 13px;
  font-weight: 600;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.case-select {
  width: 320px;
}
.file-input {
  display: none;
}
.file-name {
  font-size: 12px;
}
.warn-bar {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
}
.ok-line {
  font-size: 12px;
  margin: 0;
}
.fail-line {
  font-size: 12px;
  margin: 0;
  color: var(--sun-error-text);
}
.task-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.error-list {
  border: 1px solid var(--sun-error-border);
  background: var(--sun-error-bg);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
}
.error-title {
  color: var(--sun-error-text);
  font-weight: 600;
}
.error-list ul {
  margin: 4px 0 0;
  padding-left: 18px;
  color: var(--sun-error-text);
}
.sensitive-box {
  border: 2px solid var(--sun-error-border);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
}
.sensitive-title {
  font-weight: 600;
  color: var(--sun-error-text);
}
.sensitive-box ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.deny-line {
  font-size: 12px;
  color: var(--sun-error-text);
  font-weight: 600;
}
.import-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-top: 1px dashed var(--sun-border);
  padding-top: 10px;
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 420px;
}
.form-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.form-label b {
  color: var(--sun-error-text);
}
.form-err {
  font-size: 12px;
  color: var(--sun-error-text);
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
