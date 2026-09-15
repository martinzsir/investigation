<script setup lang="ts">
// S5 通用 schema 驱动表单壳（F2/F4/F5）：
//  - 7 个通用本体文件：SchemaNode 递归表单 + 未声明字段只读保留（F5）+
//    「提交变更提案 → 影响面 → 人工发布」流程（R4：绝不自动发布）；
//  - llm_policy：只读 JSON，永不渲染表单（R6/UC-S5-7）；
//  - schema 缺失：只读 JSON 兜底（E2-1），不猜结构。
// 不替换 S2–S4 专用编辑器（R5）：objects/links/rules/policies/actions/states/
// data_elements/case_knowledge 不在此挂载。
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  NAlert,
  NButton,
  NCheckbox,
  NInput,
  NModal,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import {
  ontologyGenericApi,
  type GenericFileDto,
  type ImpactResult,
  type ProposalDto,
} from '../api/endpoints/ontologyGeneric'
import { isApiError, presentError } from '../api/errors'
import { useCaseStore } from '../stores/case'
import { collectUnknown, validateDoc, type FormError } from '../domain/schemaForm'
import {
  canDiscardProposal,
  canPublishProposal,
  impactBlocksPublish,
  impactWarnings,
  PROPOSAL_STATUS_META,
} from '../domain/impact'
import SchemaNode from '../components/om/SchemaNode.vue'
import ImpactPanel from '../components/om/ImpactPanel.vue'

const route = useRoute()
const cs = useCaseStore()
const message = useMessage()

const FILE_LABELS: Record<string, string> = {
  derived_properties: '派生属性',
  dimensions: '维度声明',
  enum_space: '庙算枚举空间',
  jians: '五间配置',
  scoring: '评分配置',
  thresholds: '阈值策略',
  verify_playbooks: '核查剧本',
  llm_policy: 'LLM 策略',
}

const fileName = computed(() => String(route.params.file ?? ''))
const caseId = computed(() => String(route.params.caseId ?? cs.currentCaseId ?? ''))

const loading = ref(false)
const loadError = ref('')
const dto = ref<GenericFileDto | null>(null)
const originalText = ref('')
const model = ref<Record<string, unknown>>({})

const isReadonlyFile = computed(() => dto.value?.form_enabled === false)
const hasSchema = computed(() => dto.value?.schema !== null && dto.value?.schema !== undefined)

async function load(): Promise<void> {
  if (!fileName.value || !caseId.value) return
  loading.value = true
  loadError.value = ''
  try {
    dto.value = await ontologyGenericApi.getFile(caseId.value, fileName.value)
    model.value = JSON.parse(JSON.stringify(dto.value.doc))
    originalText.value = JSON.stringify(dto.value.doc)
  } catch (e) {
    dto.value = null
    loadError.value = isApiError(e) ? e.message : presentError(e).title
  } finally {
    loading.value = false
  }
}
watch([fileName, caseId], load, { immediate: true })

const dirty = computed(() => JSON.stringify(model.value) !== originalText.value)

const unknownFields = computed(() =>
  hasSchema.value ? collectUnknown(dto.value!.schema, model.value) : [],
)
const formErrors = computed<FormError[]>(() =>
  hasSchema.value ? validateDoc(dto.value!.schema, model.value) : [],
)
const readonlyJson = computed(() => JSON.stringify(model.value ?? dto.value?.doc ?? {}, null, 2))

// ---- 提案流程 ----
const showProposal = ref(false)
const reason = ref('')
const creating = ref(false)
const evaluating = ref(false)
const publishing = ref(false)
const discarding = ref(false)
const proposal = ref<ProposalDto | null>(null)
const impact = computed<ImpactResult | null>(() => proposal.value?.impact ?? null)
const acknowledged = ref(false)

const publishWarnings = computed(() => (impact.value ? impactWarnings(impact.value) : []))

function resetFlow(): void {
  showProposal.value = false
  reason.value = ''
  proposal.value = null
  acknowledged.value = false
}

async function submitProposal(): Promise<void> {
  if (formErrors.value.length) {
    message.error(`表单存在 ${formErrors.value.length} 处校验错误，请修正后再提交提案`)
    return
  }
  if (!reason.value.trim()) {
    message.error('变更理由必填（版本沿革留痕）')
    return
  }
  creating.value = true
  try {
    proposal.value = await ontologyGenericApi.createProposal(
      caseId.value, fileName.value, model.value, reason.value.trim(),
    )
    await evaluateImpact()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    creating.value = false
  }
}

async function evaluateImpact(): Promise<void> {
  if (!proposal.value) return
  evaluating.value = true
  try {
    proposal.value = await ontologyGenericApi.proposalImpact(
      caseId.value, proposal.value.proposal_id,
    )
    acknowledged.value = false
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    evaluating.value = false
  }
}

