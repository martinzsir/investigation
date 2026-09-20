<script setup lang="ts">
// ★ REQ-V-006 VerifyWorkbench：线索详情页「核查工作区」。
// 核查过程在此累积：进度（已结 x/y + 状态分布 chips）、核查项列表、
// 行内状态机动作（合法动作由 domain/verify.ts 同构表现算，非法动作不渲染）、
// 人工添加入口。写动作统一走单门禁弹窗（对齐 ClueStatusMachine 门禁序）：
//   终态裁决（已证实/已查否）结论必填 → 二次确认 + 署名审计链；
//   采纳建议项（建议→待核查）可在弹窗内「改一改」改写文本；
// 组件只 emit payload，真实 API 由 ClueDetailView 薄编排（提交后调 refresh()）。
// degraded（FE-T-010 延续）：全部写按钮禁用 + 降级通栏。
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { NModal, NButton, NInput, NIcon, NSelect, NRadioGroup, NRadio, useMessage } from 'naive-ui'
import { WarningOutline, RefreshOutline, AddOutline, SparklesOutline } from '@vicons/ionicons5'
import {
  allowedVerifyActions,
  assembleVerifyText,
  draftModeHint,
  draftResultSummary,
  entityCandidates,
  isAiDraftOrigin,
  isManualOrigin,
  isVerifySideStatus,
  replayResultExpandable,
  replayResultJson,
  replayResultSummary,
  replaySourceLabel,
  verifyKindLabel,
  verifyProgressPercent,
  verifyStatusMeta,
  VERIFY_STATUS_ORDER,
  type DraftMode,
  type EvidenceMaterial,
  type VerifyAction,
  type VerifyItem,
  type VerifyItemsPage,
  type VerifyProgress,
} from '../../domain/verify'
import { degradeReason } from '../../domain/clue'
import { verifyApi, requestApi } from '../../api/endpoints/verify'
import { evidenceApi, saveBlobAs } from '../../api/endpoints/evidence'
import { presentError } from '../../api/errors'
import EvidencePanel from './EvidencePanel.vue'
import {
  legalRequestTargets,
  type VerifyRequestRow,
} from '../../domain/verify'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'

const props = defineProps<{
  caseId: string
  clueId: string
  operator: string
  role: string
  degraded: boolean
  /** 父层写请求进行中（提交后入队回执前禁用所有写按钮） */
  submitting?: boolean
  /**
   * REQ-V-018 结构化构造器数据源：线索 source_rows（ClueDetail.source_rows）。
   * 缺省/空 → 构造器不渲染，回落纯手填（可选 prop，旧用例零影响）。
   */
  sourceRows?: Array<Record<string, unknown>>
}>()

const emit = defineEmits<{
  /** 人工添加：父层调 verifyApi.add 后 refresh() */
  add: [payload: { text: string }]
  /** 状态迁移：父层调 verifyApi.transition 后 refresh() */
  transition: [payload: {
    item_id: string
    next_status: string
    conclusion?: string
    text?: string
  }]
  /** REQ-V-017 一键复跑：父层调 verifyApi.replay（202 终态）后 refresh() */
  replay: [payload: { item_id: string }]
  /** REQ-V-007：清单加载/刷新后回传整页，父层构建「文本→状态」映射喂三栏联动 */
  loaded: [page: VerifyItemsPage]
  /** REQ-V-013 创建调取请求：父层调 requestApi.create 后 refresh() */
  requestCreate: [payload: {
    target: string
    material: string
    legal_instrument: string
    handler: string
    due_date: string
    item_id?: string
  }]
  /** REQ-V-013 调取请求迁移（发起/回执登记/关闭）：父层调 requestApi.transition 后 refresh() */
  requestTransition: [payload: { request_id: string; next_status: string }]
  /** REQ-V-011 书证挂接核查项：父层 202 入队（op=link）终态后 refresh() */
  evidenceLink: [payload: { item_id: string; material_id: string }]
  /** REQ-V-011 解除书证挂接：父层 202 入队（op=unlink）终态后 refresh() */
  evidenceUnlink: [payload: { material_id: string }]
}>()

// ---------- 读面（自加载；父层写后经 expose 的 refresh 拉新） ----------
const items = ref<VerifyItem[]>([])
const progress = ref<VerifyProgress>({
  total: 0, concluded: 0, pending: 0, suggested: 0, ignored: 0, by_status: {},
})
const available = ref(false)
const loading = ref(false)
const errorMsg = ref('')

