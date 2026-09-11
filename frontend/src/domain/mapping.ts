// MVP-3 接入向导列映射域（server/app/source_analyze.py 契约）。
// 纯函数：匹配色阶、低置信归类、缺必选列降级警告（非阻断）、映射方向转换、指纹重复。

export type MatchType = 'exact' | 'normalized' | 'fuzzy' | 'none'

/** 单声明列 ↔ 上传列匹配（analyze suggestion.matches[]） */
export interface ColumnMatch {
  source_col: string | null
  target_prop: string
  match_type: MatchType
  confidence: number
}

/** analyze 建议（向导核心） */
export interface MappingSuggestion {
  target_table: string | null
  confidence: number
  matches: ColumnMatch[]
  /** 必选但无匹配的声明列 → 降级警告（非阻断） */
  missing_required: string[]
  /** 0 < 置信度 < 0.70 的声明列 → 琥珀高亮 */
  low_confidence: string[]
}

/** 声明目标表（analyze declared_tables[]） */
export interface DeclaredTable {
  name: string
  title?: string
  object?: string
  required_columns: string[]
  optional_columns: string[]
}

/** 上传列画像（analyze columns[]） */
export interface SourceColumn {
  name: string
  inferred_type?: string
  null_rate?: number
  distinct?: number
  samples?: string[]
}

/**
 * 数据元推荐（analyze element_hints[]，v1.1 从 Step4 前移到 Step1 同步返回）。
 * col 为上传源列名；与映射表（按声明列展示）正交，需按“该声明列映射到的源列”查 hint。
 */
export interface ElementHint {
  /** 上传源列名（hint 按 source col 索引，非声明列） */
  col: string
  element_id: string
  element_name?: string
  /** 0–1 置信度 */
  confidence: number
  evidence?: { match_values?: string[] }
  /** 数据元声明的清洗 op 链（P4 三分类用） */
  clean_rule?: string[]
  /** 数据元 format 正则（可空） */
  format?: string
}

/** analyze 端点返回 */
export interface AnalyzeResult {
  upload_id: string
  filename: string
  format: string
  row_count: number
  sha256: string
  fingerprint: string
  columns: SourceColumn[]
  declared_tables: DeclaredTable[]
  suggestion: MappingSuggestion
  element_hints?: ElementHint[]
}

/** 后端低置信阈值（source_analyze.LOW_CONFIDENCE） */
export const LOW_CONFIDENCE = 0.7

export interface MatchMeta {
  label: string
  tone: 'ok' | 'info' | 'warn' | 'muted'
}

export const MATCH_META: Record<MatchType, MatchMeta> = {
  exact: { label: '精确匹配', tone: 'ok' },
  normalized: { label: '归一匹配', tone: 'info' },
  fuzzy: { label: '模糊匹配', tone: 'warn' },
  none: { label: '未匹配', tone: 'muted' },
}

export function matchTone(m: ColumnMatch): MatchMeta['tone'] {
  if (m.match_type === 'exact') return 'ok'
  if (m.match_type === 'normalized') return 'info'
  if (m.match_type === 'fuzzy' || (m.confidence > 0 && m.confidence < LOW_CONFIDENCE)) return 'warn'
  return 'muted'
}

export function isLowConfidence(m: ColumnMatch): boolean {
  return m.match_type === 'fuzzy' || (m.confidence > 0 && m.confidence < LOW_CONFIDENCE)
}

export function isMatched(m: ColumnMatch): boolean {
  return m.source_col !== null && m.source_col !== ''
}

/**
 * 缺必选列 = 降级警告，**非阻断**（FE-P-002 验收）。
 * 向导显黄色警告但允许继续导入（缺失列在清洗阶段按 null 策略处理）。
 */
export function missingRequiredColumns(s: MappingSuggestion): string[] {
  return s.missing_required ?? []
}

/**
 * 从建议 matches 构建向导视角映射 {声明列: 上传列}（仅已匹配项）。
 * 未匹配（source_col=null）不进入映射。
 */
export function buildWizardMapping(s: MappingSuggestion): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of s.matches) {
    if (isMatched(m)) out[m.target_prop] = m.source_col as string
  }
  return out
}

/**
 * 方向转换：向导 {声明列: 源列} → 导入 column_map {源列: 声明列}（rename 视角）。
 * 后端 import 体 column_map 为 {源列: 声明列}；PUT 保存 mapping 为 {声明列: 源列}。
 * 空值忽略。
 */
export function toImportColumnMap(wizardMapping: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [decl, src] of Object.entries(wizardMapping)) {
    if (decl && src) out[src] = decl
  }
  return out
}

/** 向导映射 → 后端 PUT 保存体（同向导视角，直传） */
export function toSaveMapping(
  targetTable: string,
  wizardMapping: Record<string, string>,
  notes?: string,
): { target_table: string; mapping: Record<string, string>; notes?: string } {
  return { target_table: targetTable, mapping: wizardMapping, notes }
}

/** 已映射列数（不含未匹配） */
export function mappedCount(wizardMapping: Record<string, string>): number {
  return Object.values(wizardMapping).filter((v) => v && v.trim() !== '').length
}

/** 指纹重复（409 CONFLICT）提示文案 —— 不重复入队（FE-I-007） */
export function duplicateSourceMessage(detail?: string): string {
  const base = '文件已存在（文件名 + 内容 SHA-256 + 行数 指纹一致），将跳过，不重复导入'
  return detail ? `${base}：${detail}` : base
}

/** 上传/导入异常态区分（FE-P-002） */
export type IngestErrorKind = 'format' | 'mapping' | 'permission' | 'conflict' | 'other'

export function classifyIngestError(code: string, message: string): IngestErrorKind {
  if (code === 'CONFLICT') return 'conflict'
  if (code === 'FORBIDDEN') return 'permission'
  if (code === 'VALIDATION') {
    if (/不支持的文件类型|文件解析失败|上传文件为空|格式/.test(message)) return 'format'
    if (/映射|声明列|源列|目标/.test(message)) return 'mapping'
    return 'other'
  }
  return 'other'
}