async function confirmPublish(): Promise<void> {
  if (!proposal.value || !canPublishProposal(proposal.value)) return
  if (!acknowledged.value) {
    message.error('请先勾选确认：已逐条核对影响面（含已固证线索）')
    return
  }
  publishing.value = true
  try {
    const res = await ontologyGenericApi.publishProposal(
      caseId.value, proposal.value.proposal_id,
    )
    message.success(
      `提案已发布（本体版本 ${res.ontology_version.slice(0, 8)}…）` +
        (res.needs_rebuild ? '；存在受影响物化表，记得重跑 BUILD。' : ''),
    )
    resetFlow()
    await load()
  } catch (e) {
    // E4-1/E4-3 等门禁：后端消息直接展示，并刷新提案状态
    message.error(isApiError(e) ? e.message : presentError(e).title)
    try {
      if (proposal.value) {
        proposal.value = await ontologyGenericApi.getProposal(
          caseId.value, proposal.value.proposal_id,
        )
      }
    } catch {
      /* 状态刷新失败不阻断 */
    }
  } finally {
    publishing.value = false
  }
}

async function discardProposal(): Promise<void> {
  if (!proposal.value || !canDiscardProposal(proposal.value)) return
  discarding.value = true
  try {
    proposal.value = await ontologyGenericApi.discardProposal(
      caseId.value, proposal.value.proposal_id,
    )
    message.info('提案已废弃，本体文件未改动')
    resetFlow()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    discarding.value = false
  }
}

const fileTitle = computed(() => FILE_LABELS[fileName.value] ?? fileName.value)
</script>

