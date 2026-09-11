// 线索/处置领域逻辑：红线判定的唯一事实源（FE-C-013~015、FE-T-003~006/010/015/021）。
// 组件只做薄编排，所有门禁/升格/遮蔽/空链判定一律走本模块纯函数，确定性可测。
// 与 core/registry.py ClueStatus、core/functions.py jian_cross_level、
// core/access.py ROLE_RANK、core/audit.py 空链红线语义对齐。
//
// 六项解耦（R5/R6）：状态集/动作/等级名/维度均由 ontology-config 下发，
// 本文件常量仅为默认包快照（拉取失败/测试环境兜底）；所有 helper 接受可选
// OntologyConfig，缺省走默认值，调用签名向后兼容。

import type { OntologyConfig, StateDecl } from '../api/endpoints/ontologyConfig'
import { FILED_STATUS_META, STATUS_TONE_META, type StatusTone } from '../design/tokens'

// ---------- 线索处置五态（五态配色不可变，清单 FE-D「五态配色」表） ----------

export const CLUE_STATUS = {
  PENDING: '待查',
  VERIFYING: '查证中',
  EXCLUDED: '已排除',
  CONFIRMED: '已固证',
  FILED: '已立案',
} as const

export type ClueStatus = (typeof CLUE_STATUS)[keyof typeof CLUE_STATUS]

export interface StatusMeta {
  /** CSS 变量名（组件内 var(--...) 消费，禁止硬编码色值） */
  bg: string
  border: string
  text: string
  icon: string
  /** 终态/旁路标记 */
  terminal?: boolean
}

/** 默认包快照（兜底；权威样式经 statusMetaOf 按 states 声明 tone 派生） */
export const STATUS_META: Record<ClueStatus, StatusMeta> = {
  待查: { bg: 'rgba(107,131,153,.15)', border: '#6B8399', text: '#8FB3CC', icon: '○' },
  查证中: { bg: 'rgba(0,212,255,.15)', border: '#00D4FF', text: '#00D4FF', icon: '◐' },
  已固证: { bg: 'rgba(46,212,122,.15)', border: '#2ED47A', text: '#2ED47A', icon: '●' },
  已排除: { bg: 'rgba(107,131,153,.08)', border: '#3D5668', text: '#5A7A94', icon: '✕' },
  已立案: { bg: 'rgba(196,30,58,.25)', border: '#D4AF37', text: '#FFD87A', icon: '★', terminal: true },
}

/** 取状态声明（未加载/未知名回落 undefined） */
export function stateDecl(status: string, cfg?: OntologyConfig): StateDecl | undefined {
  return cfg?.states.find((s) => s.name === status)
}

/** 受控终态：terminal && requires_role==='human'（红线：已立案类状态 AI 不可置位） */
export function isControlledTerminal(status: string, cfg?: OntologyConfig): boolean {
  const s = stateDecl(status, cfg)
  return !!s?.terminal && s.requires_role === 'human'
}

/** 终态（含旁路终态，按声明；未配置时仅已立案视为终态） */
export function isTerminalState(status: string, cfg?: OntologyConfig): boolean {
  const s = stateDecl(status, cfg)
  return s ? s.terminal : status === CLUE_STATUS.FILED
}

function toneOf(status: string, cfg?: OntologyConfig): StatusTone {
  const t = stateDecl(status, cfg)?.tone
  return (t as StatusTone) in STATUS_TONE_META
    ? (t as StatusTone)
    : 'warning'
}

/**
 * 状态样式：states.json tone → tokens 令牌；受控终态叠加金橙 FILED_STATUS_META。
 * 组件禁止再按状态名分支取色。
 */
export function statusMetaOf(status: string, cfg?: OntologyConfig): StatusMeta {
  if (isControlledTerminal(status, cfg)) return { ...FILED_STATUS_META, terminal: true }
  const meta = STATUS_TONE_META[toneOf(status, cfg)]
  const s = stateDecl(status, cfg)
  return { ...meta, terminal: s?.terminal }
}

