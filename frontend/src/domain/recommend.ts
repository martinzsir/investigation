// MVP-3 接入建议确认域（FE-P-008，server de-recommendations 契约）。
// 红线：推荐为「待核实草案，非生效声明」；采纳/驳回只记 state + 审计，永不自动改 bindings。

/** 单条数据元推荐（四要素：列 → 数据元） */
export interface DeRecommendationItem {
  /** 上传件列名 */
  col: string
  element_id: string
  element_name: string | null
  confidence: number
  evidence?: { match_values?: string[] }
}

/** 推荐单（一个上传件一份） */
export interface DeReco {
  rid: string
  upload_id: string
  /** 后端中文字面量：待核实 / 采纳 / 驳回 */
  status: string
  created_at: string
  created_by: string
  decided_by: string
  decided_at: string
  note: string
  recommendations: DeRecommendationItem[]
  filename?: string | null
}

/** 后端裁决决策 */
export type DeDecision = 'adopt' | 'reject'

/** 状态归一（后端中文 → 枚举） */
export type DeRecoStatus = 'pending' | 'adopted' | 'rejected'

const STATUS_MAP: Record<string, DeRecoStatus> = {
  待核实: 'pending',
  采纳: 'adopted',
  驳回: 'rejected',
}

export function deRecoStatus(status: string): DeRecoStatus {
  return STATUS_MAP[status] ?? 'pending'
}

export const DE_STATUS_META: Record<DeRecoStatus, { label: string; tone: 'warn' | 'ok' | 'muted' }> = {
  pending: { label: '待核实', tone: 'warn' },
  adopted: { label: '已采纳', tone: 'ok' },
  rejected: { label: '已驳回', tone: 'muted' },
}

/** 顶部红条文案（红线：草案非生效声明） */
export const DE_DRAFT_NOTICE = '待核实草案，非生效声明——采纳/驳回仅记录裁决，不会自动改写数据元配置'

/** 待核实（可裁决） */
export function isDecidable(r: DeReco): boolean {
  return deRecoStatus(r.status) === 'pending'
}

/** 无建议时说明（数据质量良好） */
export function hasRecommendations(r: DeReco): boolean {
  return Array.isArray(r.recommendations) && r.recommendations.length > 0
}

/** 置信度色阶（与画像一致：≥.85 ok / .6-.84 warn / <.6 error） */
export function deConfidenceTone(v: number): 'ok' | 'warn' | 'error' {
  if (v >= 0.85) return 'ok'
  if (v >= 0.6) return 'warn'
  return 'error'
}