async function refresh(): Promise<void> {
  if (!props.caseId || !props.clueId) return
  loading.value = true
  errorMsg.value = ''
  try {
    const page: VerifyItemsPage = await verifyApi.list(props.caseId, props.clueId)
    available.value = page.available
    items.value = page.items ?? []
    progress.value = page.progress
    // 回传统一化快照（items 缺省 [] 与渲染口径一致）
    emit('loaded', {
      items: items.value, progress: progress.value, available: available.value,
    })
  } catch (e) {
    // 不吞错：红条提示 + 重试（父层亦可用全局 message 提示写失败）
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
  // REQ-V-013：台账独立加载（错误不阻塞核查项面板）
  void refreshRequests()
  // REQ-V-010：书证清单独立加载（面板可能在错误重试等场景下尚未挂载，容错）
  void evidencePanelRef.value?.refresh()
}

// ---------- 人工添加（jumpTo 未命中时也要预填，提前声明） ----------
const newText = ref('')

// ---------- REQ-V-007：三栏待核实卡 → 工作台联动 ----------
const rootEl = ref<HTMLElement | null>(null)
/** 当前高亮文本（同文本核查项可能多条，按文本整体高亮） */
const flashText = ref('')
let flashTimer: ReturnType<typeof setTimeout> | undefined
const FLASH_MS = 2400

function clearFlash(): void {
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = undefined
  flashText.value = ''
}

onUnmounted(clearFlash)

/**
 * 三栏「待核实」卡点击入口：
 * - 命中同文本核查项 → 滚动入视 + 青色高亮 2.4s，返回 true；
 * - 未命中（尚未供给/人工新事项）→ 滚动到工作台并把文本预填进人工添加框聚焦，返回 false。
 * happy-dom 无 scrollIntoView，可选链兜底。
 */
async function jumpTo(text: string): Promise<boolean> {
  const target = text.trim()
  if (!target || !rootEl.value) return false
  await nextTick()
  const hits = Array.from(rootEl.value.querySelectorAll<HTMLElement>('.vw-item'))
    .filter((li) => li.dataset.text === target)
  if (hits.length > 0) {
    if (flashTimer) clearTimeout(flashTimer)
    flashText.value = target
    hits[0].scrollIntoView?.({ behavior: 'smooth', block: 'center' })
    flashTimer = setTimeout(clearFlash, FLASH_MS)
    return true
  }
  // 未命中：落人工添加入口（降级态输入框禁用，仅滚动到位）
  newText.value = target
  rootEl.value.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  const ta = rootEl.value.querySelector<HTMLTextAreaElement>('textarea')
  ta?.focus?.()
  return false
}

defineExpose({ refresh, jumpTo })
watch(() => [props.caseId, props.clueId], refresh, { immediate: true })

// ---------- 人工添加 ----------
const addDisabled = computed(
  () => props.degraded || !!props.submitting || !newText.value.trim(),
)

function submitAdd(): void {
  const text = newText.value.trim()
  if (!text || addDisabled.value) return
  emit('add', { text })
  newText.value = ''
}

// ---------- REQ-V-019 AI 建议核查方向（同步草案；只落提案队列待人审，不成项） ----------
const draftLoading = ref(false)
/** 草案结果条：ok=绿 / degraded=黄 / blocked·请求失败=红（均不阻塞工作区） */
const draftNote = ref<{ kind: 'ok' | 'warn' | 'error'; text: string } | null>(null)

async function fetchDraft(): Promise<void> {
  if (props.degraded || props.submitting || draftLoading.value) return
  if (!props.caseId || !props.clueId) return
  draftLoading.value = true
  draftNote.value = null
  try {
    const res = await verifyApi.draft(props.caseId, props.clueId)
    if (res.ok) {
      // 草案只落提案队列（shadow）：不 refresh() 核查项列表（未成项）
      const mode = (res.mode ?? 'off') as DraftMode
      draftNote.value = {
        kind: (res.proposals?.length ?? 0) > 0 ? 'ok' : 'warn',
        text: draftResultSummary(
          mode, res.proposals?.length ?? 0,
          res.duplicates ?? 0, res.dropped?.length ?? 0),
      }
    } else if (res.degraded) {
      draftNote.value = {
        kind: 'warn',
        text: `AI 草案不可用（${draftModeHint((res.mode ?? 'off') as DraftMode)}）：${res.reason ?? ''}`,
      }
    } else {
      draftNote.value = {
        kind: 'error',
        text: `AI 草案被拦截：${res.error ?? '未知原因'}`,
      }
    }
  } catch (e) {
    draftNote.value = {
      kind: 'error',
      text: `AI 草案请求失败：${e instanceof Error ? e.message : String(e)}`,
    }
  } finally {
    draftLoading.value = false
  }
}

// ---------- REQ-V-018：结构化构造器（实体→维度→渠道→核查点，拼装后可改） ----------
const { config: ontologyCfg } = useCaseOntologyConfig()

const entityOptions = computed<{ label: string; value: string }[]>(() =>
  entityCandidates(props.sourceRows).map((v) => ({ label: v, value: v })),
)

// 下拉 value 用 code（机器标识符，与 rules/产物口径一致），label 用 name（展示）。
// 旧声明无 code 时回落 name，与后端 load_dimension_declarations 同口径。
const dimensionOptions = computed<{ label: string; value: string }[]>(() =>
  (ontologyCfg.value.dimensions ?? []).map((d) => ({
    label: d.name,
    value: d.code || d.name,
  })),
)

const entity = ref('')
const dimension = ref('')
const channel = ref<'function' | 'external'>('function')
const extTarget = ref('')
const extMaterial = ref('')
const constructPoint = ref('')

/** 构造器是否可用：有实体候选才渲染（sourceRows 缺省 → 纯手填兜底） */
const constructorReady = computed(() => entityOptions.value.length > 0)

/** 拼装到人工添加框：确定性模板（domain 纯函数），用户可「改一改」后再提交 */
function assembleText(): void {
  newText.value = assembleVerifyText({
    entity: entity.value,
    dimension: dimension.value,
    point: constructPoint.value,
    channel: channel.value,
    extTarget: extTarget.value,
    extMaterial: extMaterial.value,
  })
  void nextTick(() => {
    rootEl.value?.querySelector<HTMLTextAreaElement>('textarea')?.focus?.()
  })
}

// ---------- 行动作 → 单门禁弹窗（动作延续契约：校验通过后恢复 emit） ----------
const modalOpen = ref(false)
const pendingItem = ref<VerifyItem | null>(null)
const pendingAction = ref<VerifyAction | null>(null)
const conclusion = ref('')
const rewriteText = ref('')

const degradeMsg = computed(() => degradeReason(props.degraded))
const percent = computed(() => verifyProgressPercent(progress.value))
/** 状态分布 chips：按展示序输出非零计数 */
const chips = computed(() =>
  VERIFY_STATUS_ORDER
    .map((s) => ({ status: s, n: progress.value.by_status?.[s] ?? 0 }))
    .filter((c) => c.n > 0),
)

function actionsOf(item: VerifyItem): VerifyAction[] {
  return allowedVerifyActions(item.status)
}

/** REQ-V-018：采纳后（待核查/核查中）才显示路由按钮；建议态用渠道徽标即可 */
function routeActive(item: VerifyItem): boolean {
  return item.status === '待核查' || item.status === '核查中'
}

/** REQ-V-017 一键复跑：只 emit，202 入队/终态等待/刷新由父层编排（同 transition 纪律） */
function runReplay(item: VerifyItem): void {
  if (props.degraded || props.submitting) return
  emit('replay', { item_id: item.item_id })
}

// REQ-V-017 方案A：结果卡片原始 JSON 折叠（默认收起，按 item_id 记忆展开态）。
// 纯前端展示开关，不触发任何请求；刷新后回落收起。
const replayDetailOpen = ref<Record<string, boolean>>({})

function toggleReplayDetail(itemId: string): void {
  replayDetailOpen.value[itemId] = !replayDetailOpen.value[itemId]
}

// ---------- REQ-V-010/011 书证（面板挂载 + 卡片内下载/解除） ----------
const message = useMessage()
const evidencePanelRef = ref<InstanceType<typeof EvidencePanel> | null>(null)
/** 卡片内下载进行中的 material_id（防止重复点击） */
const evidenceBusyId = ref('')

/** REQ-V-010 已挂书证下载（同步字节流；不走 202 编排） */
async function doDownloadEvidence(ev: EvidenceMaterial): Promise<void> {
  if (evidenceBusyId.value || !props.caseId || !props.clueId) return
  evidenceBusyId.value = ev.material_id
  try {
    const blob = await evidenceApi.download(
      props.caseId, props.clueId, ev.material_id)
    saveBlobAs(blob, ev.orig_name || ev.filename || ev.material_id)
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    evidenceBusyId.value = ''
  }
}

/** REQ-V-011 解除挂接：emit 给父层 202 编排（同 transition 红线禁用） */
function unlinkEvidence(materialId: string): void {
  if (props.degraded || props.submitting) return
  emit('evidenceUnlink', { material_id: materialId })
}

/** REQ-V-011 面板挂接动作透传（面板自身已做禁用判断，此处仅转发） */
function onEvidenceLink(payload: { item_id: string; material_id: string }): void {
  emit('evidenceLink', payload)
}

/** REQ-V-011 面板解除动作透传 */
function onEvidenceUnlink(payload: { material_id: string }): void {
  emit('evidenceUnlink', payload)
}

function statusStyle(item: VerifyItem): Record<string, string> {
  const m = verifyStatusMeta(item.status)
  return { color: m.text, borderColor: m.border, background: m.bg }
}

function openAction(item: VerifyItem, action: VerifyAction): void {
  if (props.degraded || props.submitting) return
  pendingItem.value = item
  pendingAction.value = action
  conclusion.value = ''
  // 采纳：预填建议原文，用户可「改一改」；不改则原文采纳
  rewriteText.value = action.rewrite ? item.text : ''
  modalOpen.value = true
}

const confirmDisabled = computed(() => {
  if (props.submitting) return true
  const a = pendingAction.value
  if (!a) return true
  if (a.conclusionRequired && !conclusion.value.trim()) return true
  if (a.rewrite && !rewriteText.value.trim()) return true
  return false
})

function confirm(): void {
  const item = pendingItem.value
  const action = pendingAction.value
  if (!item || !action || confirmDisabled.value) return
  const payload: {
    item_id: string
    next_status: string
    conclusion?: string
    text?: string
  } = { item_id: item.item_id, next_status: action.target }
  const c = conclusion.value.trim()
  if (c) payload.conclusion = c
  // 采纳：仅当「改一改」确实改动且非空才携带 text（未改=原文采纳，不产生覆写）
  if (action.rewrite) {
    const t = rewriteText.value.trim()
    if (t && t !== item.text) payload.text = t
  }
  emit('transition', payload)
  modalOpen.value = false
  pendingItem.value = null
  pendingAction.value = null
}

function cancel(): void {
  modalOpen.value = false
  pendingItem.value = null
  pendingAction.value = null
}

// ---------- REQ-V-013：调取清单台账（本线索催办面；状态机镜像 domain） ----------
const reqItems = ref<VerifyRequestRow[]>([])
const reqAvailable = ref(false)
const reqErrorMsg = ref('')
const reqFormOpen = ref(false)
const reqTarget = ref('')
const reqMaterial = ref('')
const reqLegal = ref('')
const reqHandler = ref('')
const reqDue = ref('')

async function refreshRequests(): Promise<void> {
  if (!props.caseId || !props.clueId) return
  try {
    const page = await requestApi.list(props.caseId, props.clueId)
    reqAvailable.value = page.available
    reqItems.value = page.items ?? []
    reqErrorMsg.value = ''
  } catch (e) {
    reqErrorMsg.value = e instanceof Error ? e.message : String(e)
  }
}

/** 台账动作文案（已发起 → 发起；材料已回 → 回执登记；其余用状态名本身） */
function requestActionLabel(nxt: string): string {
  if (nxt === '已发起') return '发起'
  if (nxt === '材料已回') return '回执登记'
  return nxt
}

const reqCreateDisabled = computed(
  () => props.degraded || !!props.submitting
    || !reqTarget.value.trim() || !reqMaterial.value.trim(),
)

/** 「转调取台账」入口：预填外部渠道路由数据并展开登记表单 */
function prefillRequest(target: string, material: string): void {
  if (props.degraded || props.submitting) return
  reqTarget.value = target
  reqMaterial.value = material
  reqFormOpen.value = true
  rootEl.value?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
}

function submitRequestCreate(): void {
  if (reqCreateDisabled.value) return
  emit('requestCreate', {
    target: reqTarget.value.trim(),
    material: reqMaterial.value.trim(),
    legal_instrument: reqLegal.value.trim(),
    handler: reqHandler.value.trim(),
    due_date: reqDue.value.trim(),
  })
  reqTarget.value = ''
  reqMaterial.value = ''
  reqLegal.value = ''
  reqHandler.value = ''
  reqDue.value = ''
  reqFormOpen.value = false
}

// 迁移单门禁弹窗（同核查项纪律：二次确认 + 署名审计链）
const reqModalOpen = ref(false)
const pendingReq = ref<VerifyRequestRow | null>(null)
const pendingReqTarget = ref('')

function openRequestAction(row: VerifyRequestRow, nxt: string): void {
  if (props.degraded || props.submitting) return
  pendingReq.value = row
  pendingReqTarget.value = nxt
  reqModalOpen.value = true
}

function confirmRequest(): void {
  const row = pendingReq.value
  if (!row || !pendingReqTarget.value) return
  emit('requestTransition', {
    request_id: row.request_id,
    next_status: pendingReqTarget.value,
  })
  reqModalOpen.value = false
  pendingReq.value = null
}

function cancelRequest(): void {
  reqModalOpen.value = false
  pendingReq.value = null
}
</script>

<template>
  <div ref="rootEl" class="vw" data-testid="verify-workbench">
    <!-- 顶部：进度条 + 状态分布 -->
    <header class="vw-head">
      <div class="vw-progress-line">
        <span class="vw-progress-label">
          已结 <b class="mono">{{ progress.concluded }}</b>/<b class="mono">{{ progress.total }}</b>
        </span>
        <div class="vw-bar" aria-hidden="true">
          <div class="vw-bar-fill" :style="{ width: `${percent}%` }" />
        </div>
        <span class="vw-percent dim mono">{{ percent }}%</span>
        <NButton text size="tiny" class="vw-reload" :disabled="loading" @click="refresh">
          <NIcon :component="RefreshOutline" />
        </NButton>
      </div>
      <div class="vw-chips">
        <span
          v-for="c in chips"
          :key="c.status"
          class="vw-chip"
          :style="verifyStatusMeta(c.status)
            ? { color: verifyStatusMeta(c.status).text,
                borderColor: verifyStatusMeta(c.status).border }
            : {}"
        >
          {{ c.status }} {{ c.n }}
        </span>
        <span v-if="progress.pending > 0" class="vw-pending-note">
          {{ progress.pending }} 项待结
        </span>
      </div>
    </header>

    <!-- 降级通栏（FE-T-010）：写按钮全禁用 -->
    <p v-if="degradeMsg" class="vw-degrade" role="alert">
      <NIcon :component="WarningOutline" /> {{ degradeMsg }}
    </p>

    <!-- 错误/重试 -->
    <p v-if="errorMsg" class="vw-error" role="alert">
      <NIcon :component="WarningOutline" /> 核查项加载失败：{{ errorMsg }}
      <NButton text size="tiny" @click="refresh">重试</NButton>
    </p>

    <!-- 人工添加（REQ-V-018：结构化构造器三步引导 + 自由输入兜底） -->
    <div class="vw-add">
      <div v-if="constructorReady" class="vw-ctor" data-testid="vw-ctor">
        <div class="vw-ctor-row">
          <NSelect
            v-model:value="entity"
            size="small"
            :options="entityOptions"
            placeholder="1 · 实体（source_rows 候选）"
            :disabled="degraded || submitting"
            class="vw-ctor-entity"
            data-testid="vw-ctor-entity"
          />
          <NSelect
            v-model:value="dimension"
            size="small"
            :options="dimensionOptions"
            placeholder="2 · 维度"
            :disabled="degraded || submitting"
            class="vw-ctor-dim"
            data-testid="vw-ctor-dim"
          />
        </div>
        <div class="vw-ctor-row">
          <NRadioGroup v-model:value="channel" size="small" :disabled="degraded || submitting">
            <NRadio value="function">库内可复跑</NRadio>
            <NRadio value="external">外部调取</NRadio>
          </NRadioGroup>
          <NButton
            size="small"
            :disabled="degraded || submitting"
            data-testid="vw-ctor-assemble"
            @click="assembleText"
          >
            拼装到输入框
          </NButton>
        </div>
        <div v-if="channel === 'external'" class="vw-ctor-row">
          <NInput
            v-model:value="extTarget"
            size="small"
            :disabled="degraded || submitting"
            placeholder="调取对象（如：住建局招标办）"
            data-testid="vw-ctor-target"
          />
          <NInput
            v-model:value="extMaterial"
            size="small"
            :disabled="degraded || submitting"
            placeholder="调取材料"
            data-testid="vw-ctor-material"
          />
        </div>
        <NInput
          v-model:value="constructPoint"
          size="small"
          :disabled="degraded || submitting"
          placeholder="3 · 核查点（要核实什么）"
          data-testid="vw-ctor-point"
        />
      </div>
      <NInput
        v-model:value="newText"
        type="textarea"
        :rows="2"
        :disabled="degraded || submitting"
        :placeholder="degraded ? '系统降级运行中，写操作已临时禁用' : '添加人工核查项：把待核实的事实/假设拆成一条可裁决的动作'"
        @keydown.ctrl.enter="submitAdd"
      />
      <div class="vw-add-foot">
        <!-- REQ-V-019：AI 建议核查方向（同步草案，落提案队列待人审；degraded 禁用） -->
        <NButton
          size="small"
          class="vw-ai-draft"
          :disabled="degraded || submitting"
          :loading="draftLoading"
          data-testid="vw-ai-draft"
          @click="fetchDraft"
        >
          <NIcon :component="SparklesOutline" />
          AI 建议核查方向
        </NButton>
        <span class="dim">Ctrl+Enter 提交</span>
        <NButton
          size="small"
          type="primary"
          :disabled="addDisabled"
          :loading="submitting"
          @click="submitAdd"
        >
          <NIcon :component="AddOutline" />
          添加核查项
        </NButton>
      </div>
    </div>

    <!-- REQ-V-019 草案结果条：ok=绿 / degraded=黄 / blocked=红 -->
    <p
      v-if="draftNote"
      class="vw-draft-note"
      :class="`vw-draft-note--${draftNote.kind}`"
      role="status"
      data-testid="vw-draft-note"
    >
      <NIcon :component="WarningOutline" /> {{ draftNote.text }}
    </p>

    <!-- 列表 -->
    <p v-if="available && progress.total === 0" class="vw-empty">
      本线索暂无核查项——在上方添加第一条，或先打开线索详情由系统自动供给。
    </p>
    <p v-else-if="!available" class="vw-empty dim">
      核查工作区尚未初始化（state.sqlite 缺失）。
    </p>

    <ul v-else class="vw-list">
      <li
        v-for="it in items"
        :key="it.item_id"
        class="vw-item"
        :class="{
          'vw-item--side': isVerifySideStatus(it.status),
          'vw-item--flash': it.text === flashText,
        }"
        :data-text="it.text"
      >
        <div class="vw-item-main">
          <div class="vw-item-badges">
            <span class="vw-status" :style="statusStyle(it)">{{ it.status }}</span>
            <span class="vw-kind vw-kind--tag">{{ verifyKindLabel(it.kind) }}</span>
            <span v-if="isManualOrigin(it.origin)" class="vw-origin-manual">人工</span>
            <span v-if="isAiDraftOrigin(it.origin)" class="vw-origin-ai" data-testid="vw-origin-ai">AI 草案</span>
            <span v-if="it.channel === 'function' && it.ref_function" class="vw-route">
              可复跑 · <code class="mono">{{ it.ref_function }}</code>
            </span>
            <span v-else-if="it.channel === 'external'" class="vw-route vw-route--external">
              外部调取<template v-if="it.external?.target"> · {{ it.external.target }}</template><template v-if="it.external?.material">（{{ it.external.material }}）</template>
            </span>
          </div>
          <p class="vw-text">{{ it.text }}</p>
          <p v-if="it.falsification" class="vw-fals">
            证伪条件：{{ it.falsification }}
          </p>
          <p v-if="it.conclusion" class="vw-conclusion">
            <span class="vw-conclusion-tag">结论</span>{{ it.conclusion }}
          </p>
          <!-- REQ-V-017 复跑结果卡片：只读 Function 回填（AI 辅助推演·需人确认，
               不改状态/结论，永不参与固证门禁判定） -->
          <div v-if="it.replay" class="vw-replay" data-testid="vw-replay-card">
            <div class="vw-replay-head">
              <span class="vw-replay-tag">内核核查</span>
              <code class="mono">{{ it.replay.function }}</code>
              <span
                v-if="it.replay.fallback_used"
                class="vw-replay-badge"
                title="主跑 Function 失败，备选 Function 接管"
              >备选接管</span>
              <span
                v-if="it.replay.degraded"
                class="vw-replay-badge"
                :title="it.replay.degraded_reason ?? ''"
              >结构降级</span>
            </div>
            <p class="vw-replay-summary">{{ replayResultSummary(it.replay) }}</p>
            <!-- 方案A：原始结果折叠明细（对象/非空行数组才有入口；
                 只读展示内核回填 JSON，不解释业务语义） -->
            <button
              v-if="replayResultExpandable(it.replay)"
              type="button"
              class="vw-replay-toggle"
              data-testid="vw-replay-toggle"
              @click="toggleReplayDetail(it.item_id)"
            >
              {{ replayDetailOpen[it.item_id] ? '收起原始结果' : '查看原始结果' }}
            </button>
            <pre
              v-show="replayDetailOpen[it.item_id]"
              class="vw-replay-detail"
              data-testid="vw-replay-detail"
            >{{ replayResultJson(it.replay) }}</pre>
            <p class="vw-meta dim">
              {{ replaySourceLabel(it.replay.mapping_source) }}
              · {{ it.replay.replayed_at }}<template v-if="it.replay.operator"> · {{ it.replay.operator }}</template>
              · {{ it.replay.version }}
            </p>
          </div>
          <!-- REQ-V-011 已挂书证（verify-items 行内投影）：原名下载 + 解除挂接；
               挂接入口在下方书证面板（未挂接材料选择核查项） -->
          <div
            v-if="it.evidence && it.evidence.length"
            class="vw-ev"
            data-testid="vw-item-evidence"
          >
            <span class="vw-ev-title">已挂书证</span>
            <span
              v-for="ev in it.evidence"
              :key="ev.material_id"
              class="vw-ev-chip"
              :data-material-id="ev.material_id"
            >
              <button
                type="button"
                class="vw-ev-link"
                :disabled="evidenceBusyId === ev.material_id"
                :title="`下载：${ev.orig_name}`"
                data-testid="vw-ev-download"
                @click="doDownloadEvidence(ev)"
              >{{ ev.orig_name }}</button>
              <button
                type="button"
                class="vw-ev-unlink"
                :disabled="degraded || submitting"
                title="解除挂接（材料保留，可重新挂接）"
                data-testid="vw-ev-unlink"
                @click="unlinkEvidence(ev.material_id)"
              >解除</button>
            </span>
          </div>
          <p class="vw-meta dim">
            <template v-if="it.operator">{{ it.operator }} · </template>
            <template v-if="it.updated_at">{{ it.updated_at }} · </template>
            <code class="mono">{{ it.item_id }}</code>
          </p>
        </div>
        <div class="vw-item-actions">
          <NButton
            v-for="a in actionsOf(it)"
            :key="a.kind"
            size="tiny"
            :class="['vw-act', `vw-act--${a.tone}`]"
            :disabled="degraded || submitting"
            @click="openAction(it, a)"
          >
            {{ a.label }}
          </NButton>
          <!-- REQ-V-018 采纳后路由（REQ-V-017 已接线）：库内复跑按钮 emit replay，
               202 入队由 Worker 调只读 Function 回填 replay_json（不改状态/结论）；
               外部渠道按钮接台账（REQ-V-013），点击预填调取登记表单 -->
          <NButton
            v-if="routeActive(it) && it.channel === 'function'"
            size="tiny"
            class="vw-route-btn"
            :disabled="degraded || submitting"
            :title="`运行内核核查（${it.ref_function}）：只读复跑，结果回填但不改状态/结论`"
            data-testid="vw-route-function"
            @click="runReplay(it)"
          >
            ▶ 运行内核核查
          </NButton>
          <NButton
            v-else-if="routeActive(it) && it.channel === 'external'"
            size="tiny"
            class="vw-route-btn"
            :disabled="degraded || submitting"
            :title="`转调取台账（${it.external?.target ?? ''}）`"
            data-testid="vw-route-external"
            @click="prefillRequest(it.external?.target ?? '', it.external?.material ?? '')"
          >
            转调取台账
          </NButton>
        </div>
      </li>
    </ul>

    <!-- REQ-V-010/011 书证材料：上传/清单/下载自管理；挂接/解除 emit 202 编排 -->
    <EvidencePanel
      ref="evidencePanelRef"
      :case-id="caseId"
      :clue-id="clueId"
      :degraded="degraded"
      :submitting="submitting"
      :items="items"
      @link="onEvidenceLink"
      @unlink="onEvidenceUnlink"
    />

    <!-- REQ-V-013：调取清单台账（本线索催办面；超期徽标为服务端读面派生） -->
    <section class="vw-req" data-testid="vw-requests">
      <header class="vw-req-head">
        <h3 class="vw-req-title">
          调取清单 <b class="mono">{{ reqItems.length }}</b>
        </h3>
        <NButton
          size="tiny"
          :disabled="degraded || submitting"
          data-testid="vw-req-new"
          @click="reqFormOpen = !reqFormOpen"
        >
          {{ reqFormOpen ? '收起' : '发起登记' }}
        </NButton>
      </header>
      <p v-if="reqErrorMsg" class="vw-error" role="alert">
        <NIcon :component="WarningOutline" /> 台账加载失败：{{ reqErrorMsg }}
        <NButton text size="tiny" @click="refreshRequests">重试</NButton>
      </p>
      <div v-if="reqFormOpen" class="vw-req-form" data-testid="vw-req-form">
        <NInput
          v-model:value="reqTarget"
          size="small"
          placeholder="调取对象（如：海州银行营业部）"
          :disabled="degraded || submitting"
          data-testid="vw-req-target"
        />
        <NInput
          v-model:value="reqMaterial"
          size="small"
          placeholder="调取材料"
          :disabled="degraded || submitting"
          data-testid="vw-req-material"
        />
        <div class="vw-req-form-row">
          <NInput
            v-model:value="reqLegal"
            size="small"
            placeholder="法律手续（调取函/审批文号，选填）"
            :disabled="degraded || submitting"
          />
          <NInput
            v-model:value="reqHandler"
            size="small"
            placeholder="经办人（选填）"
            :disabled="degraded || submitting"
          />
          <NInput
            v-model:value="reqDue"
            size="small"
            placeholder="期限 YYYY-MM-DD"
            :disabled="degraded || submitting"
          />
        </div>
        <div class="vw-req-form-foot">
          <span class="dim">登记后进入台账跟踪与审计链留痕</span>
          <NButton
            size="small"
            type="primary"
            :disabled="reqCreateDisabled"
            :loading="submitting"
            data-testid="vw-req-submit"
            @click="submitRequestCreate"
          >
            登记调取请求
          </NButton>
        </div>
      </div>
      <p v-if="reqAvailable && reqItems.length === 0 && !reqFormOpen"
         class="vw-empty dim">
        暂无调取登记——外部调取事项建议先登记台账再催办。
      </p>
      <ul v-if="reqItems.length > 0" class="vw-req-list">
        <li
          v-for="rq in reqItems"
          :key="rq.request_id"
          class="vw-req-item"
          :data-request-id="rq.request_id"
        >
          <div class="vw-item-badges">
            <span class="vw-status">{{ rq.status }}</span>
            <span v-if="rq.overdue" class="vw-overdue" data-testid="vw-req-overdue">
              超期
            </span>
          </div>
          <p class="vw-text">{{ rq.target }} ← {{ rq.material }}</p>
          <p class="vw-meta dim">
            <template v-if="rq.legal_instrument">{{ rq.legal_instrument }} · </template>
            <template v-if="rq.handler">{{ rq.handler }} · </template>
            <template v-if="rq.due_date">期限 {{ rq.due_date }} · </template>
            <template v-if="rq.created_by">{{ rq.created_by }} · </template>
            <code class="mono">{{ rq.request_id }}</code>
          </p>
          <div class="vw-item-actions">
            <NButton
              v-for="nxt in legalRequestTargets(rq.status)"
              :key="nxt"
              size="tiny"
              :class="['vw-act', nxt === '关闭' ? 'vw-act--muted' : 'vw-act--primary']"
              :disabled="degraded || submitting"
              :data-testid="`vw-req-act-${nxt}`"
              @click="openRequestAction(rq, nxt)"
            >
              {{ requestActionLabel(nxt) }}
            </NButton>
          </div>
        </li>
      </ul>
    </section>

    <!-- 单门禁确认弹窗：结论必填 / 采纳改写 / 署名审计链 -->
    <NModal
      v-model:show="modalOpen"
      preset="card"
      :title="`核查确认 · ${pendingAction?.label ?? ''}`"
      class="vw-modal"
      :mask-closable="false"
    >
      <div class="vw-confirm-body" data-testid="vw-confirm">
        <p class="vw-confirm-transition" v-if="pendingItem">
          状态迁移：<b>{{ pendingItem.status }}</b> → <b class="target">{{ pendingAction?.target }}</b>
        </p>
        <p class="vw-confirm-note">
          将以 <b>{{ operator }}（{{ role }}）</b> 名义提交，操作进入审计链留痕。
        </p>

        <template v-if="pendingAction?.rewrite">
          <label class="vw-field-label">
            采纳建议文本（可「改一改」；不改则原文采纳）
          </label>
          <NInput
            v-model:value="rewriteText"
            type="textarea"
            :rows="3"
            placeholder="核查项文本"
            data-testid="vw-rewrite"
          />
        </template>
        <template v-else>
          <label class="vw-field-label">
            核查结论{{ pendingAction?.conclusionRequired ? '（必填）' : '（选填；无法核实时可挂起转外部调取）' }}
          </label>
          <NInput
            v-model:value="conclusion"
            type="textarea"
            :rows="3"
            :placeholder="pendingAction?.conclusionRequired
              ? '据何证据得出该结论（必填）'
              : '核查情况说明'"
            data-testid="vw-conclusion"
          />
        </template>
      </div>
      <template #footer>
        <div class="vw-confirm-footer">
          <NButton size="small" @click="cancel">取消</NButton>
          <NButton
            size="small"
            type="primary"
            :disabled="confirmDisabled"
            :loading="submitting"
            data-testid="vw-confirm-btn"
            @click="confirm"
          >
            确认提交
          </NButton>
        </div>
      </template>
    </NModal>

    <!-- REQ-V-013 调取请求确认弹窗：同门禁序（二次确认 + 署名审计链） -->
    <NModal
      v-model:show="reqModalOpen"
      preset="card"
      title="调取清单确认"
      class="vw-modal"
      :mask-closable="false"
    >
      <div class="vw-confirm-body" data-testid="vw-req-confirm">
        <p class="vw-confirm-transition" v-if="pendingReq">
          状态迁移：<b>{{ pendingReq.status }}</b> →
          <b class="target">{{ pendingReqTarget }}</b>
        </p>
        <p class="vw-confirm-note">
          将以 <b>{{ operator }}（{{ role }}）</b> 名义提交，操作进入审计链留痕。
        </p>
      </div>
      <template #footer>
        <div class="vw-confirm-footer">
          <NButton size="small" @click="cancelRequest">取消</NButton>
          <NButton
            size="small"
            type="primary"
            :disabled="submitting"
            data-testid="vw-req-confirm-btn"
            @click="confirmRequest"
          >
            确认提交
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.vw {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
/* REQ-V-013 调取清单台账 */
.vw-req {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 1px dashed var(--sun-border);
  padding-top: 10px;
}
.vw-req-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.vw-req-title {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-secondary);
}
.vw-req-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.vw-req-form-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 6px;
}
.vw-req-form-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}
.vw-req-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.vw-req-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
}
.vw-overdue {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border: 1px solid var(--sun-error-border);
  border-radius: var(--sun-radius-badge, 12px);
  font-size: 11px;
  line-height: 16px;
  color: var(--sun-error-text);
  background: var(--sun-error-bg);
}
.vw-head {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.vw-progress-line {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.vw-progress-label b {
  color: var(--sun-border-active);
}
.vw-bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--sun-input-bg);
  border: 1px solid var(--sun-border);
  overflow: hidden;
}
.vw-bar-fill {
  height: 100%;
  background: var(--sun-gradient-primary);
  transition: width 0.3s ease;
}
.vw-percent {
  min-width: 38px;
  text-align: right;
}
.vw-reload {
  color: var(--sun-text-tertiary);
}
.vw-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.vw-chip {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border: 1px solid;
  border-radius: var(--sun-radius-badge, 12px);
  font-size: 11px;
  line-height: 16px;
  background: transparent;
}
.vw-pending-note {
  font-size: 11px;
  color: var(--sun-warn-text);
}
.vw-degrade,
.vw-error {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 4px;
}
.vw-degrade {
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
}
.vw-error {
  color: var(--sun-error-text);
  background: var(--sun-error-bg);
  border: 1px solid var(--sun-error-border);
}
.vw-add {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.vw-add-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
/* REQ-V-018 结构化构造器：三步引导（实体/维度/渠道/核查点）紧凑行 */
.vw-ctor {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card-hover);
}
.vw-ctor-row {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.vw-ctor-entity {
  flex: 1;
  min-width: 120px;
}
.vw-ctor-dim {
  width: 130px;
}
.vw-route-btn {
  font-size: 11px;
}
.vw-empty {
  margin: 4px 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.vw-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.vw-item {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 10px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card-hover);
}
/* 未采纳旁路态（建议/已忽略）虚线弱化 */
.vw-item--side {
  border-style: dashed;
  opacity: 0.85;
}
/* REQ-V-007 三栏联动定位高亮：青色描边 + 3 次脉冲（2.4s 与 FLASH_MS 对齐） */
.vw-item--flash {
  border-color: var(--sun-border-active);
  border-style: solid;
  opacity: 1;
  animation: vw-flash-pulse 0.8s ease-in-out 3;
}
@keyframes vw-flash-pulse {
  0%, 100% { box-shadow: 0 0 0 2px rgba(110, 222, 233, 0.35); }
  50% { box-shadow: 0 0 0 4px rgba(110, 222, 233, 0.65); }
}
.vw-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.vw-item-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.vw-status {
  display: inline-flex;
  padding: 1px 9px;
  border: 1px solid;
  border-radius: var(--sun-radius-badge, 12px);
  font-size: 11px;
  line-height: 16px;
  white-space: nowrap;
}
.vw-item--side .vw-status {
  border-style: dashed;
}
.vw-kind {
  font-size: 11px;
  line-height: 16px;
  padding: 1px 8px;
  border-radius: var(--sun-radius-badge, 12px);
  border: 1px solid var(--sun-border);
  color: var(--sun-text-secondary);
}
.vw-origin-manual {
  font-size: 10px;
  line-height: 14px;
  padding: 0 6px;
  border-radius: 8px;
  border: 1px solid var(--sun-info-border);
  color: var(--sun-info-text);
}
/* REQ-V-019 AI 草案成项徽标（origin=ai_draft，经人审批后才有） */
.vw-origin-ai {
  font-size: 10px;
  line-height: 14px;
  padding: 0 6px;
  border-radius: 8px;
  border: 1px solid var(--sun-border-active);
  color: var(--sun-border-active);
}
/* REQ-V-019 草案结果条（ok=绿 / degraded=黄 / blocked·失败=红） */
.vw-draft-note {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 4px;
}
.vw-draft-note--ok {
  color: var(--sun-ok-text);
  background: var(--sun-ok-bg);
  border: 1px solid var(--sun-ok-border);
}
.vw-draft-note--warn {
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
}
.vw-draft-note--error {
  color: var(--sun-error-text);
  background: var(--sun-error-bg);
  border: 1px solid var(--sun-error-border);
}
.vw-ai-draft {
  font-size: 12px;
}
.vw-route {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.vw-route--external {
  color: var(--sun-warn-text);
}
.vw-text {
  margin: 0;
  font-size: 13px;
  color: var(--sun-text-primary);
}
.vw-fals {
  margin: 0;
  font-size: 11px;
  color: var(--sun-warn-text);
}
.vw-conclusion {
  margin: 0;
  font-size: 12px;
  color: var(--sun-ok-text);
}
.vw-conclusion-tag {
  display: inline-block;
  margin-right: 6px;
  padding: 0 5px;
  font-size: 10px;
  border: 1px solid var(--sun-ok-border);
  border-radius: 8px;
}
/* REQ-V-017 复跑结果卡片（只读 Function 回填，AI 辅助推演·需人确认） */
.vw-replay {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 8px;
  border: 1px dashed var(--sun-border-active);
  border-radius: 6px;
  background: var(--sun-input-bg);
}
.vw-replay-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.vw-replay-tag {
  padding: 0 5px;
  font-size: 10px;
  line-height: 14px;
  border: 1px solid var(--sun-border-active);
  border-radius: 8px;
  color: var(--sun-border-active);
}
.vw-replay-badge {
  padding: 0 5px;
  font-size: 10px;
  line-height: 14px;
  border: 1px solid var(--sun-warn-border);
  border-radius: 8px;
  color: var(--sun-warn-text);
}
.vw-replay-summary {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-primary);
}
.vw-replay-toggle {
  align-self: flex-start;
  padding: 0;
  border: none;
  background: none;
  font-size: 11px;
  line-height: 16px;
  color: var(--sun-border-active);
  cursor: pointer;
}
.vw-replay-toggle:hover {
  text-decoration: underline;
}
.vw-replay-detail {
  margin: 2px 0 0;
  padding: 6px 8px;
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: var(--sun-bg-base);
  font-family: var(--sun-font-mono);
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--sun-text-primary);
}
/* REQ-V-011 已挂书证（核查项卡片行内） */
.vw-ev {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  margin-top: 3px;
}
.vw-ev-title {
  font-size: 10px;
  color: var(--sun-text-tertiary, var(--sun-border-active));
}
.vw-ev-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 0 4px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: var(--sun-input-bg);
  font-size: 11px;
}
.vw-ev-link {
  padding: 0;
  border: none;
  background: none;
  font-size: 11px;
  color: var(--sun-border-active);
  cursor: pointer;
}
.vw-ev-link:hover:not(:disabled) {
  text-decoration: underline;
}
.vw-ev-link:disabled {
  cursor: wait;
  opacity: 0.6;
}
.vw-ev-unlink {
  padding: 0;
  border: none;
  background: none;
  font-size: 10px;
  color: var(--sun-warn-text);
  cursor: pointer;
}
.vw-ev-unlink:hover:not(:disabled) {
  text-decoration: underline;
}
.vw-ev-unlink:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.vw-meta {
  margin: 0;
  font-size: 11px;
}
.vw-item-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-content: flex-start;
  justify-content: flex-end;
  max-width: 220px;
}
.vw-act {
  font-weight: 600;
}
.vw-act--success {
  color: var(--sun-ok-text);
}
.vw-act--danger {
  color: var(--sun-error-text);
}
.vw-confirm-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.vw-confirm-transition {
  margin: 0;
  font-size: 14px;
  color: var(--sun-text-secondary);
}
.vw-confirm-transition .target {
  color: var(--sun-border-active);
}
.vw-confirm-note {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--sun-text-secondary);
  background: var(--sun-input-bg);
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  padding: 8px 10px;
}
.vw-field-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.vw-confirm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
