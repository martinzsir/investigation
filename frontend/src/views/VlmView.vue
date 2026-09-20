<script setup lang="ts">
// 图像研判（P8，/c/vlm）：VLM 只产「AI 草案待核」，正兵比对原件、
// 填核验结论后才写 image_evidence（自动入图入报告）；stale 草案拒升格。
import { computed, reactive, ref, watch } from 'vue'
import { NButton, NInput, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import {
  vlmApi,
  type VlmContentClass,
  type VlmDraftRecord,
  type VlmDraftResult,
  type VlmSubjectType,
} from '../api/endpoints/vlm'
import { evidenceApi } from '../api/endpoints/evidence'
import { materialIdFromUri } from '../domain/imageEvidence'
import type { EvidenceMaterial } from '../domain/verify'
import { useCaseStore } from '../stores/case'
import { isApiError, presentError } from '../api/errors'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

// ---- 发起分析 ----
const evidenceItems = ref<EvidenceMaterial[]>([])
const imageOptions = computed(() =>
  evidenceItems.value
    .filter((it) => /\.(jpe?g|png)$/i.test(it.filename)
      || /\.(jpe?g|png)$/i.test(it.orig_name))
    .map((it) => ({
      label: `${it.orig_name}（${it.material_id}）`,
      value: `evidence/${it.material_id}/${it.filename}`,
    })),
)
const selectedImage = ref<string | null>(null)
const contentClass = ref<VlmContentClass>('invoice')
const instruction = ref('')
const subjectType = ref<VlmSubjectType | null>(null)
const subjectId = ref('')
const submitting = ref(false)
const lastResult = ref<VlmDraftResult | null>(null)

// ---- 草案列表 ----
const drafts = ref<VlmDraftRecord[]>([])
const listLoading = ref(false)

interface VerifyForm {
  conclusion: string
  subjectType: VlmSubjectType | null
  subjectId: string
  clueId: string
  busy: boolean
}
const verifyForms = reactive<Record<string, VerifyForm>>({})
const previewUrls = ref<Record<string, string>>({})

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

function scoreOf(rec: VlmDraftRecord): number | null {
  const s = rec.payload._sort_hint?.model_score
  return typeof s === 'number' ? s : null
}

function ensureForm(rec: VlmDraftRecord): void {
  if (verifyForms[rec.proposal_id]) return
  const st = rec.payload.input.subject_type
  verifyForms[rec.proposal_id] = {
    conclusion: '',
    subjectType: (st as VlmSubjectType) || null,
    subjectId: rec.payload.input.subject_id || '',
    clueId: '',
    busy: false,
  }
}

async function loadEvidence(): Promise<void> {
  if (!cs.currentCaseId) {
    evidenceItems.value = []
    return
  }
  try {
    evidenceItems.value = (await evidenceApi.listCase(cs.currentCaseId)).items
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  }
}

async function loadDrafts(): Promise<void> {
  if (!cs.currentCaseId) {
    drafts.value = []
    return
  }
  listLoading.value = true
  try {
    drafts.value = await vlmApi.list(cs.currentCaseId)
    drafts.value.forEach(ensureForm)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    listLoading.value = false
  }
}

watch(() => cs.currentCaseId,
  () => void Promise.all([loadEvidence(), loadDrafts()]),
  { immediate: true })

async function submitDraft(): Promise<void> {
  if (!selectedImage.value) {
    message.warning('请先选择图像（JPEG/PNG 书证）')
    return
  }
  submitting.value = true
  try {
    const r = await vlmApi.draft(cs.currentCaseId!, {
      image_uri: selectedImage.value,
      content_class: contentClass.value,
      instruction: instruction.value || undefined,
      subject_type: subjectType.value ?? undefined,
      subject_id: subjectId.value || undefined,
    })
    lastResult.value = r
    if (r.ok) {
      message.success(`已生成 ${r.proposals.length} 条待核草案`)
    } else if (r.degraded) {
      message.warning(r.reason ?? r.error ?? '视觉能力降级，未发起模型调用')
    } else {
      message.error(r.error ?? r.reason ?? '被安全闸门阻断')
    }
    await loadDrafts()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    submitting.value = false
  }
}

async function preview(rec: VlmDraftRecord): Promise<void> {
  if (previewUrls.value[rec.proposal_id]) return
  const mid = materialIdFromUri(rec.payload.input.image_uri)
  if (!mid) {
    message.error('图像 URI 解析失败（无法定位材料）')
    return
  }
  const mat = evidenceItems.value.find((m) => m.material_id === mid)
  const clueId = mat?.clue_id
  if (!mat || !clueId) {
    message.error('未找到书证来源（图像须先上传至某线索）')
    return
  }
  try {
    const blob = await evidenceApi.download(
      cs.currentCaseId!, clueId, mid)
    previewUrls.value[rec.proposal_id] = URL.createObjectURL(blob)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  }
}

async function verify(rec: VlmDraftRecord): Promise<void> {
  const f = verifyForms[rec.proposal_id]
  if (!f.conclusion.trim()) {
    message.warning('须填写核验结论（与原件/书证来源比对结果）')
    return
  }
  if (!f.subjectType) {
    message.warning('核验须选择主体类型（人员/机构/项目）')
    return
  }
  if (!f.subjectId.trim()) {
    message.warning('核验须填写主体 ID（如 p1）')
    return
  }
  f.busy = true
  try {
    await vlmApi.verify(cs.currentCaseId!, rec.proposal_id, {
      verify_conclusion: f.conclusion.trim(),
      subject_type: f.subjectType,
      subject_id: f.subjectId.trim(),
      clue_id: f.clueId || undefined,
    })
    message.success('核验通过：图像证据已入图入报告')
    await loadDrafts()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    f.busy = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>
        图像研判
        <NTag size="small" type="info" :bordered="false">AI 草案 · 人验闭环</NTag>
      </h2>
      <p class="dim hint">
        AI 视觉模型只产「草案」，经人工比对原件、填写核验结论后才入图入报告；
        出网前自动剥离 EXIF，人脸/证件类图像禁止出网；过期（stale）草案须重新分析。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="图像研判按案件进行" />

    <template v-else>
      <!-- 发起分析 -->
      <div class="card">
        <div class="card-title">发起 AI 图像分析</div>
        <div class="form-row">
          <label class="field grow">
            <span class="flabel">图像（已上传书证中的 JPEG/PNG）</span>
            <NSelect
              v-model:value="selectedImage"
              :options="imageOptions"
              placeholder="无图像时请先在线索详情上传书证"
              filterable
            />
          </label>
          <label class="field">
            <span class="flabel">图像类别</span>
            <NSelect v-model:value="contentClass" :options="classOptions" />
          </label>
        </div>
        <div class="form-row">
          <label class="field grow">
            <span class="flabel">分析要求（可选；不得包含手机号/身份证）</span>
            <NInput v-model:value="instruction" type="textarea" :rows="2" />
          </label>
        </div>
        <div class="form-row">
          <label class="field">
            <span class="flabel">主体类型（可选）</span>
            <NSelect
              v-model:value="subjectType"
              :options="subjectOptions"
              placeholder="不指定"
              clearable
              style="width: 170px"
            />
          </label>
          <label class="field">
            <span class="flabel">主体 ID（可选）</span>
            <NInput v-model:value="subjectId" placeholder="如 p1" style="width: 140px" />
          </label>
          <NButton
            type="primary"
            :loading="submitting"
            style="align-self: flex-end"
            @click="submitDraft"
          >发起分析</NButton>
        </div>
      </div>

      <!-- 最近一次调用的档位留痕 -->
      <div v-if="lastResult && !lastResult.ok" class="banner" :class="{ block: lastResult.blocked }">
        <template v-if="lastResult.degraded">
          能力降级（mode={{ lastResult.mode }}）：{{ lastResult.reason ?? lastResult.error }}——未发起模型调用
        </template>
        <template v-else>
          安全闸门阻断/调用失败：{{ lastResult.error ?? lastResult.reason }}
        </template>
      </div>

      <!-- 草案队列 -->
      <NSpin :show="listLoading">
        <div class="card">
          <div class="card-title row">
            <span>图像草案队列（{{ drafts.length }}）</span>
            <NButton size="tiny" quaternary @click="loadDrafts">刷新</NButton>
          </div>
          <EmptyState
            v-if="!listLoading && !drafts.length"
            type="empty"
            title="暂无图像草案"
            desc="发起分析后，AI 草案在此等待人工核验，模型不直入生产"
          />
          <div
            v-for="rec in drafts"
            :key="rec.proposal_id"
            class="draft"
            :class="{ stale: rec.stale && rec.status === 'draft', done: rec.status !== 'draft' }"
          >
            <div class="draft-head">
              <span class="draft-title">{{ rec.payload.candidate.title }}</span>
              <NTag v-if="rec.status === 'draft' && !rec.stale" size="small" type="warning">AI 草案待核</NTag>
              <NTag v-else-if="rec.stale" size="small" type="error">已过期 stale</NTag>
              <NTag v-else size="small" type="success">{{ rec.status }}</NTag>
              <NTag size="tiny" :bordered="false">{{ rec.payload.candidate.severity }}</NTag>
            </div>
            <p class="draft-detail">{{ rec.payload.candidate.detail }}</p>
            <div class="draft-meta dim">
              <span class="mono">{{ rec.payload.input.image_uri }}</span>
              <span>{{ rec.payload.input.model }} · {{ rec.payload.input.prompt_version }}</span>
              <span v-if="scoreOf(rec) !== null" class="mono">把握度 {{ scoreOf(rec)?.toFixed(2) }}</span>
              <span>EXIF 出网前已剥离</span>
            </div>
            <img
              v-if="previewUrls[rec.proposal_id]"
              :src="previewUrls[rec.proposal_id]"
              class="preview"
              alt="图像预览"
            />

            <template v-if="rec.status === 'draft'">
              <div v-if="rec.stale" class="stale-note">
                过期草案不得人验升格，请重新发起分析
              </div>
              <div v-else-if="verifyForms[rec.proposal_id]" class="verify-box">
                <NInput
                  v-model:value="verifyForms[rec.proposal_id].conclusion"
                  type="textarea"
                  :rows="2"
                  placeholder="核验结论（必填）：与原件/书证来源比对结果"
                />
                <div class="form-row tight">
                  <NSelect
                    v-model:value="verifyForms[rec.proposal_id].subjectType"
                    :options="subjectOptions"
                    placeholder="主体类型（必选）"
                    style="width: 170px"
                  />
                  <NInput
                    v-model:value="verifyForms[rec.proposal_id].subjectId"
                    placeholder="主体 ID（必填）"
                    style="width: 140px"
                  />
                  <NInput
                    v-model:value="verifyForms[rec.proposal_id].clueId"
                    placeholder="关联线索 ID（可选）"
                    style="width: 180px"
                  />
                  <NButton size="small" quaternary @click="preview(rec)">查看图像</NButton>
                  <NButton
                    size="small"
                    type="primary"
                    :loading="verifyForms[rec.proposal_id].busy"
                    @click="verify(rec)"
                  >核验通过 · 入图入报告</NButton>
                </div>
              </div>
            </template>
          </div>
        </div>
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
.page-head h2 {
  margin: 0;
  font-size: 18px;
  display: flex;
  align-items: center;
  gap: 8px;
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
}
.card-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
}
.card-title.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.form-row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.form-row.tight {
  margin-bottom: 0;
  margin-top: 8px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field.grow {
  flex: 1;
  min-width: 260px;
}
.flabel {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.banner {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
}
.banner.block {
  background: var(--sun-danger-bg, rgba(255, 107, 107, 0.1));
  border-color: var(--sun-danger-border, rgba(255, 107, 107, 0.4));
  color: var(--sun-danger-text, #ff6b6b);
}
.draft {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 8px;
}
.draft.done {
  opacity: 0.85;
  background: rgba(110, 222, 233, 0.04);
}
.draft.stale {
  border-style: dashed;
}
.draft:last-child {
  margin-bottom: 0;
}
.draft-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.draft-title {
  font-weight: 600;
  font-size: 13px;
}
.draft-detail {
  margin: 6px 0;
  font-size: 13px;
}
.draft-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 11px;
}
.preview {
  margin-top: 8px;
  max-width: 360px;
  max-height: 260px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
}
.verify-box {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--sun-border);
}
.stale-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--sun-danger-text, #ff6b6b);
}
.dim {
  color: var(--sun-text-tertiary);
}
.mono {
  font-family: var(--sun-font-mono);
}
</style>
