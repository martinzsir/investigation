// 线索研判画布前后端共享契约（PRD V1.0.0 第五章；后端 server/app/canvas_seed.py）。
// M1：只读成图 + 自动保存底座；人工节点/连线矩阵在 M3 于本文件扩充；
// M4：RC-105 手册建议采纳/待核实生成、RC-204 白名单 Function 扩展查询。

import type { TaskRow } from './task'

export const NODE_KINDS = [
  'rule',
  'fact',
  'object',
  'source_row',
  'source_file',
  'verify_item',
  'evidence',
  'hypothesis',
  'note',
  'function_result',
] as const
export type NodeKind = (typeof NODE_KINDS)[number]

/** 节点业务名（降级列表分组/节点类型标签） */
export const KIND_LABELS: Record<NodeKind, string> = {
  rule: '规则',
  fact: '事实',
  object: '实体',
  source_row: '数据行',
  source_file: '数据源',
  verify_item: '待核实',
  evidence: '书证',
  hypothesis: '假设',
  note: '备注',
  function_result: '查询结果',
}

/** 降级列表分组顺序 */
export const KIND_ORDER: NodeKind[] = [
  'rule',
  'fact',
  'object',
  'source_row',
  'source_file',
  'verify_item',
  'evidence',
  'hypothesis',
  'note',
  'function_result',
]

/** 系统边关系（后端枚举一致） */
export const SYSTEM_RELS = [
  '命中',
  '涉及',
  '来源行',
  '所属文件',
  '手册建议',
  '挂接',
  '查询自',
] as const
export type SystemRel = (typeof SYSTEM_RELS)[number]

/** 人工关系 4 类（M3 连线矩阵启用） */
export const MANUAL_RELS = ['推断为', '证实', '查否', '补充说明'] as const
export type ManualRel = (typeof MANUAL_RELS)[number]

/** 人工节点只有两类 */
export const MANUAL_NODE_KINDS = ['hypothesis', 'note'] as const
export type ManualNodeKind = (typeof MANUAL_NODE_KINDS)[number]

/** 字段长度（与 server/app/canvas_edit.py 同口径，前端先拒、后端兜底） */
export const LIMITS = {
  hypothesisTitle: 50,
  content: 500,
  edgeNote: 200,
  snapshotLabel: 100,
} as const

/** 人工关系禁入目标（只能承载系统溯源边） */
const FORBIDDEN_EDGE_TARGETS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  'source_row',
  'source_file',
])

/** 推断为 来源 */
const INFER_SOURCES: ReadonlySet<NodeKind> = new Set<NodeKind>([
  'fact',
  'object',
])
/** 证实/查否 来源 */
const VERDICT_SOURCES: ReadonlySet<NodeKind> = new Set<NodeKind>([
  'verify_item',
  'evidence',
])

export type ConnectRejectCode =
  | 'bad_rel'
  | 'note_target'
  | 'self'
  | 'forbidden_target'
  | 'note_source_only'
  | 'matrix'

export interface ConnectResult {
  ok: boolean
  /** 拒绝时的业务原因文案（直接提示用户） */
  reason?: string
  code?: ConnectRejectCode
}

/**
 * RC-203 人工关系合法性矩阵（穷举纯函数，前后端各一份，单测以独立真相表
 * 全量对拍）：
 *  fact/object → hypothesis：推断为
 *  verify_item/evidence → hypothesis：证实/查否
 *  note → 任意节点：补充说明（方向固定；任意→note 拒绝）
 *  通用拒绝：自连；目标 source_row/source_file。
 */
