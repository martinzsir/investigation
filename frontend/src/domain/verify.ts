// 核查工作区领域逻辑（REQ-V-006）：core/verify_machine.py 的前端同构镜像。
// 状态机合法迁移表以后端 core 为唯一事实源；本文件只镜像用于按钮显隐/门禁序，
// 最终裁决由 Worker（REQ-V-004）兜底，前端永不自创迁移。
// 与 domain/clue.ts 同纪律：纯函数、无 I/O，组件只做薄编排。

import { STATUS_TONE_META, type StatusTone, type ToneMeta } from '../design/tokens'

// ---------- 核查七态（与 core/verify_machine.py VERIFY_STATUSES 逐字对齐） ----------

export const VERIFY_STATUS = {
  SUGGESTED: '建议',
  PENDING: '待核查',
  IN_PROGRESS: '核查中',
  CONFIRMED: '已证实',
  DISPROVED: '已查否',
  UNVERIFIABLE: '无法核实',
  IGNORED: '已忽略',
} as const

export type VerifyStatus = (typeof VERIFY_STATUS)[keyof typeof VERIFY_STATUS]

/** 白名单（镜像 core VERIFY_TRANSITIONS；未列出即非法，按钮不渲染） */
export const VERIFY_TRANSITIONS: Record<string, VerifyStatus[]> = {
  建议: [VERIFY_STATUS.PENDING, VERIFY_STATUS.IGNORED],
  已忽略: [VERIFY_STATUS.PENDING],
  待核查: [VERIFY_STATUS.IN_PROGRESS, VERIFY_STATUS.CONFIRMED,
    VERIFY_STATUS.DISPROVED, VERIFY_STATUS.UNVERIFIABLE],
  核查中: [VERIFY_STATUS.CONFIRMED, VERIFY_STATUS.DISPROVED,
    VERIFY_STATUS.UNVERIFIABLE, VERIFY_STATUS.PENDING],
  已证实: [VERIFY_STATUS.IN_PROGRESS],
  已查否: [VERIFY_STATUS.IN_PROGRESS],
  无法核实: [VERIFY_STATUS.IN_PROGRESS],
}

/** 终态裁决：必须随附结论（镜像 core CONCLUSION_REQUIRED） */
const CONCLUSION_REQUIRED: ReadonlySet<string> = new Set([
  VERIFY_STATUS.CONFIRMED, VERIFY_STATUS.DISPROVED,
])

/** 门禁计数口径（镜像 core VERIFY_PENDING_STATUSES）：建议/已忽略不进固证门禁 */
export const VERIFY_PENDING_STATUSES: readonly string[] = [
  VERIFY_STATUS.PENDING, VERIFY_STATUS.IN_PROGRESS,
]
export const VERIFY_CONCLUDED_STATUSES: readonly string[] = [
  VERIFY_STATUS.CONFIRMED, VERIFY_STATUS.DISPROVED, VERIFY_STATUS.UNVERIFIABLE,
]

/** 进度 chips / 列表的状态展示序（与 state_store _VERIFY_STATUS_RANK_SQL 对齐） */
export const VERIFY_STATUS_ORDER: readonly string[] = [
  VERIFY_STATUS.PENDING, VERIFY_STATUS.IN_PROGRESS,
  VERIFY_STATUS.CONFIRMED, VERIFY_STATUS.DISPROVED, VERIFY_STATUS.UNVERIFIABLE,
  VERIFY_STATUS.SUGGESTED, VERIFY_STATUS.IGNORED,
]

// ---------- 状态机纯函数 ----------

export function canVerifyTransition(cur: string, nxt: string): boolean {
  return VERIFY_TRANSITIONS[cur]?.includes(nxt as VerifyStatus) ?? false
}

/** 当前状态的合法目标态（未知状态返回空数组，不抛异常） */
export function legalVerifyTargets(cur: string): string[] {
  return VERIFY_TRANSITIONS[cur] ? [...VERIFY_TRANSITIONS[cur]] : []
}

/** 终态裁决是否必填结论（已证实/已查否；无法核实允许挂起转外部调取） */
export function verifyConclusionRequired(nextStatus: string): boolean {
  return CONCLUSION_REQUIRED.has(nextStatus)
}

export function isVerifyConcluded(status: string): boolean {
  return VERIFY_CONCLUDED_STATUSES.includes(status)
}

// ---------- 行动作（由状态机现算；非法动作永不出现） ----------

export type VerifyActionKind =
  | 'adopt'      // 建议 → 待核查（可带改写文本）
  | 'ignore'     // 建议 → 已忽略
  | 'readopt'    // 已忽略 → 待核查
  | 'start'      // 待核查 → 核查中
  | 'confirm'    // → 已证实（结论必填）
  | 'disprove'   // → 已查否（结论必填）
  | 'unverifiable' // → 无法核实
  | 'sendback'   // 核查中 → 待核查（退回）
  | 'reopen'     // 终态 → 核查中（重开/翻案）

export interface VerifyAction {
  kind: VerifyActionKind
  label: string
  target: string
  /** 已证实/已查否 */
  conclusionRequired: boolean
  /** 采纳建议项：弹窗提供「改一改」文本框 */
  rewrite: boolean
  /** 按钮语义色（primary/danger/warn/muted） */
  tone: 'primary' | 'success' | 'danger' | 'muted'
}

