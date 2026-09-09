import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// ETL 管道 + 映射校验 + 缺列降级（server/app/routers/etl.py + data_governance.py 契约）。
// GET/PUT /cases/{cid}/etl-pipeline          🔒 bindings.json 派生视图/回写可编辑字段
// POST   /cases/{cid}/etl-pipeline/validate     映射预检（不写盘；无 force/ignore/continue）
// GET    /cases/{cid}/governance/missing-columns 缺列/CAST 失败降级聚合（无分页，组数小）

/** on_cast_error 状态枚举（core ontology_loader） */
export const CAST_ERROR_POLICIES = ['fail', 'quarantine'] as const
/** null_policy 状态枚举 */
export const NULL_POLICIES = ['allow', 'reject', 'quarantine'] as const

export interface EtlSource {
  source_table: string | null
  object: string
  clean: string[]
  /** 属性 → fail | quarantine（缺省=降级 NULL） */
  on_cast_error: Record<string, string>
  /** 属性 → allow | reject | quarantine */
  null_policy: Record<string, string>
  dedup_key: string[]
  dedup_on_conflict: string
  /** 复合列无声明位置，恒为 []（诊断+出路指引，不可写） */
  composite_props: unknown[]
}

export interface EtlPipelineDoc {
  sources: EtlSource[]
}

export interface EtlSaveBody {
  sources: Array<Partial<EtlSource> & { object: string }>
  /** 变更理由（FE-T-012，落审计链 note） */
  reason?: string
}

export type ValidateConflictType = 'one_to_one' | 'unknown_prop' | 'missing_column'

export interface ValidateConflict {
  type: ValidateConflictType
  message: string
  source_col?: string
  target_a?: string
  target_b?: string
  target_prop?: string
}

export interface ValidatePath {
  key: 'A_split_source_sql' | 'B_degrade_column'
  label: string
}

export interface ValidateResult {
  valid: boolean
  conflicts: ValidateConflict[]
  paths: ValidatePath[]
}

export interface MissingColumnItem {
  object: string
  property: string
  count: number
  /** 诊断种类：source_column_missing / source_value_cast_failed */
  kinds: string[]
  samples: string[]
}

export interface MissingColumnsResult {
  items: MissingColumnItem[]
  total_warnings: number
}

export const etlApi = {
  /** GET /cases/{cid}/etl-pipeline */
  async getPipeline(caseId: string): Promise<EtlPipelineDoc> {
    const res = await api.get<EtlPipelineDoc>(
      `/cases/${encodeURIComponent(caseId)}/etl-pipeline`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/etl-pipeline 🔒 回写 clean/on_cast_error/null_policy/dedup */
  async savePipeline(caseId: string, body: EtlSaveBody): Promise<EtlPipelineDoc> {
    const res = await api.put<EtlPipelineDoc>(
      `/cases/${encodeURIComponent(caseId)}/etl-pipeline`,
      body,
      { idempotencyAction: 'etl-pipeline-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/etl-pipeline/validate 映射预检（红线四：响应无 force/ignore） */
  async validateMapping(caseId: string, targetTable: string, mapping: Record<string, string>): Promise<ValidateResult> {
    const res = await api.post<ValidateResult>(
      `/cases/${encodeURIComponent(caseId)}/etl-pipeline/validate`,
      { target_table: targetTable, mapping },
    )
    return res.data
  },

  /** GET /cases/{cid}/governance/missing-columns 缺列/CAST 失败降级（客户端分组渲染） */
  async missingColumns(caseId: string): Promise<MissingColumnsResult> {
    const res = await api.get<MissingColumnsResult>(
      `/cases/${encodeURIComponent(caseId)}/governance/missing-columns`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
