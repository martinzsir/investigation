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
  /** REQ-V-017 一键复跑结果（replay_json 投影；未复跑=null） */
  replay: VerifyReplay | null
  /** REQ-V-011 已挂接书证（verify-items 清单行内投影；可能缺省为空数组） */
  evidence?: EvidenceMaterial[]
}

/**
 * REQ-V-017 复跑结果（Worker op=replay 回填 replay_json 的投影契约；
 * server/app/worker/verify.py _replay 落盘结构）。status/conclusion 等裁决
 * 四列不在其中——复跑是 AI 辅助推演，永不改写人工裁决。
 */
export interface VerifyReplay {
  /** 实际执行的 Function（主跑成功=映射主函数；备选接管=fallback_function） */
  function: string
  /** 主跑失败、备选 Function 接管 */
  fallback_used: boolean
  /** playbook=手册映射 / fallback=维度关键词兜底 */
  mapping_source: string
  params_used: Record<string, unknown>
  output_type: string | null
  /** SQL Function 为行数组 / py Function 为标量或对象（前端只做行数摘要） */
  result: unknown
  /** 结构降级（如空库缺 obj_* 表）：任务成功但无业务结果 */
  degraded: boolean
  degraded_reason: string | null
  /** 复跑时的语义层版本（v{N}） */
  version: string
  source_row_ids: string[]
  operator: string
  replayed_at: string
}

// ---------- REQ-V-010/011 书证材料（clue_evidence 读面投影） ----------

/** 书证大小上限（与后端 evidence_store.py MAX_EVIDENCE_SIZE 对齐：20MB） */
export const EVIDENCE_MAX_SIZE = 20 * 1024 * 1024

/** 书证类型白名单（与后端 routers/evidence.py MATERIAL_TYPES 逐字对齐） */
export const EVIDENCE_MATERIAL_TYPES = [
  '缴款单', '监控截图', '合同', '付款凭证', '审批文件', '其他',
] as const

/**
 * 书证元数据（GET evidence 清单与 verify-items 行内挂接投影同形状；
 * 行内投影是子集，未回填字段在展示侧容错为空串）。
 */
export interface EvidenceMaterial {
  material_id: string
  /** 已挂接的核查项；未挂接为空串 */
  item_id: string
  material_type: string
  /** 落盘文件名（material_id + 安全化原名） */
  filename: string
  /** 用户上传时的原始文件名（下载/展示用） */
  orig_name: string
  sha256: string
  size: number
  note: string
  uploaded_by: string
  uploaded_at: string
}