export function canConnect(
  srcKind: NodeKind,
  tgtKind: NodeKind,
  rel: string,
): ConnectResult {
  if (!MANUAL_RELS.includes(rel as ManualRel)) {
    return { ok: false, code: 'bad_rel', reason: '非法人工关系类型' }
  }
  if (tgtKind === 'note') {
    return {
      ok: false,
      code: 'note_target',
      reason: '备注节点只能作为连线起点',
    }
  }
  if (srcKind === tgtKind) {
    return { ok: false, code: 'self', reason: '不能连接节点自身' }
  }
  if (FORBIDDEN_EDGE_TARGETS.has(tgtKind)) {
    return {
      ok: false,
      code: 'forbidden_target',
      reason: '数据行/数据源节点仅可由系统溯源连线关联',
    }
  }
  if (rel === '补充说明') {
    return srcKind === 'note'
      ? { ok: true }
      : {
          ok: false,
          code: 'note_source_only',
          reason: '「补充说明」只能由备注节点发起',
        }
  }
  if (tgtKind !== 'hypothesis') {
    return { ok: false, code: 'matrix', reason: '该两类节点不能建立该关系' }
  }
  if (rel === '推断为' && INFER_SOURCES.has(srcKind)) return { ok: true }
  if (
    (rel === '证实' || rel === '查否') &&
    VERDICT_SOURCES.has(srcKind)
  ) {
    return { ok: true }
  }
  return { ok: false, code: 'matrix', reason: '该两类节点不能建立该关系' }
}

/** 给定两端允许的人工关系（关系选择气泡只放白名单项） */
export function allowedRels(
  srcKind: NodeKind,
  tgtKind: NodeKind,
): ManualRel[] {
  return MANUAL_RELS.filter((rel) => canConnect(srcKind, tgtKind, rel).ok)
}

/** 同两端同关系是否已存在人工/系统边（重复边拒连） */
export function findEdge(
  doc: CanvasDoc,
  source: string,
  target: string,
  rel: string,
): CanvasEdge | undefined {
  return doc.edges.find(
    (e) => e.source === source && e.target === target && e.rel === rel,
  )
}

// ----------------------------------------------------------------------
// RC-202/206：表单校验（返回字段级错误；空对象=通过）
// ----------------------------------------------------------------------
export interface ManualNodeForm {
  title?: string
  content?: string
}

export type ManualNodeFormErrors = Partial<Record<'title' | 'content', string>>

export function validateManualNode(
  kind: ManualNodeKind,
  form: ManualNodeForm,
): ManualNodeFormErrors {
  const errs: ManualNodeFormErrors = {}
  const title = (form.title ?? '').trim()
  const content = (form.content ?? '').trim()
  if (kind === 'hypothesis') {
    if (!title) errs.title = '请填写假设标题'
    else if (title.length > LIMITS.hypothesisTitle) {
      errs.title = `假设标题不超过 ${LIMITS.hypothesisTitle} 字`
    }
    if (!content) errs.content = '请填写假设内容'
    else if (content.length > LIMITS.content) {
      errs.content = `假设内容不超过 ${LIMITS.content} 字`
    }
  } else {
    if (!content) errs.content = '请填写备注内容'
    else if (content.length > LIMITS.content) {
      errs.content = `备注内容不超过 ${LIMITS.content} 字`
    }
  }
  return errs
}

export function validateEdgeNote(note: string): string | null {
  const v = note.trim()
  if (v.length > LIMITS.edgeNote) return `备注不超过 ${LIMITS.edgeNote} 字`
  return null
}

export function validateSnapshotLabel(label: string): string | null {
  const v = label.trim()
  if (!v) return '请填写快照备注（不超过 100 字）'
  if (v.length > LIMITS.snapshotLabel) {
    return `快照备注不超过 ${LIMITS.snapshotLabel} 字`
  }
  return null
}

export interface CanvasNode {
  id: string
  kind: NodeKind
  /** 业务锚点（rule_id / row_uri / item_id / material_id / 人工 cn_xxx） */
  ref: string
  label: string
  system: boolean
  pinned: boolean
  x: number
  y: number
  /** verify_item 建议节点采纳态 */
  adopted?: boolean
  /** 快照回滚后引用失效 */
  stale?: boolean
  props?: Record<string, unknown>
  created_by?: string
  created_at?: string
  updated_at?: string
}

export interface CanvasEdge {
  id: string
  source: string
  target: string
  /** 系统边枚举或人工 4 类 */
  rel: string
  system: boolean
  note?: string
  created_by?: string
  created_at?: string
}

export interface CanvasDoc {
  nodes: CanvasNode[]
  edges: CanvasEdge[]
}