<template>
  <div class="gcv-page">
    <div class="gcv-head">
      <span class="gcv-title">{{ fileTitle }}</span>
      <span class="mono dim">{{ fileName }}.json</span>
      <NTag v-if="isReadonlyFile" size="tiny" type="warning" :bordered="false">🔒 仅只读 · 无表单</NTag>
      <NTag v-else-if="dto?.writable" size="tiny" type="success" :bordered="false">可提案</NTag>
    </div>

    <NSpin v-if="loading" size="medium" class="gcv-spin" />
    <NAlert v-else-if="loadError" type="error" :show-icon="false">加载失败：{{ loadError }}</NAlert>

    <template v-else-if="dto">
      <!-- R6：llm_policy 永不生成表单，只读 JSON 展示（UC-S5-7） -->
      <NAlert v-if="isReadonlyFile" type="warning" class="gcv-banner">
        该文件为 LLM 策略声明（隔离/审计/出网控制），本期仅提供只读展示，不开放可视化编辑与提案。
        变更需走标准层治理流程。
      </NAlert>

      <!-- E2-1：schema 缺失兜底，不猜结构 -->
      <NAlert v-else-if="!hasSchema" type="warning" class="gcv-banner">
        该文件暂无结构声明（schema），无法安全渲染表单，按未知结构只读展示（原样保留，不丢字段）。
      </NAlert>

      <!-- 只读 JSON（llm_policy / 无 schema） -->
      <pre v-if="isReadonlyFile || !hasSchema" class="gcv-raw readonly">{{ readonlyJson }}</pre>

      <template v-else>
        <!-- E2-2：校验错误（必填/类型冲突/枚举越界） -->
        <NAlert
          v-if="formErrors.length"
          type="error"
          class="gcv-banner"
          title="表单校验错误（提交提案前必须修正）"
        >
          <ul class="gcv-err-list">
            <li v-for="(e, i) in formErrors.slice(0, 20)" :key="i">
              <span class="mono">{{ e.path || '(根)' }}</span>：{{ e.message }}
            </li>
            <li v-if="formErrors.length > 20">…另有 {{ formErrors.length - 20 }} 处</li>
          </ul>
        </NAlert>

        <SchemaNode
          :schema="dto.schema as NonNullable<GenericFileDto['schema']>"
          :model-value="model"
          @update:model-value="(v) => (model = v as Record<string, unknown>)"
        />

        <!-- F5：schema 未声明字段，只读保留、原样回传（不静默丢失） -->
        <div v-if="unknownFields.length" class="gcv-unknown">
          <div class="gcv-sec-title">
            ⚠ 未在 schema 中声明的字段（{{ unknownFields.length }}）— 只读保留，提交时原样带回，不会丢失
          </div>
          <div v-for="u in unknownFields" :key="u.path" class="gcv-unknown-item">
            <div class="mono gcv-unknown-path">{{ u.path }}</div>
            <pre class="gcv-raw">{{ JSON.stringify(u.value, null, 2) }}</pre>
          </div>
        </div>

        <!-- 底部操作：唯一写路径是「提案 → 影响面 → 人工发布」（R4） -->
        <div class="gcv-actions">
          <NButton
            type="primary"
            :disabled="!dirty || !!formErrors.length"
            @click="showProposal = true"
          >
            提交变更提案
          </NButton>
          <span v-if="!dirty" class="dim gcv-hint">无未保存修改</span>
          <span v-else-if="formErrors.length" class="gcv-hint gcv-bad">请先修正 {{ formErrors.length }} 处校验错误</span>
        </div>
      </template>
    </template>

    <!-- 提案弹窗：① 理由 → ② 影响面 → ③ 人工确认发布 -->
    <NModal
      v-model:show="showProposal"
      preset="card"
      title="本体变更提案（轻量版：不自动发布）"
      style="width: 760px; max-width: 94vw"
      :mask-closable="false"
    >
      <div class="pp-flow">
        <div class="pp-status">
          <NTag
            size="small"
            :type="proposal ? (PROPOSAL_STATUS_META[proposal.status]?.color ?? 'default') : 'default'"
          >
            {{ proposal ? PROPOSAL_STATUS_META[proposal.status]?.label : '① 填写理由' }}
          </NTag>
          <span v-if="proposal" class="mono dim">{{ proposal.proposal_id }}</span>
        </div>

        <!-- ① 理由 -->
        <div v-if="!proposal" class="pp-step">
          <div class="pp-label">变更理由（必填，写入版本沿革与审计链）：</div>
          <NInput
            v-model:value="reason"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="说明为什么改、预期影响与回滚方式。提案仅存草稿，不生效、不触发 BUILD、不迁移线索。"
          />
          <div class="pp-btns">
            <NButton @click="resetFlow">取消</NButton>
            <NButton type="primary" :loading="creating" :disabled="!reason.trim()" @click="submitProposal">
              提交并评估影响面
            </NButton>
          </div>
        </div>

        <!-- ② 影响面 -->
        <div v-else class="pp-step">
          <NSpin v-if="evaluating" size="small">正在计算影响面…</NSpin>
          <template v-else-if="impact">
            <ImpactPanel :impact="impact" />
            <!-- D10：failed 时禁止发布 -->
            <NAlert
              v-if="impactBlocksPublish(impact)"
              type="error"
              class="gcv-banner"
              title="影响面不可用，禁止发布"
            >
              影响面计算失败时不得按「无影响」处理。请修复数据源后重新评估，或废弃本提案。
            </NAlert>
            <NCheckbox
              v-else
              v-model:checked="acknowledged"
              class="pp-ack"
            >
              我已逐条核对上述影响面（{{ impact.totals.total ?? 0 }} 项命中{{ publishWarnings.some((w) => w.level === 'danger') ? '，含已固证线索/不可用类别' : '' }}），确认人工发布此变更
            </NCheckbox>
          </template>
          <div v-else class="dim">影响面尚未生成。</div>

          <div class="pp-btns">
            <NButton
              type="error"
              ghost
              :loading="discarding"
              :disabled="!canDiscardProposal(proposal)"
              @click="discardProposal"
            >
              废弃提案（本体不变）
            </NButton>
            <NButton
              v-if="proposal.status === 'impact_ready'"
              size="small"
              @click="evaluateImpact"
            >
              重新评估影响面
            </NButton>
            <NButton
              type="primary"
              :loading="publishing"
              :disabled="!canPublishProposal(proposal) || !acknowledged"
              @click="confirmPublish"
            >
              🔴 确认发布（人工）
            </NButton>
          </div>
        </div>
      </div>
    </NModal>
  </div>
</template>

<style scoped>
.gcv-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.gcv-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.gcv-title {
  font-size: 15px;
  font-weight: 600;
}
.gcv-spin {
  padding: 40px 0;
}
.gcv-banner {
  font-size: 12px;
}
.gcv-err-list {
  margin: 4px 0 0;
  padding-left: 18px;
}
.gcv-raw {
  background: var(--sun-input-bg);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 10px;
  font-family: var(--sun-font-mono);
  font-size: 11px;
  line-height: 1.6;
  max-height: 56vh;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.gcv-raw.readonly {
  max-height: 64vh;
}
.gcv-unknown {
  border-top: 1px dashed var(--sun-border);
  padding-top: 10px;
  margin-top: 8px;
}
.gcv-sec-title {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 6px;
}
.gcv-unknown-item {
  margin-bottom: 8px;
}
.gcv-unknown-path {
  font-size: 11px;
  color: var(--sun-warn-text);
  margin-bottom: 2px;
}
.gcv-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  border-top: 1px solid var(--sun-border);
  padding-top: 12px;
}
.gcv-hint {
  font-size: 12px;
}
.gcv-bad {
  color: var(--sun-error-text);
}
.pp-flow {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pp-status {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pp-label {
  font-size: 12px;
  margin-bottom: 6px;
}
.pp-btns {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.pp-ack {
  margin-top: 10px;
  font-size: 12px;
}
.dim {
  color: var(--sun-text-tertiary);
}
.mono {
  font-family: var(--sun-font-mono);
}
</style>
