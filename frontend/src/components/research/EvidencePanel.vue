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
import {
  vlmApi,
  type VlmContentClass,
  type VlmMaterialFindings,
  type VlmSubjectType,
} from '../../api/endpoints/vlm'
import { materialIdFromUri } from '../../domain/imageEvidence'
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
  // P8 回流：图像 findings 独立加载（失败不阻塞书证清单）
  void refreshFindings()
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

// ---------- P8 回流：材料卡 AI 图像分析 + findings 子列表（发起→待核→人验闭环） ----------
const isImageMaterial = (m: EvidenceMaterial): boolean =>
  /\.(jpe?g|png)$/i.test(m.filename) || /\.(jpe?g|png)$/i.test(m.orig_name)

const findingsMap = ref<Record<string, VlmMaterialFindings>>({})
const findingsError = ref('')

async function refreshFindings(): Promise<void> {
  if (!props.caseId || !props.clueId) return
  try {
    const page = await vlmApi.findings(props.caseId, props.clueId)
    findingsMap.value = page.findings ?? {}
    findingsError.value = ''
  } catch (e) {
    // findings 是材料卡增强面：失败黄条提示但不阻塞书证清单
    findingsError.value = e instanceof Error ? e.message : String(e)
  }
}

const EMPTY_FINDINGS: VlmMaterialFindings = { pending: [], verified: [] }

function findingsOf(mid: string): VlmMaterialFindings {
  return findingsMap.value[mid] ?? EMPTY_FINDINGS
}

function hasFindings(mid: string): boolean {
  const f = findingsMap.value[mid]
  return !!f && (f.pending.length > 0 || f.verified.length > 0)
}

/** content_class 默认取材料已知类别（可改） */
const CLASS_BY_MATERIAL: Record<string, VlmContentClass> = {
  缴款单: 'invoice',
  付款凭证: 'receipt',
  合同: 'document',
  审批文件: 'document',
  监控截图: 'other',
  其他: 'other',
}

const classOptions = [
  { label: '发票 invoice', value: 'invoice' },
  { label: '收据 receipt', value: 'receipt' },
  { label: '文书 document', value: 'document' },
  { label: '其他 other', value: 'other' },
]

const subjectOptions = [
  { label: '人员 person', value: 'person' },
  { label: '机构 org', value: 'org' },
  { label: '项目 bid_project', value: 'bid_project' },
]

// 发起分析（区内直发，仍走 draft 闸门：content_class 门禁/LLM 策略/提案草案）
const aiFormOpenId = ref('')
const aiClassSel = ref<Record<string, VlmContentClass>>({})
const aiBusyId = ref('')

function toggleAiForm(m: EvidenceMaterial): void {
  if (props.degraded || props.submitting) return
  if (aiFormOpenId.value === m.material_id) {
    aiFormOpenId.value = ''
    return
  }
  aiFormOpenId.value = m.material_id
  if (!aiClassSel.value[m.material_id]) {
    aiClassSel.value[m.material_id] = CLASS_BY_MATERIAL[m.material_type] ?? 'other'
  }
}

async function submitAiDraft(m: EvidenceMaterial): Promise<void> {
  if (aiBusyId.value || props.degraded || props.submitting) return
  aiBusyId.value = m.material_id
  try {
    const r = await vlmApi.draft(props.caseId, {
      image_uri: `evidence/${m.material_id}/${m.filename}`,
      content_class: aiClassSel.value[m.material_id] ?? 'other',
    })
    if (r.ok) {
      message.success(`已生成 ${r.proposals.length} 条待核草案`)
      aiFormOpenId.value = ''
    } else if (r.degraded) {
      message.warning(r.reason ?? r.error ?? '视觉能力降级，未发起模型调用')
    } else {
      message.error(r.error ?? r.reason ?? '被安全闸门阻断')
    }
    await refreshFindings()
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    aiBusyId.value = ''
  }
}

// 内联核验（与 VlmView 核验行同契约：结论必填、主体必选必填；clue_id 预填当前线索）
interface InlineVerifyForm {
  conclusion: string
  subjectType: VlmSubjectType | null
  subjectId: string
  busy: boolean
}
const inlineVerify = ref<Record<string, InlineVerifyForm>>({})

function verifyFormOf(pid: string): InlineVerifyForm {
  if (!inlineVerify.value[pid]) {
    inlineVerify.value[pid] = {
      conclusion: '', subjectType: null, subjectId: '', busy: false,
    }
  }
  return inlineVerify.value[pid]
}