/** GET / PATCH .../canvas 响应 data */
export interface CanvasEnvelope {
  canvas_id: string
  clue_id: string
  doc: CanvasDoc
  version: number
  created_by?: string
  created_at?: string
  updated_by?: string
  updated_at?: string
  /** 本次 GET 是否触发了惰性 seed */
  seeded?: boolean
  /** 语义层是否已构建（false → 顶部横幅，M1 对象层跳过） */
  semantic_ready?: boolean
  /** 成图规模声明（后端截断保护；缺失=未截断） */
  meta?: CanvasMeta
}

/**
 * 画布成图规模声明。
 * 真实案件单条线索溯源行可达数千，全量成图会拖垮前端渲染——后端首屏只画
 * 前 N 条，并**如实声明 shown/total**（不静默少画，避免用户误以为数据只有这些）。
 * 用户点「展开更多」时调 expand-rows 补齐（只增不改删）。
 */
export interface CanvasMeta {
  truncated?: {
    source_row?: { shown: number; total: number }
    fact?: { shown: number; total: number }
  }
  limits?: { row_limit?: number; fact_limit?: number }
  hint?: string
}

/** 取溯源行截断信息（无 meta 视为未截断） */
export function rowTruncation(meta?: CanvasMeta): {
  shown: number
  total: number
  hidden: number
} | null {
  const t = meta?.truncated?.source_row
  if (!t || typeof t.total !== 'number') return null
  const shown = typeof t.shown === 'number' ? t.shown : 0
  const hidden = Math.max(0, t.total - shown)
  return hidden > 0 ? { shown, total: t.total, hidden } : null
}

// ======================================================================
// RC-103/104：逐层溯源 expand 契约（server/app/routers/canvas.py）
// ======================================================================
export type ExpandDirection = 'source' | 'neighbors' | 'all'

/** 前端展开四态机（PRD RC-103；展开态不持久化，仅内存） */
export type ExpandUiState = 'collapsed' | 'expanding' | 'expanded' | 'expand_failed'

/** 可展开的节点类型（M4：function_result 返回查询源与入参快照，无下一层语义节点） */
export const EXPANDABLE_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  'fact',
  'object',
  'source_row',
  'source_file',
  'function_result',
])

/** 后端 notices 业务码 → 前端文案在组件内映射 */
export type ExpandNotice =
  | 'semantic_unavailable'
  | 'leaf'
  | 'node_not_found'
  | 'unsupported_kind'
  | 'object_gone'

export interface RowFieldDto {
  name: string
  raw?: string
  value: string
  hit?: boolean
  mask?: 'phone' | 'idcard' | 'text'
  policy?: 'visible' | 'masked' | 'denied'
  denied_hint?: string
}

/** 抽屉内 source_row 负载（在 SourceRowDto 之上带 expand 状态标记） */
export interface RowDetailDto {
  row_uri: string
  source?: string
  fields: RowFieldDto[]
  /** 归档行取回成功；false=归档缺失（仅 locator/URI 原文） */
  archived?: boolean
  /** 归档行缺失标记（抽屉红条 + URI） */
  missing?: boolean
  /** 表级汇总（无字段明细） */
  granularity?: string
  from_clue?: boolean
}

export interface SourceFileCard {
  upload_id: string
  filename: string
  format: string
  rows?: number | null
  uploaded_by: string
  uploaded_at: string
  dataset: string
}

export interface FileDetailDto {
  registered: boolean
  dataset: string
  file?: SourceFileCard
}

/** M4 RC-204：function_result 展开负载（查询源 + 入参/输入表快照） */
export interface FunctionResultDetail {
  kind: 'function_result'
  function: string
  function_title: string
  params: Record<string, unknown>
  executed_at: string
  executed_by: string
  output_type: string
  summary: FunctionResultSummary
  /** 「查询自」源节点（点击后继续 RC-103 四层溯源） */
  source_node_ids: string[]
  /** Function 声明消费的语义表 */
  input_tables: string[]
}

export type NodeDetail = RowDetailDto | FileDetailDto | FunctionResultDetail

/** POST .../canvas/expand 响应 data */
export interface ExpandEnvelope extends CanvasEnvelope {
  added_nodes: string[]
  added_edges: string[]
  /** key=节点 id：source_row → RowDetailDto；source_file → FileDetailDto */
  details: Record<string, NodeDetail>
  notices: ExpandNotice[]
  truncated: boolean
  leaf: boolean
}