// ---------- 处置动作（actions.json 下发；默认 = verify/reset/exclude/confirm/file） ----------

export type ClueAction = 'verify' | 'reset' | 'exclude' | 'confirm' | 'file'

export const ACTION_LABEL: Record<ClueAction, string> = {
  verify: '开始查证',
  reset: '退回待查',
  exclude: '排除线索',
  confirm: '固证',
  file: '立案',
}

/** 默认包状态机快照（与 ontology/default 对齐；权威以 config 下发为准） */
const TRANSITIONS: Record<ClueStatus, ClueAction[]> = {
  待查: ['verify', 'exclude'],
  查证中: ['confirm', 'exclude', 'reset'],
  已固证: ['file', 'exclude'],
  已排除: ['reset'],
  已立案: [], // 终态
}

/**
 * 当前态可执行动作（actions.json 声明现算）：
 * 动作可见优先用 only_from（D1 显式收紧），缺省时才用 allowed_from（迁移反推）。
 * 未知动作名（非五类处置动作）不进本视图。
 */
export function allowedActions(status: ClueStatus | string, cfg?: OntologyConfig): ClueAction[] {
  if (!cfg) return TRANSITIONS[status as ClueStatus] ?? []
  const out: ClueAction[] = []
  for (const a of cfg.actions) {
    if (!Object.prototype.hasOwnProperty.call(ACTION_LABEL, a.name)) continue
    const froms = a.only_from && a.only_from.length > 0 ? a.only_from : a.allowed_from
    if (froms.includes(status)) out.push(a.name as ClueAction)
  }
  // 稳定序：按声明 actions 顺序本身即确定，无需再排
  return out
}

/** 权威按钮文案（title 声明；D4 与现 UI 对齐） */
export function actionTitle(action: ClueAction | string, cfg?: OntologyConfig): string {
  return cfg?.actions.find((a) => a.name === action)?.title
    ?? ACTION_LABEL[action as ClueAction]
    ?? action
}

/** 动作目标态（仅用于前端确认文案；真值以服务端/审计链为准） */
export const ACTION_TARGET: Record<ClueAction, ClueStatus> = {
  verify: CLUE_STATUS.VERIFYING,
  reset: CLUE_STATUS.PENDING,
  exclude: CLUE_STATUS.EXCLUDED,
  confirm: CLUE_STATUS.CONFIRMED,
  file: CLUE_STATUS.FILED,
}

export function actionTarget(action: ClueAction | string, cfg?: OntologyConfig): string {
  return cfg?.actions.find((a) => a.name === action)?.target_status
    ?? ACTION_TARGET[action as ClueAction]
    ?? ''
}

/** 动作是否需要指定具名 human（file 类受控终态） */
export function actionRequiresHuman(action: ClueAction | string, cfg?: OntologyConfig): boolean {
  const a = cfg?.actions.find((x) => x.name === action)
  return a ? a.requires_role === 'human' : action === 'file'
}

// ---------- 侦查五维（dimensions.json：资金/通讯/行为/关系/时间，FE-D-007 色板） ----------
// 注意：这是「侦查维度/数据通道」（线索 detail.dimension、雷达、房间色板），
// 与兵法五间（jians.json：因间/内间/反间/死间/生间，线索 jian_types）不是一套。

export const JIAN_ROOMS = ['资金', '通讯', '行为', '关系', '时间'] as const
export type JianRoom = (typeof JIAN_ROOMS)[number]

/** 默认包维度名快照；权威维度集取 config.dimensions（数量/名称可变，雷达按 N 参数化） */
export function dimensionRooms(cfg?: OntologyConfig): string[] {
  const names = cfg?.dimensions.map((d) => d.name)
  return names && names.length > 0 ? names : [...JIAN_ROOMS]
}