/** 字节大小人类可读（B/KB/MB，整数 KB、1 位小数 MB） */
export function evidenceSizeLabel(size: number): string {
  const n = Number(size) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
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

// ---------- REQ-V-013 调取清单（core/verify_machine.py 请求状态机镜像） ----------

/** 调取请求四态（与 core REQUEST_STATUSES 逐字对齐；「超期」是读面派生非存储态） */
export const REQUEST_STATUS = {
  DRAFT: '待发起',
  SENT: '已发起',
  RETURNED: '材料已回',
  CLOSED: '关闭',
} as const

export type RequestStatus = (typeof REQUEST_STATUS)[keyof typeof REQUEST_STATUS]

/** 白名单（镜像 core REQUEST_TRANSITIONS；未列出即非法，按钮不渲染） */
export const REQUEST_TRANSITIONS: Record<string, RequestStatus[]> = {
  待发起: [REQUEST_STATUS.SENT],
  已发起: [REQUEST_STATUS.RETURNED, REQUEST_STATUS.CLOSED],
  材料已回: [REQUEST_STATUS.CLOSED],
}

export function canRequestTransition(cur: string, nxt: string): boolean {
  return REQUEST_TRANSITIONS[cur]?.includes(nxt as RequestStatus) ?? false
}

/** 当前状态的合法目标态（未知状态返回空数组，不抛异常） */
export function legalRequestTargets(cur: string): string[] {
  return REQUEST_TRANSITIONS[cur] ? [...REQUEST_TRANSITIONS[cur]] : []
}

/** 台账行（GET verify-requests 契约；overdue 为服务端读面派生） */
export interface VerifyRequestRow {
  request_id: string
  clue_id: string
  item_id: string | null
  target: string
  material: string
  legal_instrument: string
  handler: string
  due_date: string
  status: string
  note: string
  created_by: string
  created_at: string
  updated_at: string
  /** due_date 已过且 status=已发起（服务端派生，存储状态不变） */
  overdue: boolean
}

export interface VerifyRequestsPage {
  items: VerifyRequestRow[]
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

// ---------- REQ-V-019 LLM 核查方向草案（档位提示 + AI 草案徽标） ----------

/** 草案部署档（与后端 draft 端点响应 mode 对齐；off=能力关闭/无交集） */
export type DraftMode = 'local' | 'cloud' | 'off'

/** 草案提案（后端 draft 端点 proposals 元素；落提案队列待人审） */
export interface VerifyDraftProposal {
  proposal_id: string
  text: string
  dimension: string
  channel: string
  ref_function: string
  author: string
}

/** AI 草案徽标判据（origin=ai_draft：LLM 草案经人审批通过成项） */
export function isAiDraftOrigin(origin: string): boolean {
  return origin === 'ai_draft'
}

/**
 * 档位状态提示（ADR-V-8）：off=置灰原因；local=数据不出机；cloud=数据将出网。
 * 前端不预判会话档位（服务端会话∩策略交集唯一裁决），按调用结果展示。
 */
export function draftModeHint(mode: DraftMode): string {
  if (mode === 'local') return '本地模型（数据不出机）'
  if (mode === 'cloud') return '公网模型（数据将出网）'
  return '内核隔离模式，LLM 能力关闭'
}

/** 草案结果提示语：N 条待审批；去重/无效候选附带过滤说明 */
export function draftResultSummary(
  mode: DraftMode,
  n: number,
  duplicates = 0,
  dropped = 0,
): string {
  if (n <= 0) {
    const parts: string[] = ['本次未产出新的 AI 草案']
    if (duplicates > 0) parts.push(`${duplicates} 条与已有草案重复`)
    if (dropped > 0) parts.push(`${dropped} 条未通过确定性校验已过滤`)
    return `${parts.join('，')}（mode=${mode}）`
  }
  const parts: string[] = [
    `已生成 ${n} 条 AI 草案（mode=${mode}），待审批`,
  ]
  if (duplicates > 0) parts.push(`另有 ${duplicates} 条重复未提交`)
  if (dropped > 0) parts.push(`${dropped} 条无效候选已过滤`)
  return `${parts.join('；')}。审批通过后才会成为正式核查项`
}

// ---------- REQ-V-018 结构化构造器（确定性拼装，组件薄调用） ----------

/** 核查渠道：function=库内可复跑 / external=外部调取 */
export type VerifyChannel = 'function' | 'external'

/** 数据源常见主体列名（source_rows 候选抽取依据；与后端主体列约定对齐） */
const SUBJECT_COLUMNS = ['人', 'from_raw', '资金主体', '对方户名', '户名',
  'to_raw', '姓名', 'person', '主体'] as const

/**
 * 实体候选抽取：命中主体列的非空字符串值去重排序。
 * source_rows 缺省/无命中 → 空数组（构造器不渲染，回落纯手填）。
 */
export function entityCandidates(
  sourceRows?: Array<Record<string, unknown>>,
): string[] {
  const hits = new Set<string>()
  for (const col of SUBJECT_COLUMNS) {
    for (const row of sourceRows ?? []) {
      const v = row?.[col]
      if (typeof v === 'string' && v.trim()) hits.add(v.trim())
    }
  }
  return [...hits].sort()
}

export interface VerifyCtorInput {
  /** 实体（entityCandidates 候选之一，可空） */
  entity?: string
  /** 维度（ontology dimensions，可空） */
  dimension?: string
  /** 核查点（要核实什么） */
  point?: string
  /** function=库内可复跑 / external=外部调取 */
  channel: VerifyChannel
  /** 外部调取对象/材料（channel=external 时拼入渠道段） */
  extTarget?: string
  extMaterial?: string
}

/**
 * 结构化构造 → 核查项文本（确定性模板；提交仍只发 text，用户可「改一改」）。
 * 无实体/维度时不输出前导冒号；外部渠道按 对象/材料 有无拼细节段。
 */
export function assembleVerifyText(input: VerifyCtorInput): string {
  const segs: string[] = []
  const entity = (input.entity ?? '').trim()
  const dimension = (input.dimension ?? '').trim()
  const point = (input.point ?? '').trim()
  if (entity) segs.push(`对「${entity}」`)
  if (dimension) segs.push(`开展${dimension}维度核查`)
  if (point) segs.push(segs.length ? `：${point}` : point)
  let text = segs.join('')
  if (input.channel === 'external') {
    const t = (input.extTarget ?? '').trim()
    const m = (input.extMaterial ?? '').trim()
    const detail = t && m ? `向${t}调取${m}` : (t ? `向${t}` : (m ? `调取${m}` : ''))
    text += `（渠道：外部调取${detail ? `·${detail}` : ''}）`
  } else {
    text += '（渠道：库内可复跑）'
  }
  return text.trim()
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

// ---------- REQ-V-017 一键复跑回填（结果卡片展示纯函数） ----------

/** mapping_source → 中文标签（未知来源原样透出，不吞新枚举） */
const REPLAY_SOURCE_LABEL: Record<string, string> = {
  playbook: '手册映射',
  fallback: '关键词兜底',
}

export function replaySourceLabel(source: string): string {
  return REPLAY_SOURCE_LABEL[source] ?? source
}

/**
 * 复跑结果摘要（结果卡片正文）：行数组报行数、标量原样透出、对象不展开。
 * 前端只做形状摘要、不解释业务语义——判读结论由人下，与"不下定性结论"纪律一致。
 * 对（方案A）：对象/行数组可经 replayResultExpandable 折叠查看原始 JSON，
 * 摘要不再指引"详见导出"。
 */
export function replayResultSummary(replay: VerifyReplay): string {
  const r = replay.result
  if (Array.isArray(r)) return `返回 ${r.length} 行结果`
  if (r == null || r === '') {
    return replay.degraded
      ? '无结果（结构降级，见降级原因）'
      : '无结果'
  }
  if (typeof r === 'object') {
    return replay.degraded
      ? '计算完成（对象结果，结构降级）'
      : '计算完成（对象结果）'
  }
  return String(r)
}

/**
 * 原始结果是否可展开：仅对象/非空行数组有明细可看；
 * 空结果（null/''）与标量（摘要行已原样透出）不渲染折叠入口。
 */
export function replayResultExpandable(replay: VerifyReplay): boolean {
  const r: unknown = replay.result
  if (r === null || typeof r !== 'object') return false
  if (Array.isArray(r)) return r.length > 0
  return Object.keys(r as Record<string, unknown>).length > 0
}

/**
 * 原始结果 JSON 文本（折叠区只读展示，缩进 2 空格）。
 * 结果来自内核回填的可序列化 JSON，循环引用仅理论可能——异常回落 String 兜底。
 */
export function replayResultJson(replay: VerifyReplay): string {
  try {
    return JSON.stringify(replay.result, null, 2)
  } catch {
    return String(replay.result)
  }
}
