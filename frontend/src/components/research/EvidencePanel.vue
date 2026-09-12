<script setup lang="ts">
// ★ REQ-V-010/011 书证面板（线索详情页核查工作区内）。
// 上传/清单/下载为同步端点（组件内直调 evidenceApi，成功自刷新）；
// 挂接/解除为 202 入队 TASK_VERIFY，仅 emit 给视图层等终态后整体刷新
//（同 VerifyWorkbench transition/replay 纪律：入队即刷新会读到消费前旧 state）。
// 书证是人的行为：degraded/提交中禁用写控件，agent:* 由后端 403 兜底。
import { computed, onMounted, ref } from 'vue'
import { NButton, NInput, NSelect, useMessage } from 'naive-ui'
import {
  EVIDENCE_MATERIAL_TYPES,
  EVIDENCE_MAX_SIZE,
  evidenceSizeLabel,
  type EvidenceMaterial,
  type VerifyItem,
} from '../../domain/verify'
import { evidenceApi, saveBlobAs } from '../../api/endpoints/evidence'
import { presentError } from '../../api/errors'

const props = defineProps<{
  caseId: string
  clueId: string
  degraded: boolean
  /** 父层 202 写请求进行中（挂接/解除/裁决等入队回执期间禁用写控件） */
  submitting?: boolean
  /** 核查项清单（挂接下拉选项 + item_id→文本映射，由 VerifyWorkbench 传入） */
  items: VerifyItem[]
}>()

const emit = defineEmits<{
  /** REQ-V-011 挂接：父层 202 入队，终态后刷新工作区与本面板 */
  link: [payload: { item_id: string; material_id: string }]
  /** REQ-V-011 解除挂接：父层 202 入队 */
  unlink: [payload: { material_id: string }]
}>()

const message = useMessage()

// ---------- 清单（自加载；父层写终态后经 expose 的 refresh 拉新） ----------
const materials = ref<EvidenceMaterial[]>([])
const loading = ref(false)
const errorMsg = ref('')