export const JIAN_ROOM_VAR: Record<JianRoom, string> = {
  资金: 'var(--sun-jian-fund)',
  通讯: 'var(--sun-jian-comms)',
  行为: 'var(--sun-jian-behavior)',
  关系: 'var(--sun-jian-relation)',
  时间: 'var(--sun-jian-time)',
}

/**
 * 五维交叉等级（core/functions.py jian_cross_level 同构）：
 * 单源=观察 → 双源=线索 → 三源=可立案依据候选。
 * 红线 FE-T-006：单源候选必须为「观察」。
 * n = 独立通道命中数（LLM 同源多次判读只计一个间，FE-T-007 在 MVP-2 落地）。
 * 等级**名称**由 cross_levels 声明；1/2/3 映射红线不可配置。
 */
export type CrossLevel = string

const DEFAULT_LEVEL_NAMES: Record<number, string> = {
  1: '观察',
  2: '线索',
  3: '可立案依据候选',
}

export function crossLevel(roomCount: number, cfg?: OntologyConfig): CrossLevel {
  const n = roomCount >= 3 ? 3 : roomCount === 2 ? 2 : 1
  if (cfg) {
    const hit = cfg.cross_levels.find((l) => l.min_independent_sources === n)
    if (hit) return hit.name
  }
  return DEFAULT_LEVEL_NAMES[n]
}

/** 异常通道线索恒「待核实」，绝不参与交叉升格（core/anomaly_channel.py 红线） */
export const ANOMALY_LEVEL = '待核实'

// ---------- R13：优先级分可解释分解（scoring.json@v2） ----------

import type { ScoreBasis } from '../api/endpoints/clues'

/** 计分维度英文键 → 中文标签（与 scoring.json dimensions 对齐） */
export const SCORE_DIM_LABELS: Record<string, string> = {
  confidence: '假设置信度',
  jian_coverage: '间类覆盖',
  data_strength: '数据强度',
}

export interface ScoreBasisRow {
  key: string
  label: string
  raw: number
  weight: number
  contrib: number
}

/** 把后端 score_basis 对象展开为稳定顺序的分解行（未知键原样保留键名） */
export function scoreBasisRows(basis?: ScoreBasis | null): ScoreBasisRow[] {
  if (!basis) return []
  return Object.entries(basis).map(([key, t]) => ({
    key,
    label: SCORE_DIM_LABELS[key] ?? key,
    raw: t?.raw ?? 0,
    weight: t?.weight ?? 0,
    contrib: t?.contrib ?? 0,
  }))
}

/** 分解贡献分合计（priority_score 的可复算口径，保留 3 位） */
export function scoreContribSum(basis?: ScoreBasis | null): number | null {
  const rows = scoreBasisRows(basis)
  return rows.length
    ? Math.round(rows.reduce((a, r) => a + r.contrib, 0) * 1000) / 1000
    : null
}

// ---------- 角色门禁（FE-C-014 四重门禁第 1 关；core/access.py ROLE_RANK） ----------

/** 机器占位 operator（system/ai/assistant/agent:*）：红线 FE-T-003，永远不可立案 */
const MACHINE_ROLE_RE = /^(system|ai|assistant|agent)/

export const ROLE_RANK: Record<string, number> = {
  见习: 0,
  正兵: 1,
  偏将: 2,
  主办: 3,
  human: 4,
  system: 99,
}

/** 角色是否可见「已立案」按钮（门禁 1：不通过=按钮不渲染，非置灰） */
export function canFileRole(role: string): boolean {
  if (MACHINE_ROLE_RE.test(role)) return false
  return (ROLE_RANK[role] ?? 0) >= 3
}

export function isMachineRole(role: string): boolean {
  return MACHINE_ROLE_RE.test(role)
}

// ---------- 立案四重门禁（FE-C-014：不渲染 > 置灰 > 报错） ----------

