<script setup lang="ts">
// RC-102/103/104 画布节点抽屉（纯展示 + 事件上抛，数据与请求都在父组件）。
//  - RC-102 规则节点：业务视图（rule_text/依据）↔ 审计视图（rule_id/function/
//    params/版本，只读 + 规则工坊外链）；业务视图按构造不出现任何技术标识；
//  - RC-103 四态机：展开/折叠按钮 + expanding/failed 反馈 + 各类降级文案；
//  - RC-104 数据行字段直接喂 TraceabilityPanel（MaskedField 唯一遮蔽口径）。
import { computed, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NDrawer,
  NDrawerContent,
  NIcon,
  NInput,
  NModal,
  NSpin,
  NTag,
} from 'naive-ui'
import { ChevronForwardOutline, GitNetworkOutline } from '@vicons/ionicons5'
import {
  isManualNode,
  isSuggestionNode,
  KIND_LABELS,
  type AdoptUiState,
  type CanvasEdge,
  type CanvasNode,
  type ExpandDirection,
  type ExpandNotice,
  type ExpandUiState,
  type FileDetailDto,
  type FunctionResultDetail,
  type NodeDetail,
  type RowDetailDto,
  type RuleAudit,
} from '../../domain/canvas'
import type { VerifySuggestion } from '../../domain/canvas-verify-suggest'
import { VERIFY_CHANNEL_LABELS } from '../../domain/canvas-verify-suggest'
import TraceabilityPanel from './TraceabilityPanel.vue'

const props = withDefaults(
  defineProps<{
    show: boolean
    node: CanvasNode | null
    uiState?: ExpandUiState
    detail?: NodeDetail | null
    notices?: ExpandNotice[]
    truncated?: boolean
    audit?: RuleAudit | null
    auditLoading?: boolean
    auditMissing?: boolean
    auditView?: 'business' | 'audit'
    /** 该节点是否已展开过（控制 折叠/展开 按钮互斥） */
    hasExpanded?: boolean
    /** M3：该节点关联的人工连线（系统边不在内） */
    manualEdges?: CanvasEdge[]
    /** 全部节点（人工连线对端标签解析/M4 查询源标签） */
    nodes?: CanvasNode[]
    busy?: boolean
    /** M4 RC-105：规则节点「生成手册核实建议」请求中 */
    suggesting?: boolean
    /** M4 RC-105：当前规则节点的建议生成结果态 */
    suggestionState?: 'idle' | 'empty' | 'added'
    /** M4 RC-105：当前 pb 建议节点采纳态 */
    adoptState?: AdoptUiState
    /** M4 RC-105：当前假设节点「转待核实」任务进行中 */
    toVerifying?: boolean
    /** M4 RC-105：转待核实失败信息（确认弹窗内展示） */
    toVerifyError?: string | null
    /** M4 P3：「下一步可核查」建议列表（仅 fact/object/source_row 有值） */
    verifySuggestions?: VerifySuggestion[]
  }>(),
  {
    uiState: 'collapsed',
    detail: null,
    notices: () => [],
    truncated: false,
    audit: null,
    auditLoading: false,
    auditMissing: false,
    auditView: 'business',
    hasExpanded: false,
    manualEdges: () => [],
    nodes: () => [],
    busy: false,
    suggesting: false,
    suggestionState: 'idle',
    adoptState: () => ({ status: 'idle' }),
    toVerifying: false,
    toVerifyError: null,
    verifySuggestions: () => [],
  },
)

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'expand', direction: ExpandDirection): void
  (e: 'retry'): void
  (e: 'collapse'): void
  (e: 'update:auditView', v: 'business' | 'audit'): void
  (e: 'edit-node'): void
  (e: 'delete-node'): void
  (e: 'unpin-node', node: CanvasNode): void
  (e: 'delete-edge', edge: CanvasEdge): void
  /** M4 RC-105：规则节点生成手册核实建议 */
  (e: 'gen-suggestions'): void
  /** M4 RC-105：采纳 pb 建议节点（text 仅在人工改写时携带=新幂等任务） */
  (
    e: 'adopt-suggestion',
    payload: { node: CanvasNode; text?: string },
  ): void
  /** M4 RC-105：失败任务重试（改写文本→新 idem 新任务） */
  (
    e: 'retry-adopt',
    payload: { node: CanvasNode; text?: string },
  ): void
  /** M4 RC-105：假设转待核实确认文本 */
  (e: 'confirm-to-verify', text: string): void
  /** M4 RC-204：定位到「查询自」源节点（继续 RC-103 溯源） */
  (e: 'focus-node', nodeId: string): void
}>()

const STAGE_LABELS: Record<string, string> = {
  xu_shi: '虚始',
  qi_zheng: '期正',
  yong_jian: '用间',
}

const title = computed(() =>
  props.node ? `${KIND_LABELS[props.node.kind]}节点` : '节点',
)