async function refresh(): Promise<void> {
  if (!props.caseId || !props.clueId) return
  loading.value = true
  errorMsg.value = ''
  try {
    const page = await evidenceApi.list(props.caseId, props.clueId)
    materials.value = page.items ?? []
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

defineExpose({ refresh })

onMounted(() => {
  void refresh()
})

// ---------- REQ-V-010 上传（multipart 同步；20MB 前端预检 + 后端 413 兜底） ----------
const formOpen = ref(false)
const fileInputEl = ref<HTMLInputElement | null>(null)
const pickedFile = ref<File | null>(null)
const materialType = ref<string>('其他')
const note = ref('')
const uploading = ref(false)

const typeOptions = EVIDENCE_MATERIAL_TYPES.map((t) => ({ label: t, value: t }))

function pickFile(): void {
  fileInputEl.value?.click()
}

function onFileChange(e: Event): void {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  if (f.size > EVIDENCE_MAX_SIZE) {
    message.warning(`文件超过 20MB 上限（实际 ${evidenceSizeLabel(f.size)}）`)
    pickedFile.value = null
    input.value = ''
    return
  }
  pickedFile.value = f
}

async function submitUpload(): Promise<void> {
  const f = pickedFile.value
  if (!f || props.degraded || props.submitting) return
  uploading.value = true
  try {
    await evidenceApi.upload(
      props.caseId, props.clueId, f, materialType.value, note.value.trim())
    message.success(`书证已上传：${f.name}`)
    pickedFile.value = null
    note.value = ''
    if (fileInputEl.value) fileInputEl.value.value = ''
    await refresh()
    formOpen.value = false
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    uploading.value = false
  }
}

// ---------- REQ-V-010 下载（原文件字节流，原名保存） ----------
const downloadingId = ref('')

async function doDownload(m: EvidenceMaterial): Promise<void> {
  if (downloadingId.value) return
  downloadingId.value = m.material_id
  try {
    const blob = await evidenceApi.download(
      props.caseId, props.clueId, m.material_id)
    saveBlobAs(blob, m.orig_name || m.filename || m.material_id)
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    downloadingId.value = ''
  }
}

// ---------- REQ-V-011 挂接/解除（解除与挂接均 emit，202 由视图层编排） ----------
/** 未挂接行的挂接目标选择（按 material_id 暂存） */
const linkSel = ref<Record<string, string>>({})

const itemOptions = computed(() =>
  props.items.map((it) => ({
    label: it.text.length > 24 ? `${it.text.slice(0, 24)}…` : it.text,
    value: it.item_id,
  })))

function itemText(itemId: string): string {
  const t = props.items.find((it) => it.item_id === itemId)?.text
  return t ?? itemId
}

function confirmLink(m: EvidenceMaterial): void {
  const itemId = linkSel.value[m.material_id]
  if (!itemId || props.degraded || props.submitting) return
  emit('link', { item_id: itemId, material_id: m.material_id })
}
</script>

<template>
  <section class="ep" data-testid="evidence-panel">
    <header class="ep-head">
      <h3 class="ep-title">
        书证材料 <b class="mono">{{ materials.length }}</b>
      </h3>
      <NButton
        size="tiny"
        :disabled="degraded || submitting"
        data-testid="ep-new"
        @click="formOpen = !formOpen"
      >
        {{ formOpen ? '收起' : '上传书证' }}
      </NButton>
    </header>

    <p v-if="errorMsg" class="ep-error" role="alert">
      书证清单加载失败：{{ errorMsg }}
      <NButton text size="tiny" @click="refresh">重试</NButton>
    </p>

    <!-- REQ-V-010 上传表单（文件 + 类型 + 备注；上传成功即落审计链） -->
    <div v-if="formOpen" class="ep-form" data-testid="ep-form">
      <input
        ref="fileInputEl"
        type="file"
        class="ep-file-input"
        data-testid="ep-file"
        @change="onFileChange"
      />
      <div class="ep-form-row">
        <NButton
          size="small"
          :disabled="degraded || submitting"
          data-testid="ep-pick"
          @click="pickFile"
        >
          选择文件
        </NButton>
        <span v-if="pickedFile" class="ep-file-name" data-testid="ep-file-name">
          {{ pickedFile.name }}（{{ evidenceSizeLabel(pickedFile.size) }}）
        </span>
        <span v-else class="dim">单个文件 ≤ 20MB</span>
      </div>
      <div class="ep-form-row">
        <NSelect
          v-model:value="materialType"
          :options="typeOptions"
          size="small"
          class="ep-type"
          :disabled="degraded || submitting"
          data-testid="ep-type"
        />
        <NInput
          v-model:value="note"
          size="small"
          placeholder="备注（选填）"
          :disabled="degraded || submitting"
          data-testid="ep-note"
        />
      </div>
      <div class="ep-form-foot">
        <span class="dim">上传即入审计链；书证文件与导入数据源物理分离</span>
        <NButton
          size="small"
          type="primary"
          :loading="uploading"
          :disabled="!pickedFile || degraded || submitting"
          data-testid="ep-submit"
          @click="submitUpload"
        >
          上传书证
        </NButton>
      </div>
    </div>

    <p
      v-if="!loading && materials.length === 0 && !formOpen && !errorMsg"
      class="ep-empty dim"
    >
      暂无书证材料——缴款单、合同、监控截图等可在此上传归档并挂接核查项。
    </p>

    <ul v-if="materials.length > 0" class="ep-list">
      <li
        v-for="m in materials"
        :key="m.material_id"
        class="ep-item"
        :data-material-id="m.material_id"
      >
        <div class="ep-item-main">
          <div class="ep-item-badges">
            <span class="ep-type-tag">{{ m.material_type }}</span>
            <span v-if="m.item_id" class="ep-linked" data-testid="ep-linked">
              已挂核查项
            </span>
          </div>
          <p class="ep-name">
            {{ m.orig_name }}
            <span class="ep-size dim mono">{{ evidenceSizeLabel(m.size) }}</span>
          </p>
          <p v-if="m.note" class="ep-note dim">{{ m.note }}</p>
          <p class="ep-meta dim">
            <template v-if="m.uploaded_by">{{ m.uploaded_by }} · </template>{{ m.uploaded_at }}
            <template v-if="m.item_id"> · 挂于「{{ itemText(m.item_id) }}」</template>
            · <code class="mono">{{ m.material_id }}</code>
          </p>
        </div>
        <div class="ep-item-actions">
          <NButton
            size="tiny"
            :loading="downloadingId === m.material_id"
            data-testid="ep-download"
            @click="doDownload(m)"
          >
            下载
          </NButton>
          <!-- REQ-V-011：已挂接 → 解除（行保留）；未挂接 → 选核查项挂接 -->
          <NButton
            v-if="m.item_id"
            size="tiny"
            :disabled="degraded || submitting"
            data-testid="ep-unlink"
            @click="emit('unlink', { material_id: m.material_id })"
          >
            解除挂接
          </NButton>
          <template v-else>
            <NSelect
              v-model:value="linkSel[m.material_id]"
              :options="itemOptions"
              size="tiny"
              class="ep-link-select"
              :disabled="degraded || submitting || items.length === 0"
              :placeholder="items.length === 0 ? '无核查项' : '选择核查项'"
              data-testid="ep-link-select"
            />
            <NButton
              size="tiny"
              type="primary"
              :disabled="degraded || submitting || !linkSel[m.material_id]"
              data-testid="ep-link"
              @click="confirmLink(m)"
            >
              挂接
            </NButton>
          </template>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.ep {
  margin-top: 12px;
  padding: 8px 10px;
  border: 1px solid var(--sun-border);
  border-radius: 8px;
  background: var(--sun-bg-card);
}
.ep-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.ep-title {
  margin: 0;
  font-size: 13px;
}
.ep-error {
  margin: 6px 0 0;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--sun-danger-text, var(--sun-warn-text));
}
.ep-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
  padding: 8px;
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  background: var(--sun-input-bg);
}
.ep-file-input {
  display: none;
}
.ep-form-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ep-type {
  width: 120px;
  flex: none;
}
.ep-file-name {
  font-size: 12px;
  color: var(--sun-ok-text);
  word-break: break-all;
}
.ep-form-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
}
.ep-empty {
  margin: 8px 0 0;
  font-size: 12px;
}
.ep-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}
.ep-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
}
.ep-item-main {
  min-width: 0;
  flex: 1;
}
.ep-item-badges {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ep-type-tag {
  display: inline-block;
  padding: 0 5px;
  font-size: 10px;
  line-height: 15px;
  border: 1px solid var(--sun-border-active);
  border-radius: 8px;
  color: var(--sun-border-active);
}
.ep-linked {
  display: inline-block;
  padding: 0 5px;
  font-size: 10px;
  line-height: 15px;
  border: 1px solid var(--sun-ok-border);
  border-radius: 8px;
}
.ep-name {
  margin: 3px 0 0;
  font-size: 12px;
  word-break: break-all;
}
.ep-size {
  margin-left: 6px;
  font-size: 11px;
}
.ep-note {
  margin: 2px 0 0;
  font-size: 11px;
}
.ep-meta {
  margin: 2px 0 0;
  font-size: 11px;
}
.ep-item-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.ep-link-select {
  width: 150px;
}
</style>