async function submitInlineVerify(pid: string): Promise<void> {
  const f = verifyFormOf(pid)
  if (f.busy || props.degraded || props.submitting) return
  if (!f.conclusion.trim()) {
    message.warning('须填写核验结论（与原件比对结果）')
    return
  }
  if (!f.subjectType) {
    message.warning('核验须选择主体类型（人员/机构/项目）')
    return
  }
  if (!f.subjectId.trim()) {
    message.warning('核验须填写主体 ID')
    return
  }
  f.busy = true
  try {
    await vlmApi.verify(props.caseId, pid, {
      verify_conclusion: f.conclusion.trim(),
      subject_type: f.subjectType,
      subject_id: f.subjectId.trim(),
      clue_id: props.clueId || undefined,
    })
    message.success('核验通过：图像证据已入图入报告')
    await refreshFindings()
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    f.busy = false
  }
}

// 查看图像（pending/verified 共用；按 uri 反解 material_id 后走书证下载流）
const findingPreviews = ref<Record<string, string>>({})

async function previewFinding(key: string, imageUri: string): Promise<void> {
  if (findingPreviews.value[key]) return
  const mid = materialIdFromUri(imageUri)
  if (!mid) {
    message.error('image_uri 不可解析（evidence/<材料ID>/<文件名>）')
    return
  }
  try {
    const blob = await evidenceApi.download(props.caseId, props.clueId, mid)
    findingPreviews.value[key] = URL.createObjectURL(blob)
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  }
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

    <!-- P8 回流：findings 拉取失败软提示（不阻塞书证清单） -->
    <p v-if="findingsError" class="ep-findings-error" role="status" data-testid="ep-findings-error">
      图像 findings 加载失败：{{ findingsError }}
      <NButton text size="tiny" @click="refreshFindings">重试</NButton>
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
          <!-- P8 回流：图像材料区内直发 AI 分析（仍走 draft 闸门） -->
          <NButton
            v-if="isImageMaterial(m)"
            size="tiny"
            class="ep-ai-entry"
            :disabled="degraded || submitting"
            data-testid="ep-ai-new"
            @click="toggleAiForm(m)"
          >
            AI 图像分析
          </NButton>
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
        <!-- P8 回流：发起表单（content_class 默认取材料已知类别） -->
        <div
          v-if="aiFormOpenId === m.material_id"
          class="ep-ai-form"
          data-testid="ep-ai-form"
        >
          <NSelect
            v-model:value="aiClassSel[m.material_id]"
            :options="classOptions"
            size="tiny"
            class="ep-ai-class"
            :disabled="degraded || submitting"
            data-testid="ep-ai-class"
          />
          <NButton
            size="tiny"
            type="primary"
            :loading="aiBusyId === m.material_id"
            :disabled="degraded || submitting"
            data-testid="ep-ai-submit"
            @click="submitAiDraft(m)"
          >
            发起分析
          </NButton>
          <span class="dim ep-ai-hint">模型只产待核草案，经人比对原件后才入图入报告</span>
        </div>
        <!-- P8 回流：findings 子列表（待核草案 + 已人验证据，归属=本材料） -->
        <div v-if="hasFindings(m.material_id)" class="ep-findings" data-testid="ep-findings">
          <div
            v-for="f in findingsOf(m.material_id).pending"
            :key="f.proposal_id"
            class="ep-finding ep-finding--pending"
            :data-proposal-id="f.proposal_id"
          >
            <div class="ep-finding-head">
              <span class="ep-f-tag ep-f-tag--ai">AI 草案</span>
              <span v-if="f.stale" class="ep-f-tag ep-f-tag--stale">已过期</span>
              <span class="ep-f-sev" :class="{ 'ep-f-sev--warn': f.severity === 'warn' }">
                {{ f.severity }}
              </span>
              <span class="ep-f-title">{{ f.title }}</span>
            </div>
            <p class="ep-f-detail">{{ f.detail }}</p>
            <p class="ep-f-meta dim">
              {{ f.model }} · {{ f.created_at }}
              <template v-if="f.model_score !== null && f.model_score !== undefined">
                · 把握度 {{ f.model_score.toFixed(2) }}
              </template>
            </p>
            <img
              v-if="findingPreviews[f.proposal_id]"
              :src="findingPreviews[f.proposal_id]"
              class="ep-f-preview"
              alt="图像预览"
            />
            <div v-if="!f.stale" class="ep-f-verify" data-testid="ep-inline-verify">
              <NInput
                v-model:value="verifyFormOf(f.proposal_id).conclusion"
                type="textarea"
                :rows="2"
                placeholder="核验结论（必填）：与原件/书证来源比对结果"
                :disabled="degraded || submitting"
                data-testid="ep-inline-verify-conclusion"
              />
              <div class="ep-f-verify-row">
                <NSelect
                  v-model:value="verifyFormOf(f.proposal_id).subjectType"
                  :options="subjectOptions"
                  placeholder="主体类型（必选）"
                  size="tiny"
                  class="ep-f-subject-type"
                  :disabled="degraded || submitting"
                  data-testid="ep-inline-verify-subject-type"
                />
                <NInput
                  v-model:value="verifyFormOf(f.proposal_id).subjectId"
                  placeholder="主体 ID（必填）"
                  size="tiny"
                  class="ep-f-subject-id"
                  :disabled="degraded || submitting"
                  data-testid="ep-inline-verify-subject-id"
                />
                <NButton size="tiny" :disabled="degraded || submitting" @click="previewFinding(f.proposal_id, f.image_uri)">
                  查看图像
                </NButton>
                <NButton
                  size="tiny"
                  type="primary"
                  :loading="verifyFormOf(f.proposal_id).busy"
                  :disabled="degraded || submitting"
                  data-testid="ep-inline-verify-submit"
                  @click="submitInlineVerify(f.proposal_id)"
                >
                  核验通过
                </NButton>
              </div>
            </div>
            <p v-else class="ep-f-stale-note">
              过期草案不得人验升格，请重新发起分析
            </p>
          </div>
          <div
            v-for="r in findingsOf(m.material_id).verified"
            :key="r.image_evidence_id"
            class="ep-finding ep-finding--verified"
            :data-image-evidence-id="r.image_evidence_id"
          >
            <div class="ep-finding-head">
              <span class="ep-f-tag ep-f-tag--ok">已人验</span>
              <span class="ep-f-sev" :class="{ 'ep-f-sev--warn': r.severity === 'warn' }">
                {{ r.severity }}
              </span>
              <span class="ep-f-title">{{ r.title || r.image_uri }}</span>
            </div>
            <p class="ep-f-detail">{{ r.detail }}</p>
            <p class="ep-f-conclusion">结论：{{ r.verify_conclusion }}</p>
            <p class="ep-f-meta dim">{{ r.verifier }} · {{ r.created_at }}</p>
            <img
              v-if="findingPreviews[r.image_evidence_id]"
              :src="findingPreviews[r.image_evidence_id]"
              class="ep-f-preview"
              alt="图像预览"
            />
            <NButton size="tiny" quaternary @click="previewFinding(r.image_evidence_id, r.image_uri)">
              查看图像
            </NButton>
          </div>
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
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
}
/* ---- P8 回流：发起表单 + findings 子列表（占满整行） ---- */
.ep-findings-error {
  margin: 6px 0 0;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--sun-warn-text);
}
.ep-ai-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: 1px dashed var(--sun-border-active);
  border-radius: 6px;
  background: var(--sun-input-bg);
}
.ep-ai-class {
  width: 150px;
}
.ep-ai-hint {
  font-size: 11px;
}
.ep-findings {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border-top: 1px dashed var(--sun-border);
}
.ep-finding {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 8px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-input-bg);
  font-size: 12px;
}
.ep-finding--pending {
  border-style: dashed;
}
.ep-finding-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.ep-f-tag {
  display: inline-flex;
  padding: 0 6px;
  font-size: 10px;
  line-height: 15px;
  border-radius: 8px;
  border: 1px solid var(--sun-border);
}
.ep-f-tag--ai {
  border-color: var(--sun-warn-border);
  color: var(--sun-warn-text);
}
.ep-f-tag--stale {
  border-color: var(--sun-error-border, var(--sun-warn-border));
  color: var(--sun-error-text, var(--sun-warn-text));
}
.ep-f-tag--ok {
  border-color: var(--sun-ok-border);
  color: var(--sun-ok-text);
}
.ep-f-sev {
  font-size: 10px;
  color: var(--sun-text-tertiary);
}
.ep-f-sev--warn {
  color: var(--sun-warn-text);
}
.ep-f-title {
  font-weight: 600;
}
.ep-f-detail {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-primary);
}
.ep-f-conclusion {
  margin: 0;
  font-size: 12px;
  color: var(--sun-ok-text);
}
.ep-f-meta {
  margin: 0;
  font-size: 11px;
}
.ep-f-preview {
  max-width: 280px;
  max-height: 200px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
}
.ep-f-stale-note {
  margin: 0;
  font-size: 11px;
  color: var(--sun-warn-text);
}
.ep-f-verify {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--sun-border);
}
.ep-f-verify-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.ep-f-subject-type {
  width: 150px;
}
.ep-f-subject-id {
  width: 140px;
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
