// 线索/处置领域逻辑：红线判定的唯一事实源（FE-C-013~015、FE-T-003~006/010/015/021）。
// 组件只做薄编排，所有门禁/升格/遮蔽/空链判定一律走本模块纯函数，确定性可测。
// 与 core/registry.py ClueStatus、core/functions.py jian_cross_level、
// core/access.py ROLE_RANK、core/audit.py 空链红线语义对齐。

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

export const STATUS_META: Record<ClueStatus, StatusMeta> = {
  待查: { bg: 'rgba(107,131,153,.15)', border: '#6B8399', text: '#8FB3CC', icon: '○' },
  查证中: { bg: 'rgba(0,212,255,.15)', border: '#00D4FF', text: '#00D4FF', icon: '◐' },
  已固证: { bg: 'rgba(46,212,122,.15)', border: '#2ED47A', text: '#2ED47A', icon: '●' },
  已排除: { bg: 'rgba(107,131,153,.08)', border: '#3D5668', text: '#5A7A94', icon: '✕' },
  已立案: { bg: 'rgba(196,30,58,.25)', border: '#D4AF37', text: '#FFD87A', icon: '★', terminal: true },
}

// ---------- 处置动作（server LEGAL_ACTIONS = verify/reset/exclude/confirm/file） ----------

export type ClueAction = 'verify' | 'reset' | 'exclude' | 'confirm' | 'file'

export const ACTION_LABEL: Record<ClueAction, string> = {
  verify: '开始查证',
  reset: '退回待查',
  exclude: '排除线索',
  confirm: '固证',
  file: '立案',
}

/** 状态机（core/registry.py ClueStatusMachine 同构）：当前态 → 可执行动作 */
const TRANSITIONS: Record<ClueStatus, ClueAction[]> = {
  待查: ['verify', 'exclude'],
  查证中: ['confirm', 'exclude', 'reset'],
  已固证: ['file', 'exclude'],
  已排除: ['reset'],
  已立案: [], // 终态
}

export function allowedActions(status: ClueStatus): ClueAction[] {
  return TRANSITIONS[status] ?? []
}

/** 动作目标态（仅用于前端确认文案；真值以服务端/审计链为准） */
export const ACTION_TARGET: Record<ClueAction, ClueStatus> = {
  verify: CLUE_STATUS.VERIFYING,
  reset: CLUE_STATUS.PENDING,
  exclude: CLUE_STATUS.EXCLUDED,
  confirm: CLUE_STATUS.CONFIRMED,
  file: CLUE_STATUS.FILED,
}

// ---------- 五间（数据通道：资金/通讯/行为/关系/时间，FE-D-007 色板） ----------

export const JIAN_ROOMS = ['资金', '通讯', '行为', '关系', '时间'] as const
export type JianRoom = (typeof JIAN_ROOMS)[number]

export const JIAN_ROOM_VAR: Record<JianRoom, string> = {
  资金: 'var(--sun-jian-fund)',
  通讯: 'var(--sun-jian-comms)',
  行为: 'var(--sun-jian-behavior)',
  关系: 'var(--sun-jian-relation)',
  时间: 'var(--sun-jian-time)',
}

/**
 * 五间交叉等级（core/functions.py jian_cross_level 同构）：
 * 单源=观察 → 双源=线索 → 三源=可立案依据候选。
 * 红线 FE-T-006：单源候选必须为「观察」。
 * n = 独立通道命中数（LLM 同源多次判读只计一个间，FE-T-007 在 MVP-2 落地）。
 */
export type CrossLevel = '观察' | '线索' | '可立案依据候选'

export function crossLevel(roomCount: number): CrossLevel {
  if (roomCount >= 3) return '可立案依据候选'
  if (roomCount === 2) return '线索'
  return '观察'
}

/** 异常通道线索恒「待核实」，绝不参与交叉升格（core/anomaly_channel.py 红线） */
export const ANOMALY_LEVEL = '待核实'

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
  /** 渲染但不可提交（置灰 + 写明原因） */
  enabled: boolean
  reason: string
}

/**
 * @param degraded 降级态（FE-C-027/FE-T-010）：降级时立案/导出禁用且写明原因
 */
export function fileGate(
  role: string,
  status: ClueStatus,
  legalBasis: string,
  degraded: boolean,
): FileGate {
  if (!canFileRole(role)) {
    return { render: false, enabled: false, reason: '' }
  }
  if (degraded) {
    return { render: true, enabled: false, reason: '系统降级运行中：立案/导出已临时禁用（见顶部降级通栏）' }
  }
  if (status !== CLUE_STATUS.CONFIRMED) {
    return { render: true, enabled: false, reason: '需先完成固证（仅「已固证」线索可立案）' }
  }
  if (!legalBasis.trim()) {
    return { render: true, enabled: false, reason: '请填写法定依据/案号' }
  }
  return { render: true, enabled: true, reason: '' }
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
