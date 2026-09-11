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
import { NModal, NButton, NInput, NIcon } from 'naive-ui'
import { WarningOutline, RefreshOutline, AddOutline } from '@vicons/ionicons5'
import {
  allowedVerifyActions,
  isManualOrigin,
  isVerifySideStatus,
  verifyKindLabel,
  verifyProgressPercent,
  verifyStatusMeta,
  VERIFY_STATUS_ORDER,
  type VerifyAction,
  type VerifyItem,
  type VerifyItemsPage,
  type VerifyProgress,
} from '../../domain/verify'
import { degradeReason } from '../../domain/clue'
import { verifyApi } from '../../api/endpoints/verify'

const props = defineProps<{
  caseId: string
  clueId: string
  operator: string
  role: string
  degraded: boolean
  /** 父层写请求进行中（提交后入队回执前禁用所有写按钮） */
  submitting?: boolean
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
  /** REQ-V-007：清单加载/刷新后回传整页，父层构建「文本→状态」映射喂三栏联动 */
  loaded: [page: VerifyItemsPage]
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

    <!-- 人工添加 -->
    <div class="vw-add">
      <NInput
        v-model:value="newText"
        type="textarea"
        :rows="2"
        :disabled="degraded || submitting"
        :placeholder="degraded ? '系统降级运行中，写操作已临时禁用' : '添加人工核查项：把待核实的事实/假设拆成一条可裁决的动作'"
        @keydown.ctrl.enter="submitAdd"
      />
      <div class="vw-add-foot">
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
            <span v-if="it.channel === 'function' && it.ref_function" class="vw-route">
              可复跑 · <code class="mono">{{ it.ref_function }}</code>
            </span>
            <span v-else-if="it.channel === 'external'" class="vw-route vw-route--external">
              外部调取<template v-if="it.external?.target"> · {{ it.external.target }}</template>
            </span>
          </div>
          <p class="vw-text">{{ it.text }}</p>
          <p v-if="it.falsification" class="vw-fals">
            证伪条件：{{ it.falsification }}
          </p>
          <p v-if="it.conclusion" class="vw-conclusion">
            <span class="vw-conclusion-tag">结论</span>{{ it.conclusion }}
          </p>
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
        </div>
      </li>
    </ul>

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
  </div>
</template>

<style scoped>
.vw {
  display: flex;
  flex-direction: column;
  gap: 10px;
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