const isRule = computed(() => props.node?.kind === 'rule')
const isFact = computed(() => props.node?.kind === 'fact')
const isObject = computed(() => props.node?.kind === 'object')
const isRow = computed(() => props.node?.kind === 'source_row')
const isFile = computed(() => props.node?.kind === 'source_file')
const isManual = computed(() => (props.node ? isManualNode(props.node) : false))
const manualTitle = computed(() =>
  String((props.node?.props as Record<string, unknown> | undefined)?.title ?? ''))
const manualContent = computed(() =>
  String((props.node?.props as Record<string, unknown> | undefined)?.content ?? ''))

/** 人工连线对端标签（节点表缺失时退回 id） */
function edgeOtherLabel(edge: CanvasEdge): string {
  const otherId = edge.source === props.node?.id ? edge.target : edge.source
  return props.nodes.find((n) => n.id === otherId)?.label ?? otherId
}

function edgeDirection(edge: CanvasEdge): '出' | '入' {
  return edge.source === props.node?.id ? '出' : '入'
}
const expanding = computed(() => props.uiState === 'expanding')
const failed = computed(() => props.uiState === 'expand_failed')

const businessRuleText = computed(
  () => String((props.node?.props as Record<string, unknown> | undefined)
    ?.rule_text ?? ''),
)
const businessBasis = computed(
  () => String((props.node?.props as Record<string, unknown> | undefined)
    ?.basis ?? ''),
)

const rowDetail = computed<RowDetailDto | null>(() => {
  const d = props.detail
  if (!d || !('fields' in d)) return null
  return d as RowDetailDto
})
const fileDetail = computed<FileDetailDto | null>(() => {
  const d = props.detail
  if (!d || !('registered' in d)) return null
  return d as FileDetailDto
})

// ======================================================================
// M4 RC-105：核查项节点（pb 建议虚节点 / 已采纳核查项）
// ======================================================================
const isVerifyItem = computed(() => props.node?.kind === 'verify_item')
const isSuggestion = computed(
  () => (props.node ? isSuggestionNode(props.node) : false),
)

function nodeProps(): Record<string, unknown> {
  return (props.node?.props as Record<string, unknown> | undefined) ?? {}
}
const suggestionText = computed(() => String(nodeProps().text ?? ''))
const suggestionChannel = computed(() => String(nodeProps().channel ?? ''))
const suggestionRefFn = computed(() => String(nodeProps().ref_function ?? ''))
const suggestionFalsification = computed(
  () => String(nodeProps().falsification ?? ''),
)
const suggestionExternal = computed<Record<string, unknown> | null>(() => {
  const ext = nodeProps().external
  return ext && typeof ext === 'object'
    ? (ext as Record<string, unknown>)
    : null
})
const adoptRunning = computed(() => props.adoptState?.status === 'running')
const adoptFailed = computed(() => props.adoptState?.status === 'failed')

/** 采纳前可改写核查项文本（改写=新 idem 新任务，失败重试的唯一途径） */
const adoptText = ref('')
watch(
  () => [props.node?.id, isSuggestion.value, suggestionText.value] as const,
  () => {
    adoptText.value = suggestionText.value
  },
  { immediate: true },
)
/** 文本未改写则不发送（与后端 transition idem 空覆盖口径一致） */
function adoptPayload(node: CanvasNode): { node: CanvasNode; text?: string } {
  const t = adoptText.value.trim()
  return t && t !== suggestionText.value ? { node, text: t } : { node }
}

// ======================================================================
// M4 RC-105：人工假设 → 转待核实确认弹窗（文本默认带入假设标题）
// ======================================================================
const tvShow = ref(false)
const tvText = ref('')

function openToVerify(): void {
  tvText.value = String(nodeProps().title ?? '')
  tvShow.value = true
}

/** M4 P3：采纳 fact/object/source_row 上的建议 → 打开同一确认弹窗，预填建议文本 */
function openAddVerify(suggestedText: string): void {
  tvText.value = suggestedText
  tvShow.value = true
}

function submitToVerify(): void {
  const text = tvText.value.trim()
  if (!text) return
  emit('confirm-to-verify', text)
}

// 任务结束（busy 由 true→false）：无错误则关闭弹窗，有错误保留供重试
watch(
  () => props.toVerifying,
  (busy, prev) => {
    if (prev && !busy && !props.toVerifyError) tvShow.value = false
  },
)
// 切换节点时复位弹窗
watch(
  () => props.node?.id,
  () => {
    tvShow.value = false
  },
)