// ======================================================================
// RC-102：规则审计视图（只读；业务视图不出现任何技术标识）
// ======================================================================
export interface RuleAudit {
  rule_id: string
  title: string
  stage: string
  dimension: string
  function: string
  params: Record<string, unknown>
  hit_when: string
  jian_types: string[]
  assumption: string
  rule_text: string
  basis_text: string
  ontology_version: string
  pack_id: string
  rule_workshop_href: string
}

// ======================================================================
// M3（RC-202/203/206）：人工节点/边/快照端点契约
// ======================================================================
export interface CanvasSnapshot {
  snapshot_id: string
  clue_id: string
  label: string
  /** manual | report */
  origin: 'manual' | 'report' | string
  report_id?: string
  created_by: string
  created_at: string
  node_count: number
  edge_count: number
  doc: CanvasDoc
}

export interface SnapshotListEnvelope {
  snapshots: CanvasSnapshot[]
}

export interface SnapshotCreateEnvelope extends CanvasEnvelope {
  snapshot: CanvasSnapshot
}

export interface RollbackEnvelope extends CanvasEnvelope {
  target_snapshot_id: string
  recovery_snapshot_id: string
  stale_node_ids: string[]
  recovery_snapshot: CanvasSnapshot
}

/** 结构变更端点（nodes/edges）统一回包：整文档 + 本次实体 */
export interface NodeMutateEnvelope extends CanvasEnvelope {
  node: CanvasNode
}
export interface NodeDeleteEnvelope extends CanvasEnvelope {
  removed_node: string
  removed_edges: string[]
}
export interface EdgeMutateEnvelope extends CanvasEnvelope {
  edge: CanvasEdge
}

export function isSystemNode(n: CanvasNode): boolean {
  return n.system === true
}

export function isManualNode(
  n: Partial<CanvasNode> & { kind: CanvasNode['kind'] },
): boolean {
  return n.system !== true && MANUAL_NODE_KINDS.includes(n.kind as ManualNodeKind)
}

/** 节点关联的人工边（删除确认文案/抽屉连线管理） */
export function incidentManualEdges(
  doc: CanvasDoc,
  nodeId: string,
): CanvasEdge[] {
  return doc.edges.filter(
    (e) =>
      e.system !== true && (e.source === nodeId || e.target === nodeId),
  )
}

export function isExpandableKind(kind: NodeKind): boolean {
  return EXPANDABLE_KINDS.has(kind)
}

/** 节点默认展开方向（对象给全部，其余只沿溯源方向） */
export function defaultDirection(kind: NodeKind): ExpandDirection {
  return kind === 'object' ? 'all' : 'source'
}

// ======================================================================
// M4 RC-105：手册建议（pb: 虚节点）采纳与待核实生成
// （后端 server/app/canvas_infer.py + routers/canvas.py）
// ======================================================================
/** RC-105 手册建议采纳编排态（ResearchCanvas 持有，失败必须可见可重试） */
export interface AdoptUiState {
  status: 'idle' | 'running' | 'failed'
  message?: string
}

/** 未采纳手册建议节点的 ref 前缀（verify_item:pb:<playbook_id> / ref="pb:<id>"） */
export const PB_REF_PREFIX = 'pb:'

export function isPlaybookRef(ref: unknown): ref is string {
  return typeof ref === 'string' && ref.startsWith(PB_REF_PREFIX)
}

/** 未采纳的手册建议节点（虚线态；不经 state、不进核查工作台） */
export function isSuggestionNode(n: CanvasNode): boolean {
  return (
    n.kind === 'verify_item' &&
    n.adopted !== true &&
    isPlaybookRef(n.ref)
  )
}

// ======================================================================
// 人机来源标记（P1-②）：画布须能区分「机器说的」和「人确认过的」
// ======================================================================
// 正兵在画布上做可信度判断，第一件事是知道这条内容是谁给的：
// 机器自动派生？AI 建议还没人看？系统建议已被人确认？还是人自己加的？
// 四态互斥，判定顺序不可调换（manual 最优先，adopted 只在系统节点上成立）。
export type Provenance = 'system' | 'suggestion' | 'adopted' | 'manual'