function actionFor(cur: string, nxt: string): VerifyAction {
  if (cur === VERIFY_STATUS.SUGGESTED && nxt === VERIFY_STATUS.PENDING) {
    return { kind: 'adopt', label: '采纳', target: nxt,
      conclusionRequired: false, rewrite: true, tone: 'primary' }
  }
  if (cur === VERIFY_STATUS.SUGGESTED && nxt === VERIFY_STATUS.IGNORED) {
    return { kind: 'ignore', label: '忽略', target: nxt,
      conclusionRequired: false, rewrite: false, tone: 'muted' }
  }
  if (cur === VERIFY_STATUS.IGNORED && nxt === VERIFY_STATUS.PENDING) {
    return { kind: 'readopt', label: '重新采纳', target: nxt,
      conclusionRequired: false, rewrite: false, tone: 'primary' }
  }
  if (cur === VERIFY_STATUS.PENDING && nxt === VERIFY_STATUS.IN_PROGRESS) {
    return { kind: 'start', label: '开始核查', target: nxt,
      conclusionRequired: false, rewrite: false, tone: 'primary' }
  }
  if (cur === VERIFY_STATUS.IN_PROGRESS && nxt === VERIFY_STATUS.PENDING) {
    return { kind: 'sendback', label: '退回', target: nxt,
      conclusionRequired: false, rewrite: false, tone: 'muted' }
  }
  if (VERIFY_CONCLUDED_STATUSES.includes(cur) && nxt === VERIFY_STATUS.IN_PROGRESS) {
    return { kind: 'reopen', label: '重开', target: nxt,
      conclusionRequired: false, rewrite: false, tone: 'muted' }
  }
  if (nxt === VERIFY_STATUS.CONFIRMED) {
    return { kind: 'confirm', label: '证实', target: nxt,
      conclusionRequired: true, rewrite: false, tone: 'success' }
  }
  if (nxt === VERIFY_STATUS.DISPROVED) {
    return { kind: 'disprove', label: '查否', target: nxt,
      conclusionRequired: true, rewrite: false, tone: 'danger' }
  }
  // 无法核实（待核查/核查中 → 无法核实）
  return { kind: 'unverifiable', label: '无法核实', target: nxt,
    conclusionRequired: false, rewrite: false, tone: 'muted' }
}

/**
 * 当前态可渲染行动作（按 VERIFY_TRANSITIONS 现算，顺序即白名单顺序）。
 * 未知状态 → []（非法动作不渲染）。
 */
export function allowedVerifyActions(status: string): VerifyAction[] {
  return legalVerifyTargets(status).map((nxt) => actionFor(status, nxt))
}

// ---------- 核查项类型（GET verify-items 契约，REQ-V-005） ----------

export interface VerifyItem {
  item_id: string
  /** pending_degrade|pending_hypothesis|pending_rule|inference|manual|suggested */
  kind: string
  text: string
  /** auto=供给生成 / manual=人工添加 / suggested=手册建议 / ai_draft=LLM 草案 */
  origin: string
  status: string
  conclusion: string
  operator: string
  updated_at: string
  /** function=库内可复跑 / external=需外部调取（REQ-V-018 建议项路由） */
  channel: string
  ref_function: string
  external: { target?: string; material?: string } | null
  falsification: string
}

export interface VerifyProgress {
  total: number
  concluded: number
  pending: number
  suggested: number
  ignored: number
  by_status: Record<string, number>
}

export interface VerifyItemsPage {
  items: VerifyItem[]
  progress: VerifyProgress
  /** state.sqlite 不存在（工作区未初始化）时 false */
  available: boolean
}

// ---------- 展示元数据 ----------

/** item.kind → 徽标文案（未知 kind 原样透出） */
const VERIFY_KIND_LABEL: Record<string, string> = {
  pending_degrade: '降级待核',
  pending_hypothesis: '假设待核',
  pending_rule: '规则待核',
  inference: '推断',
  manual: '人工',
  suggested: '手册建议',
}

export function verifyKindLabel(kind: string): string {
  return VERIFY_KIND_LABEL[kind] ?? kind
}

/** 是否需要「人工」角标（origin=manual；suggested/auto 各自另有样式） */
export function isManualOrigin(origin: string): boolean {
  return origin === 'manual'
}

/**
 * 核查七态色调：复用 StatusBadge 变量色板（tokens STATUS_TONE_META），
 * states.json 不含核查态（核查态不是线索处置态），故在本域单独声明映射。
 * 建议/已忽略为未采纳旁路态，组件以虚线描边二次区分。
 */
const VERIFY_STATUS_TONE: Record<string, StatusTone> = {
  待核查: 'warning',
  核查中: 'info',
  已证实: 'success',
  已查否: 'danger',
  无法核实: 'muted',
  建议: 'muted',
  已忽略: 'muted',
}

export function verifyStatusMeta(status: string): ToneMeta {
  return STATUS_TONE_META[VERIFY_STATUS_TONE[status] ?? 'warning']
}

/** 建议态/已忽略：未采纳的手册建议生命周期（虚线描边） */
export function isVerifySideStatus(status: string): boolean {
  return status === VERIFY_STATUS.SUGGESTED || status === VERIFY_STATUS.IGNORED
}

/** 进度条百分比（total=0 → 0） */
export function verifyProgressPercent(p: VerifyProgress | null | undefined): number {
  if (!p || p.total <= 0) return 0
  return Math.round((p.concluded / p.total) * 100)
}