export interface FileGate {
  /** 门禁 1 不通过：按钮根本不渲染 */
  render: boolean
  /** 四重全过（含弹窗内法定依据非空）：可提交 */
  enabled: boolean
  /**
   * 立案「入口」可用：前三重门禁通过（角色 + 未降级 + 前置状态）。
   * 法定依据（第 3 重）只门控弹窗内的确认提交按钮——若连入口都置灰，
   * 弹窗永远无法打开、依据永远无法填入，形成死锁。
   */
  entryEnabled: boolean
  reason: string
}

/**
 * @param degraded 降级态（FE-C-027/FE-T-010）：降级时立案/导出禁用且写明原因
 * @param cfg Ontology 声明：file 动作的目标态/前置态从 actions.json 派生
 */
export function fileGate(
  role: string,
  status: ClueStatus | string,
  legalBasis: string,
  degraded: boolean,
  cfg?: OntologyConfig,
): FileGate {
  if (!canFileRole(role)) {
    return { render: false, enabled: false, entryEnabled: false, reason: '' }
  }
  if (degraded) {
    return {
      render: true,
      enabled: false,
      entryEnabled: false,
      reason: '系统降级运行中：立案/导出已临时禁用（见顶部降级通栏）',
    }
  }
  const fileDecl = cfg?.actions.find((a) => a.name === 'file')
  const prerequisite = (fileDecl?.only_from && fileDecl.only_from.length > 0
    ? fileDecl.only_from
    : fileDecl?.allowed_from) ?? [CLUE_STATUS.CONFIRMED]
  if (!prerequisite.includes(status)) {
    return {
      render: true,
      enabled: false,
      entryEnabled: false,
      reason: `需先完成固证（仅「${prerequisite.join('/')}」线索可立案）`,
    }
  }
  if (!legalBasis.trim()) {
    // 入口可开（在弹窗内填写依据），仅最终提交门控
    return { render: true, enabled: false, entryEnabled: true, reason: '请填写法定依据/案号' }
  }
  return { render: true, enabled: true, entryEnabled: true, reason: '' }
}

/** 降级态下所有写动作禁用（FE-T-010） */
export function degradeReason(degraded: boolean): string {
  return degraded ? '系统降级运行中，写操作已临时禁用（见顶部降级通栏）' : ''
}

// ---------- 三栏证据（FE-C-013；红线 FE-T-004 推断必挂溯源 / FE-T-005 三栏不合并） ----------

export type EvidenceKind = 'fact' | 'inference' | 'pending'

export interface SourceRef {
  /** 行 URI（等宽展示 + 可复制，FE-C-012） */
  row_uri: string
  source?: string
}

export interface EvidenceItem {
  id: string
  kind: EvidenceKind
  text: string
  /** 推断必须挂溯源；缺失则拒绝渲染（红线 FE-T-004） */
  source_rows?: SourceRef[]
  room?: string
}

export interface ThreeColumn {
  facts: EvidenceItem[]
  inferences: EvidenceItem[]
  pending: EvidenceItem[]
  /** 因缺溯源被拒绝渲染的推断条数（界面留痕，红线可见） */
  droppedInferences: number
}

/**
 * 三栏物理分隔：事实（青）/推断（琥珀）/待核实（灰虚线）。
 * - 推断栏只收 kind==='inference' 且 source_rows 非空的条目；
 * - 无 source_rows 的推断 → 拒绝渲染并计数（FE-T-004）；
 * - 事实栏只收 kind==='fact'，推断内容永不混入事实栏（FE-T-005）。
 */
export function partitionEvidence(items: EvidenceItem[]): ThreeColumn {
  const col: ThreeColumn = { facts: [], inferences: [], pending: [], droppedInferences: 0 }
  for (const it of items) {
    if (it.kind === 'fact') {
      col.facts.push(it)
    } else if (it.kind === 'pending') {
      col.pending.push(it)
    } else if (it.kind === 'inference') {
      if (Array.isArray(it.source_rows) && it.source_rows.length > 0) {
        col.inferences.push(it)
      } else {
        col.droppedInferences += 1
      }
    }
  }
  return col
}

// ---------- 字段遮蔽（FE-C-010；红线 FE-T-021 明文不入缓存） ----------