export const PROVENANCE_LABELS: Record<Provenance, string> = {
  system: '机器派生',
  suggestion: 'AI 建议',
  adopted: '人工已采纳',
  manual: '人工新增',
}

/** PROVENANCE 语义说明（图例/悬浮提示用，讲清每一态意味着什么） */
export const PROVENANCE_HINTS: Record<Provenance, string> = {
  system: '规则/事实/数据行等自动成图，未经人工表态',
  suggestion: '手册或 AI 给出的建议，尚未采纳（虚线）',
  adopted: '系统建议经正兵确认采纳，已进核查工作台',
  manual: '正兵手工新增的判断（假设/备注）',
}

/**
 * 节点来源判定。
 * - manual    : system=false → 人工新建（M3 端点产生）
 * - suggestion: 手册/AI 建议且未采纳
 * - adopted   : 系统节点但 adopted=true（建议经人确认）
 * - system    : 其余自动派生
 */
export function provenanceOf(n: CanvasNode): Provenance {
  if (n.system !== true) return 'manual'
  if (isSuggestionNode(n)) return 'suggestion'
  if (n.adopted === true) return 'adopted'
  return 'system'
}

/**
 * 边来源判定（两态即可：系统溯源边 / 人工连线）。
 * 边没有 adopted 概念——人工连线本身就是「人建的」。
 */
export function provenanceOfEdge(e: CanvasEdge): 'system' | 'manual' {
  return e.system === false ? 'manual' : 'system'
}

/** POST .../canvas/suggestions 响应 data */
export interface SuggestionEnvelope extends CanvasEnvelope {
  added_nodes: CanvasNode[]
  added_edges: CanvasEdge[]
  skipped: Array<{ playbook_id: string; reason: string }>
}

export type AdoptMode = 'transition' | 'add_manual' | 'adopted'

/** POST .../canvas/suggestions/{nodeId}/adopt（202 已入队 / 200 state 已采纳） */
export interface AdoptEnvelope {
  /** mode=adopted 时为 null（无需任务，直接 sync 即一致） */
  task: TaskRow | null
  mode: AdoptMode
  node_id: string
  item_id?: string
  effective_text: string
}

export type SuggestionSyncStatus =
  | 'adopted'
  | 'created'
  | 'exists'
  | 'pending'
  | 'missing'

export interface SuggestionSyncResult {
  node_id: string
  status: SuggestionSyncStatus
  item_id?: string
  new_node_id?: string
}

/** POST .../canvas/suggestions/sync 响应 data */
export interface SuggestionSyncEnvelope extends CanvasEnvelope {
  results: SuggestionSyncResult[]
}

/** POST .../canvas/nodes/{nodeId}/to-verify（202 add_manual 已入队） */
export interface ToVerifyEnvelope {
  task: TaskRow
  mode: 'add_manual'
  node_id: string
  effective_text: string
}

// ======================================================================
// M4 RC-204：白名单只读 Function 扩展查询（function_result 节点）
// ======================================================================
export interface FunctionParamForm {
  key: string
  label: string
  /** values.json 同口径：integer/decimal/string/boolean/date */
  type: string
  /** string 仅 enum 白名单（null=该参数不提供枚举选择，按后端口径此类参数不放行） */
  enum: string[] | null
  default: unknown
  required: boolean
}

export interface FunctionForm {
  name: string
  title: string
  description: string
  output_type: string
  params: FunctionParamForm[]
}

/** GET .../canvas/functions 响应 data */
export interface FunctionCatalogEnvelope {
  functions: FunctionForm[]
  /** 案件包装载失败时 false（目录为空，不 500） */
  available: boolean
}

/** 节点 props / expand detail 中的 Function 输出摘要（前 20 行预览） */
export interface FunctionResultSummary {
  kind: 'rows' | 'report' | 'empty' | string
  row_count?: number
  columns?: string[]
  preview_rows?: Array<Record<string, unknown>>
  meta?: Record<string, unknown>
  report?: unknown
}

/** POST .../canvas/function-query 成功：落节点 + 「查询自」边 */
export interface FunctionQuerySuccessEnvelope extends CanvasEnvelope {
  executed: true
  node: CanvasNode
  edge: CanvasEdge | null
  result: FunctionResultSummary
  params: Record<string, unknown>
}