// ======================================================================
// M4 RC-204：function_result 结果节点
// ======================================================================
const isFunctionResult = computed(
  () => props.node?.kind === 'function_result',
)
const frDetail = computed<FunctionResultDetail | null>(() => {
  const d = props.detail
  if (!d || !('kind' in d) || d.kind !== 'function_result') return null
  return d as FunctionResultDetail
})
/** 抽屉内优先用 expand 负载（含查询源/输入表），字段回退节点 props 快照 */
const frSummary = computed(() => {
  if (frDetail.value?.summary) return frDetail.value.summary
  const p = nodeProps()
  return {
    kind: String(p.kind ?? ''),
    row_count: typeof p.row_count === 'number' ? p.row_count : undefined,
    columns: Array.isArray(p.columns) ? (p.columns as string[]) : undefined,
    preview_rows: Array.isArray(p.preview_rows)
      ? (p.preview_rows as Array<Record<string, unknown>>)
      : undefined,
    meta: p.meta as Record<string, unknown> | undefined,
    report: p.report,
  }
})
const frParams = computed<Record<string, unknown>>(() => {
  const raw = frDetail.value?.params ?? nodeProps().params
  return raw && typeof raw === 'object'
    ? (raw as Record<string, unknown>)
    : {}
})
const frParamRows = computed(() =>
  Object.entries(frParams.value).map(([k, v]) => ({
    key: k,
    value: typeof v === 'object' ? JSON.stringify(v) : String(v),
  })),
)
const frColumns = computed<string[]>(() => frSummary.value.columns ?? [])
const frPreviewRows = computed<Array<Record<string, unknown>>>(
  () => frSummary.value.preview_rows ?? [],
)
const frSourceNodes = computed(() =>
  (frDetail.value?.source_node_ids ?? [])
    .map((id) => props.nodes.find((n) => n.id === id))
    .filter((n): n is CanvasNode => !!n),
)
function focusSource(nodeId: string): void {
  emit('focus-node', nodeId)
}
function formatCell(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
function formatReport(report: unknown): string {
  if (typeof report === 'string') return report
  return JSON.stringify(report, null, 2)
}

const hasSemanticNotice = computed(() =>
  props.notices.includes('semantic_unavailable'))
const isLeaf = computed(() => props.notices.includes('leaf'))

const auditParamRows = computed(() =>
  Object.entries(props.audit?.params ?? {}).map(([k, v]) => ({
    key: k,
    value: typeof v === 'object' ? JSON.stringify(v) : String(v),
  })),
)

function close(): void {
  emit('update:show', false)
}
function doExpand(d: ExpandDirection): void {
  emit('expand', d)
}
function switchAudit(v: 'business' | 'audit'): void {
  emit('update:auditView', v)
}
</script>

<template>
  <NDrawer
    :show="show"
    :width="560"
    :z-index="900"
    @update:show="emit('update:show', $event)"
  >
    <NDrawerContent :title="title" closable @close="close">
      <div v-if="!node" class="dim">未选择节点</div>

      <div v-else class="drawer-body" data-testid="canvas-node-drawer">
        <!-- 通用：节点标题行 -->
        <div class="node-head">
          <NTag size="small" :bordered="false" class="kind-tag">
            {{ KIND_LABELS[node.kind] }}
          </NTag>
          <span class="node-label" data-testid="drawer-node-label">{{ node.label }}</span>
          <NTag v-if="node.pinned" size="tiny" type="warning" :bordered="false">
            已钉住
          </NTag>
          <span v-if="node.stale" class="stale-tag" data-testid="drawer-stale">
            引用已失效
          </span>
        </div>

        <!-- ============ M3 RC-201：钉住管理（人工/系统节点通用） ============ -->
        <div v-if="node.pinned" class="m3-actions">
          <NButton
            size="tiny"
            data-testid="drawer-unpin-node"
            @click="emit('unpin-node', node)"
          >
            取消钉住
          </NButton>
        </div>

        <!-- ============ M3 RC-202：人工节点内容与编辑/删除 ============ -->
        <section v-if="isManual" class="manual-block" data-testid="manual-node-section">
          <div v-if="manualTitle" class="block">
            <h4>假设标题</h4>
            <p class="manual-text">{{ manualTitle }}</p>
          </div>
          <div class="block">
            <h4>{{ node.kind === 'hypothesis' ? '假设内容' : '备注内容' }}</h4>
            <p class="manual-text">{{ manualContent }}</p>
          </div>
          <div class="m3-actions">
            <NButton
              size="small"
              type="primary"
              :loading="busy"
              data-testid="drawer-edit-node"
              @click="emit('edit-node')"
            >
              编辑
            </NButton>
            <!-- M4 RC-105：假设转待核实（确认后入队，202 非终态） -->
            <NButton
              v-if="node.kind === 'hypothesis'"
              size="small"
              :loading="toVerifying"
              data-testid="drawer-to-verify"
              @click="openToVerify"
            >
              转为待核实
            </NButton>
            <NButton
              size="small"
              type="error"
              ghost
              :loading="busy"
              data-testid="drawer-delete-node"
              @click="emit('delete-node')"
            >
              删除
            </NButton>
          </div>
        </section>

        <!-- ============ M3 RC-203：关联人工连线管理 ============ -->
        <section
          v-if="manualEdges.length"
          class="edge-block"
          data-testid="manual-edge-section"
        >
          <h4>人工连线（{{ manualEdges.length }}）</h4>
          <ul class="edge-list">
            <li
              v-for="edge in manualEdges"
              :key="edge.id"
              class="edge-item"
              :data-testid="`drawer-edge-${edge.id}`"
            >
              <div class="edge-line">
                <NTag size="tiny" type="warning" :bordered="false">
                  {{ edgeDirection(edge) === '出' ? '→' : '←' }} {{ edge.rel }}
                </NTag>
                <span class="edge-other">{{ edgeOtherLabel(edge) }}</span>
              </div>
              <p v-if="edge.note" class="edge-note dim">{{ edge.note }}</p>
              <NButton
                size="tiny"
                quaternary
                type="error"
                :data-testid="`drawer-delete-edge-${edge.id}`"
                @click="emit('delete-edge', edge)"
              >
                删除连线
              </NButton>
            </li>
          </ul>
        </section>

        <!-- ============ RC-102 规则节点：业务/审计双视图 ============ -->
        <template v-if="isRule">
          <div class="view-switch" role="tablist" aria-label="规则视图切换">
            <button
              type="button"
              class="seg"
              :class="{ 'seg--on': auditView === 'business' }"
              data-testid="rule-view-business"
              @click="switchAudit('business')"
            >
              业务视图
            </button>
            <button
              type="button"
              class="seg"
              :class="{ 'seg--on': auditView === 'audit' }"
              data-testid="rule-view-audit"
              @click="switchAudit('audit')"
            >
              审计视图
            </button>
          </div>

          <!-- 业务视图：只渲染自然语言判据/依据，按构造不含技术标识 -->
          <div v-if="auditView === 'business'" data-testid="rule-business" class="rule-business">
            <section v-if="businessRuleText" class="block">
              <h4>规则判据</h4>
              <p class="rule-text">{{ businessRuleText }}</p>
            </section>
            <section v-if="businessBasis" class="block">
              <h4>命中依据</h4>
              <p class="rule-text">{{ businessBasis }}</p>
            </section>
            <p v-if="!businessRuleText && !businessBasis" class="dim">
              该规则暂无自然语言说明
            </p>
            <p class="dim note-line">
              技术挂钩与阈值参数见「审计视图」（只读）；如需调整规则请前往规则工坊。
            </p>

            <!-- M4 RC-105：生成手册核实建议（虚节点，不经审批不进工作台） -->
            <div class="m4-block" data-testid="rule-suggest-actions">
              <NButton
                size="small"
                type="primary"
                :loading="suggesting"
                data-testid="drawer-gen-suggestions"
                @click="emit('gen-suggestions')"
              >
                {{ suggesting ? '正在匹配手册…' : '生成手册核实建议' }}
              </NButton>
              <p
                v-if="suggestionState === 'empty'"
                class="dim"
                data-testid="suggestion-empty"
              >
                手册暂无该规则的核实建议，可手工添加假设
              </p>
              <p
                v-else-if="suggestionState === 'added'"
                class="dim ok-note"
                data-testid="suggestion-added"
              >
                已在画布生成手册建议节点（虚线样式），采纳后才会进入核查工作台
              </p>
            </div>
          </div>

          <!-- 审计视图：只读技术字段 -->
          <div v-else data-testid="rule-audit" class="rule-audit">
            <div v-if="auditLoading" class="state-line" data-testid="audit-loading">
              <NSpin size="small" />
              <span>正在装载规则声明…</span>
            </div>
            <NAlert
              v-else-if="auditMissing"
              type="warning"
              :show-icon="false"
              :bordered="false"
              data-testid="audit-missing"
            >
              规则声明缺失或已被移除（历史占位规则），审计字段不可用。
            </NAlert>
            <template v-else-if="audit">
              <table class="kv">
                <tbody>
                  <tr><th>规则 ID</th><td class="mono">{{ audit.rule_id }}</td></tr>
                  <tr><th>规则名称</th><td>{{ audit.title }}</td></tr>
                  <tr>
                    <th>阶段</th>
                    <td>{{ STAGE_LABELS[audit.stage] ?? audit.stage }}</td>
                  </tr>
                  <tr><th>维度</th><td>{{ audit.dimension || '—' }}</td></tr>
                  <tr><th>只读 Function</th><td class="mono">{{ audit.function }}</td></tr>
                  <tr>
                    <th>命中条件</th>
                    <td class="mono">{{ audit.hit_when }}</td>
                  </tr>
                  <tr v-if="audit.jian_types.length">
                    <th>间类</th>
                    <td>{{ audit.jian_types.join('、') }}</td>
                  </tr>
                  <tr v-if="audit.assumption">
                    <th>庙算假设</th>
                    <td class="mono">{{ audit.assumption }}</td>
                  </tr>
                  <tr>
                    <th>规则版本</th>
                    <td class="mono">{{ audit.ontology_version || '未记录' }}</td>
                  </tr>
                  <tr><th>案件包</th><td class="mono">{{ audit.pack_id }}</td></tr>
                </tbody>
              </table>

              <h4 class="params-title">阈值参数（params）</h4>
              <table v-if="auditParamRows.length" class="kv params">
                <tbody>
                  <tr v-for="r in auditParamRows" :key="r.key">
                    <th class="mono">{{ r.key }}</th>
                    <td class="mono">{{ r.value }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="dim">无参数</p>

              <h4>规则判据原文</h4>
              <p class="rule-text">{{ audit.rule_text }}</p>

              <!-- RC-102-4：规则工坊外链（画布内无编辑控件） -->
              <a
                :href="audit.rule_workshop_href"
                class="workshop-link"
                data-testid="rule-workshop-link"
              >
                在规则工坊中打开
                <NIcon :component="ChevronForwardOutline" />
              </a>
              <p class="dim note-line">审计视图只读，规则调整请在规则工坊完成。</p>
            </template>
          </div>
        </template>

        <!-- ============ M4 RC-105：核查项节点（pb 建议虚节点 / 已采纳） ============ -->
        <template v-else-if="isVerifyItem">
          <!-- 未采纳手册建议 -->
          <section
            v-if="isSuggestion"
            class="suggest-block"
            data-testid="suggestion-block"
          >
            <NTag size="small" type="warning" :bordered="false">
              手册建议·未采纳
            </NTag>
            <div class="block">
              <h4>建议核查内容</h4>
              <p class="manual-text">{{ suggestionText }}</p>
            </div>
            <div class="block">
              <h4>采纳文本（可改写）</h4>
              <NInput
                v-model:value="adoptText"
                type="textarea"
                :rows="3"
                :disabled="adoptRunning"
                placeholder="核查项文本"
                data-testid="adopt-text-input"
              />
              <p class="dim note-line">
                直接采纳使用手册原文；改写文本会生成新的办理任务（失败重试时可修正后重提）。
              </p>
            </div>
            <table class="kv">
              <tbody>
                <tr>
                  <th>核实渠道</th>
                  <td>
                    {{ suggestionChannel === 'external'
                      ? '外部调取'
                      : suggestionChannel === 'function'
                        ? '只读查询'
                        : (suggestionChannel || '—') }}
                  </td>
                </tr>
                <tr v-if="suggestionRefFn">
                  <th>挂钩查询</th>
                  <td class="mono">{{ suggestionRefFn }}</td>
                </tr>
                <tr v-if="suggestionExternal?.target">
                  <th>调取对象</th>
                  <td>{{ suggestionExternal.target }}</td>
                </tr>
                <tr v-if="suggestionFalsification">
                  <th>证伪口径</th>
                  <td>{{ suggestionFalsification }}</td>
                </tr>
              </tbody>
            </table>

            <NAlert
              v-if="adoptFailed"
              type="error"
              :show-icon="false"
              :bordered="false"
              data-testid="adopt-failed"
            >
              <div>
                核查项生成失败<span v-if="adoptState?.message">：{{ adoptState.message }}</span>
                ；入队失败不会产生已采纳假象，可重试。
              </div>
              <NButton
                size="small"
                type="primary"
                class="retry"
                :loading="adoptRunning"
                data-testid="drawer-retry-adopt"
                @click="node && emit('retry-adopt', adoptPayload(node))"
              >
                用当前文本重试
              </NButton>
            </NAlert>

            <div class="m3-actions">
              <NButton
                size="small"
                type="primary"
                :loading="adoptRunning"
                :disabled="!adoptText.trim()"
                data-testid="drawer-adopt-suggestion"
                @click="node && emit('adopt-suggestion', adoptPayload(node))"
              >
                {{ adoptRunning ? '正在生成核查项…' : '采纳为核查项' }}
              </NButton>
            </div>
            <p class="dim note-line">
              采纳后进入核查工作台；未采纳的建议仅存在于本画布，工作台不可见。
            </p>
          </section>

          <!-- 已采纳核查项（只读状态） -->
          <section v-else data-testid="verify-item-block">
            <NTag size="small" type="success" :bordered="false">已采纳核查项</NTag>
            <div class="block">
              <h4>核查内容</h4>
              <p class="manual-text">{{ suggestionText || node.label }}</p>
            </div>
            <table class="kv">
              <tbody>
                <tr v-if="String(nodeProps().status ?? '')">
                  <th>当前状态</th>
                  <td>{{ nodeProps().status }}</td>
                </tr>
                <tr v-if="suggestionChannel">
                  <th>核实渠道</th>
                  <td>
                    {{ suggestionChannel === 'external'
                      ? '外部调取'
                      : suggestionChannel === 'function'
                        ? '只读查询'
                        : suggestionChannel }}
                  </td>
                </tr>
                <tr v-if="suggestionRefFn">
                  <th>挂钩查询</th>
                  <td class="mono">{{ suggestionRefFn }}</td>
                </tr>
              </tbody>
            </table>
            <p class="dim note-line">核查办理请在「核查工作台」进行。</p>
          </section>
        </template>

        <!-- ============ 事实节点 ============ -->
        <template v-else-if="isFact">
          <p class="dim">沿事实反向追溯其涉及的语义实体。</p>
          <div class="actions">
            <NButton
              size="small"
              type="primary"
              :loading="expanding"
              :disabled="hasExpanded"
              data-testid="expand-fact"
              @click="doExpand('source')"
            >
              <template v-if="!expanding">
                <NIcon :component="GitNetworkOutline" />
                追溯涉及实体
              </template>
              <template v-else>正在追溯来源…</template>
            </NButton>
            <NButton
              v-if="hasExpanded"
              size="small"
              data-testid="collapse-fact"
              @click="emit('collapse')"
            >
              折叠本层
            </NButton>
          </div>
        </template>

        <!-- ============ 对象节点：邻居 + 来源行 ============ -->
        <template v-else-if="isObject">
          <p class="dim">
            类型：{{ String(node.props?.type_title ?? node.props?.type ?? '') }}；
            可展开关联对象（语义链接一跳）或回溯对象来源行。
          </p>
          <div class="actions">
            <NButton
              size="small"
              :loading="expanding"
              data-testid="expand-object-all"
              @click="doExpand('all')"
            >
              {{ expanding ? '正在追溯来源…' : '全部展开' }}
            </NButton>
            <NButton
              size="small"
              :loading="expanding"
              data-testid="expand-object-neighbors"
              @click="doExpand('neighbors')"
            >
              展开关联对象
            </NButton>
            <NButton
              size="small"
              :loading="expanding"
              data-testid="expand-object-source"
              @click="doExpand('source')"
            >
              查看来源行
            </NButton>
            <NButton
              v-if="hasExpanded"
              size="small"
              data-testid="collapse-object"
              @click="emit('collapse')"
            >
              折叠本层
            </NButton>
          </div>
        </template>

        <!-- ============ 数据行节点：字段表（RC-104 遮蔽）+ 所属文件 ============ -->
        <template v-else-if="isRow">
          <NAlert
            v-if="rowDetail?.missing"
            type="error"
            :show-icon="false"
            :bordered="false"
            class="block"
            data-testid="row-missing-alert"
          >
            归档行缺失（URI：{{ rowDetail.row_uri }}），可能数据版本已更新；
            下方仅展示语义层记录的定位字段。
          </NAlert>
          <NTag v-if="rowDetail?.granularity === '表级汇总'" size="small" type="info">
            表级汇总行
          </NTag>

          <div v-if="expanding" class="state-line" data-testid="row-expanding">
            <NSpin size="small" />
            <span>正在追溯来源…</span>
          </div>

          <TraceabilityPanel
            v-else-if="rowDetail && rowDetail.fields.length"
            :rows="[rowDetail]"
            data-testid="row-trace"
          />
          <p v-else-if="!failed" class="dim">
            <NButton
              size="small"
              type="primary"
              :loading="expanding"
              data-testid="expand-row"
              @click="doExpand('source')"
            >
              取回字段并查看所属文件
            </NButton>
          </p>

          <div class="actions">
            <NButton
              v-if="rowDetail"
              size="small"
              :loading="expanding"
              data-testid="expand-row-file"
              @click="doExpand('source')"
            >
              查看所属文件
            </NButton>
          </div>
        </template>

        <!-- ============ 数据源文件节点 ============ -->
        <template v-else-if="isFile">
          <div v-if="fileDetail?.registered && fileDetail.file" class="file-card" data-testid="file-card">
            <h4>{{ fileDetail.file.filename }}</h4>
            <table class="kv">
              <tbody>
                <tr><th>格式</th><td>{{ fileDetail.file.format || '—' }}</td></tr>
                <tr><th>行数</th><td>{{ fileDetail.file.rows ?? '—' }}</td></tr>
                <tr><th>数据表</th><td class="mono">{{ fileDetail.file.dataset }}</td></tr>
                <tr><th>上传人</th><td>{{ fileDetail.file.uploaded_by || '—' }}</td></tr>
                <tr><th>上传时间</th><td>{{ fileDetail.file.uploaded_at || '—' }}</td></tr>
                <tr><th>登记号</th><td class="mono">{{ fileDetail.file.upload_id }}</td></tr>
              </tbody>
            </table>
          </div>
          <NAlert
            v-else-if="fileDetail && !fileDetail.registered"
            type="warning"
            :show-icon="false"
            :bordered="false"
            data-testid="file-unregistered"
          >
            未登记数据源：{{ fileDetail.dataset }}（语义层引用的数据表未在 ingest
            登记，无法展示文件元数据）
          </NAlert>
          <p v-else class="dim">
            <NButton size="small" :loading="expanding" @click="doExpand('source')">
              查看文件信息
            </NButton>
          </p>
          <p class="dim leaf-line">已到达数据源文件，溯源结束。</p>
        </template>

        <!-- ============ M4 RC-204：扩展查询结果节点 ============ -->
        <template v-else-if="isFunctionResult">
          <section class="fr-block" data-testid="function-result-block">
            <table class="kv">
              <tbody>
                <tr>
                  <th>执行时间</th>
                  <td>{{ frDetail?.executed_at || String(nodeProps().executed_at ?? '') || '—' }}</td>
                </tr>
                <tr>
                  <th>操作人</th>
                  <td>{{ frDetail?.executed_by || String(nodeProps().executed_by ?? '') || '—' }}</td>
                </tr>
              </tbody>
            </table>

            <div class="block">
              <h4>入参快照</h4>
              <table v-if="frParamRows.length" class="kv params">
                <tbody>
                  <tr v-for="row in frParamRows" :key="row.key">
                    <th class="mono">{{ row.key }}</th>
                    <td class="mono">{{ row.value }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="dim">无参数</p>
            </div>

            <div class="block">
              <h4>结果预览</h4>
              <template v-if="frSummary.kind === 'rows'">
                <p class="dim">
                  共 {{ frSummary.row_count ?? frPreviewRows.length }} 行<span
                    v-if="frPreviewRows.length && frSummary.row_count && frSummary.row_count > frPreviewRows.length"
                  >，仅展示前 {{ frPreviewRows.length }} 行</span>
                </p>
                <div v-if="frPreviewRows.length" class="fr-table-wrap">
                  <table class="fr-table">
                    <thead>
                      <tr><th v-for="c in frColumns" :key="c" class="mono">{{ c }}</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="(r, i) in frPreviewRows" :key="i">
                        <td v-for="c in frColumns" :key="c">{{ formatCell(r[c]) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p v-else class="dim" data-testid="function-result-empty">查询无命中记录</p>
              </template>
              <template v-else-if="frSummary.kind === 'report'">
                <pre v-if="frSummary.report" class="fr-report" data-testid="function-result-report">{{ formatReport(frSummary.report) }}</pre>
                <p v-else class="dim">查询无命中记录</p>
              </template>
              <p v-else class="dim">查询无命中记录</p>
            </div>

            <div class="block">
              <h4>查询自（继续溯源）</h4>
              <div v-if="frSourceNodes.length" class="fr-sources">
                <NButton
                  v-for="s in frSourceNodes"
                  :key="s.id"
                  size="tiny"
                  class="fr-source"
                  data-testid="fr-source-btn"
                  @click="focusSource(s.id)"
                >
                  {{ KIND_LABELS[s.kind] }}：{{ s.label }}
                </NButton>
              </div>
              <p v-else-if="frDetail" class="dim">该查询结果未挂接来源节点</p>
              <div v-else class="state-line">
                <NSpin size="small" />
                <span>正在装载溯源来源…</span>
              </div>
            </div>

            <p v-if="frDetail?.input_tables?.length" class="dim">
              输入语义表：{{ frDetail.input_tables.join('、') }}
            </p>
          </section>
        </template>

        <!-- ============ M4 P3：下一步可核查建议（仅 fact/object/source_row） ============ -->
        <section
          v-if="(isFact || isObject || isRow) && verifySuggestions.length"
          class="next-verify-block"
          data-testid="next-verify-section"
        >
          <h4>下一步可核查</h4>
          <ul class="next-verify-list">
            <li
              v-for="s in verifySuggestions"
              :key="s.id"
              class="next-verify-item"
              :data-testid="`next-verify-${s.id}`"
            >
              <div class="nv-head">
                <NTag size="tiny" :bordered="false" class="nv-channel">
                  {{ VERIFY_CHANNEL_LABELS[s.channel] }}
                </NTag>
                <span class="nv-reason">{{ s.reason }}</span>
              </div>
              <p class="nv-text">{{ s.text }}</p>
              <p v-if="s.falsification" class="nv-falsification dim">
                证伪口径：{{ s.falsification }}
              </p>
              <NButton
                size="tiny"
                type="primary"
                :loading="toVerifying"
                :disabled="toVerifying"
                :data-testid="`next-verify-adopt-${s.id}`"
                @click="openAddVerify(s.text)"
              >
                {{ toVerifying ? '正在入队…' : '采纳为核查项' }}
              </NButton>
            </li>
          </ul>
          <p class="dim note-line">
            采纳后入队生成「待核实」节点，可在核查工作台继续办理。
          </p>
          <NAlert
            v-if="toVerifyError"
            type="error"
            :show-icon="false"
            :bordered="false"
            data-testid="next-verify-error"
          >
            核查项生成失败：{{ toVerifyError }}；可修改文本后重试。
          </NAlert>
        </section>

        <!-- ============ 通用通知/失败态 ============ -->
        <NAlert
          v-if="hasSemanticNotice"
          type="warning"
          :show-icon="false"
          :bordered="false"
          class="block"
          data-testid="expand-semantic-notice"
        >
          实体关联不可用，请先在数据治理中构建语义层；数据行与文件溯源仍可使用。
        </NAlert>
        <NAlert
          v-if="truncated"
          type="info"
          :show-icon="false"
          :bordered="false"
          class="block"
        >
          关联结果过多，已按上限截断；可在数据治理视图查看完整关系。
        </NAlert>
        <p v-if="isLeaf && !isFile" class="dim">已到达溯源终点。</p>

        <NAlert
          v-if="failed"
          type="error"
          :show-icon="false"
          :bordered="false"
          class="block"
          data-testid="expand-failed"
        >
          <div>溯源展开失败，请重试；已展开图层不受影响。</div>
          <NButton size="small" type="primary" class="retry" @click="emit('retry')">
            重试
          </NButton>
        </NAlert>
      </div>

      <!-- M4 RC-105：假设转待核实确认弹窗（202 入队，终态失败保留可重试） -->
      <NModal
        :show="tvShow"
        display-directive="if"
        preset="card"
        title="转为待核实"
        class="tv-modal"
        :style="{ width: '480px' }"
        :mask-closable="false"
        data-testid="to-verify-modal"
        @update:show="(v: boolean) => { if (!v && !toVerifying) tvShow = v }"
      >
        <div class="tv-form">
          <label class="tv-label">
            核查项文本 <span class="req">*</span>
          </label>
          <NInput
            v-model:value="tvText"
            type="textarea"
            :rows="3"
            :disabled="toVerifying"
            data-testid="to-verify-text"
          />
          <NAlert
            v-if="toVerifyError"
            type="error"
            :show-icon="false"
            :bordered="false"
            data-testid="to-verify-error"
          >
            核查项生成失败：{{ toVerifyError }}；可修改文本后重试。
          </NAlert>
          <p class="dim">
            确认后入队生成核查项（HTTP 202，不代表完成）；成功后画布新增「待核实」节点，
            并可在核查工作台继续办理。
          </p>
        </div>
        <template #footer>
          <NButton size="small" :disabled="toVerifying" @click="tvShow = false">
            取消
          </NButton>
          <NButton
            size="small"
            type="primary"
            :loading="toVerifying"
            :disabled="!tvText.trim()"
            data-testid="to-verify-submit"
            @click="submitToVerify"
          >
            确认转为待核实
          </NButton>
        </template>
      </NModal>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.drawer-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.node-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.kind-tag {
  flex: none;
}
.node-label {
  font-size: 15px;
  font-weight: 600;
  color: var(--sun-text-primary);
  word-break: break-all;
}
.stale-tag {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  padding: 0 6px;
}
.m3-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.manual-block {
  border-left: 3px solid #ff9d4d;
  padding-left: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.manual-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--sun-text-primary);
  white-space: pre-wrap;
  word-break: break-all;
}
.edge-block h4 {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}

/* M4 P3：下一步可核查 */
.next-verify-block {
  border-left: 3px solid #6e95b8;
  padding-left: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.next-verify-block h4 {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.next-verify-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.next-verify-item {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: rgba(110, 149, 184, 0.04);
}
.nv-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.nv-channel {
  flex: none;
}
.nv-reason {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.nv-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--sun-text-primary);
  white-space: pre-wrap;
  word-break: break-all;
}
.nv-falsification {
  font-size: 12px;
  line-height: 1.5;
}
.edge-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.edge-item {
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.edge-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.edge-other {
  color: var(--sun-text-primary);
  word-break: break-all;
}
.edge-note {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
.view-switch {
  display: inline-flex;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
  width: fit-content;
}
.seg {
  border: none;
  background: transparent;
  color: var(--sun-text-secondary);
  font-size: 12px;
  padding: 5px 14px;
  cursor: pointer;
}
.seg--on {
  background: var(--sun-info-bg);
  color: var(--sun-info-text);
  font-weight: 600;
}
.block {
  margin: 0;
}
.block h4 {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.rule-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--sun-text-primary);
  white-space: pre-wrap;
}
.note-line {
  margin: 4px 0 0;
}
.kv {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.kv th {
  text-align: left;
  width: 110px;
  color: var(--sun-text-tertiary);
  font-weight: 400;
  padding: 4px 8px 4px 0;
  vertical-align: top;
}
.kv td {
  padding: 4px 0;
  word-break: break-all;
}
.mono {
  font-family: var(--sun-font-mono);
}
.params-title {
  margin: 12px 0 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.params th {
  width: 160px;
}
.workshop-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--sun-info-text);
  text-decoration: none;
  border: 1px solid var(--sun-info-border);
  border-radius: 4px;
  padding: 4px 10px;
  margin-top: 8px;
}
.workshop-link:hover {
  text-decoration: underline;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.state-line {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--sun-text-secondary);
  font-size: 12px;
  padding: 8px 0;
}
.dim {
  color: var(--sun-text-tertiary);
  font-size: 12px;
}
.file-card h4 {
  margin: 0 0 8px;
  font-size: 14px;
}
.leaf-line {
  margin-top: 4px;
}
.retry {
  margin-top: 8px;
}

/* ==================== M4 RC-105/RC-204 ==================== */
.m4-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--sun-border);
}
.ok-note {
  color: var(--sun-success-text, #18a058);
}
.suggest-block,
.fr-block {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.suggest-block .kv,
.fr-block .kv {
  margin-top: 2px;
}

/* 转待核实弹窗 */
.tv-modal :deep(.n-card) {
  width: 480px;
}
.tv-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tv-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.req {
  color: var(--sun-danger, #d03050);
}

/* Function 结果预览表 */
.fr-table-wrap {
  overflow-x: auto;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
}
.fr-table {
  border-collapse: collapse;
  font-size: 12px;
  width: 100%;
}
.fr-table th,
.fr-table td {
  border-bottom: 1px solid var(--sun-border);
  padding: 5px 8px;
  text-align: left;
  white-space: nowrap;
}
.fr-table thead th {
  background: var(--sun-fill-light, rgba(0, 0, 0, 0.03));
  color: var(--sun-text-secondary);
  font-weight: 600;
}
.fr-report {
  margin: 0;
  padding: 8px;
  background: var(--sun-fill-light, rgba(0, 0, 0, 0.03));
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: var(--sun-font-mono);
}
.fr-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>