export type MaskPolicy = 'visible' | 'masked' | 'denied'

/** 手机号：前 3 后 4 */
export function maskPhone(v: string): string {
  const digits = v.replace(/\D/g, '')
  if (digits.length < 7) return '****'
  return `${digits.slice(0, 3)}****${digits.slice(-4)}`
}

/** 身份证号：前 6 后 4 */
export function maskIdCard(v: string): string {
  const s = v.trim()
  if (s.length < 11) return '****'
  return `${s.slice(0, 6)}********${s.slice(-4)}`
}

/**
 * 字段渲染策略：denied 一律 `****`（无权限不渲染真值，FE-C-015 fail-closed）；
 * masked 按字段类型遮蔽；visible 出真值。
 */
export function maskField(value: string, policy: MaskPolicy, type: 'phone' | 'idcard' | 'text' = 'text'): string {
  if (policy === 'denied') return '****'
  if (policy === 'masked') {
    if (type === 'phone') return maskPhone(value)
    if (type === 'idcard') return maskIdCard(value)
    return value ? '****' : ''
  }
  return value
}

// ---------- 审计链完整性（FE-C-018；红线 FE-T-015 空链警示） ----------

export interface AuditVerify {
  chain_ok: boolean
  expected_count: number
  actual_count: number
  broken_links: unknown[]
  empty_chain?: boolean
}

export interface AuditBanner {
  tone: 'ok' | 'warn'
  title: string
  detail: string
}

/**
 * 空链（actual_count===0 / empty_chain）一律 warn 语义，
 * 不得显示「校验通过」（W-023 AC-4，平台案件无合法空链场景）。
 */
export function auditBanner(v: AuditVerify | null): AuditBanner {
  if (!v) {
    return { tone: 'warn', title: '完整性校验未完成', detail: '尚未取得校验结果，请刷新重试' }
  }
  if (v.actual_count === 0 || v.empty_chain) {
    return {
      tone: 'warn',
      title: '审计链为空',
      detail: '当前案件无任何审计留痕（0 条），完整性校验不通过',
    }
  }
  if (!v.chain_ok || (v.broken_links?.length ?? 0) > 0) {
    return {
      tone: 'warn',
      title: '完整性校验异常',
      detail: `断链 ${v.broken_links?.length ?? 0} 处；期望 ${v.expected_count} 条 / 实际 ${v.actual_count} 条`,
    }
  }
  return {
    tone: 'ok',
    title: '完整性校验通过',
    detail: `记录总数 ${v.actual_count} 条，哈希链连续无断环`,
  }
}

// ---------- 仪表盘健康度（FE-P-001：零记录走 warn，不显示「一切正常」） ----------

export interface HealthSection {
  available?: boolean
  status?: string
  诊断总数?: number
  计数?: { critical?: number; warning?: number; info?: number }
  说明?: string
}

export function healthBanner(h: HealthSection | null | undefined): AuditBanner {
  const total = h?.诊断总数 ?? 0
  const sev = h?.计数 ?? {}
  if (!h?.available || total === 0) {
    return {
      tone: 'warn',
      title: '尚无诊断留痕',
      detail: h?.说明 || '案件尚未运行分析诊断（BUILD/RESCAN 后生成），不显示「一切正常」',
    }
  }
  if ((sev.critical ?? 0) > 0) {
    return { tone: 'warn', title: '治理健康度：严重告警', detail: `critical ${sev.critical} · warning ${sev.warning ?? 0} · info ${sev.info ?? 0}` }
  }
  if ((sev.warning ?? 0) > 0) {
    return { tone: 'warn', title: '治理健康度：降级运行', detail: `warning ${sev.warning} · info ${sev.info ?? 0}；降级期间立案/导出禁用` }
  }
  return { tone: 'ok', title: '治理健康度：正常', detail: `诊断 ${total} 条（info ${sev.info ?? 0}），无 warning/critical` }
}