/** 数据源未接入/执行降级：200 但 executed=false，不产生节点 */
export interface FunctionQuerySkippedEnvelope {
  executed: false
  code: 'DATASOURCE_UNAVAILABLE' | 'DEGRADED' | string
  message: string
  reason?: string
  function: string
  params: Record<string, unknown>
}

export type FunctionQueryEnvelope =
  | FunctionQuerySuccessEnvelope
  | FunctionQuerySkippedEnvelope

/**
 * RC-204 扩展查询表单前端校验（后端 merge_query_params 兜底；前端先拒）：
 * 必填缺失、integer/decimal 非有限数、enum 外值。返回字段级错误（空对象=通过）。
 */
export function validateFunctionParams(
  form: FunctionForm,
  values: Record<string, unknown>,
): Record<string, string> {
  const errs: Record<string, string> = {}
  for (const p of form.params) {
    const v = values[p.key]
    const empty =
      v === null || v === undefined || (typeof v === 'string' && v.trim() === '')
    if (empty) {
      if (p.required) errs[p.key] = '该参数必填'
      continue
    }
    if (p.type === 'integer') {
      if (typeof v !== 'number' || !Number.isInteger(v)) {
        errs[p.key] = '请填写整数'
      }
    } else if (p.type === 'decimal' || p.type === 'number') {
      if (typeof v !== 'number' || !Number.isFinite(v)) {
        errs[p.key] = '请填写数值'
      }
    } else if (p.type === 'boolean') {
      if (typeof v !== 'boolean') errs[p.key] = '请选择是/否'
    } else if (p.type === 'string') {
      if (p.enum !== null && !p.enum.includes(String(v))) {
        errs[p.key] = '取值不在允许范围内'
      } else if (p.enum === null) {
        // 与后端同口径：string 无 enum 白名单不放行（防自由文本注入）
        errs[p.key] = '该参数不支持自由文本'
      }
    }
  }
  return errs
}

// ----------------------------------------------------------------------
// M5 RC-301：画布只读问答（RC-302 引用校验串联）
// ----------------------------------------------------------------------
export interface CanvasChatFact {
  sentence: string
  citations: string[]
}

/**
 * 工具确定性产物（ReAct 模式）。
 * 由只读工具（当前仅 report.render）产出，服务端以产物原文作为答复，
 * 绕过逐句引用分流；正文完整性由确定性管道保证。
 */
export interface CanvasChatArtifact {
  artifact_type: 'sunzi-report' | string
  tool: string
  format: string
  report_type: string
  type_name: string
  text: string
  degraded: boolean
}

export interface CanvasChatEnvelope {
  answer: string
  facts: CanvasChatFact[]
  pending: string[]
  warnings: string[]
  citations: string[]
  model: string | null
  /** 非 null 时 answer 即工具确定性产物原文（如十段式研判报告 md） */
  artifact?: CanvasChatArtifact | null
}

/** RC-303：问答建议提交为提案的响应 */
export interface CanvasSuggestionEnvelope {
  proposal_id: string
  status: string
  message: string
}

// ------------------------------------------------------------------
// M6 RC-304/305/306：研判报告（生成/列表/详情/导出）
// ------------------------------------------------------------------

/** 报告生成 202 响应 */
export interface ReportGenerateEnvelope {
  report_id: string
  version_no: number
  snapshot_id: string
  task_id: string
  status: string
}

/** 报告列表项 */
export interface ReportListItem {
  report_id: string
  clue_id: string
  version_no: number
  snapshot_id: string
  status: string
  model: string
  error: string
  task_id: string
  extra_request: string
  warning_count: number
  citation_count: number
  created_by: string
  created_at: string
}

/** 报告列表响应 */
export interface ReportListEnvelope {
  reports: ReportListItem[]
}

/** 报告详情 */
export interface ReportDetail {
  report_id: string
  clue_id: string
  version_no: number
  snapshot_id: string
  status: string
  model: string
  error: string
  task_id: string
  extra_request: string
  content_md: string
  sections: Record<string, string>
  citations: Array<{ cite_id: number; ref: string; summary: string }>
  warnings: string[]
  created_by: string
  created_at: string
}

/** 报告详情响应 */
export interface ReportDetailEnvelope {
  report: ReportDetail
}


