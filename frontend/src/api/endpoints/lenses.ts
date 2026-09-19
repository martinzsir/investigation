import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { TaskRow } from '../../domain/task'

// 案件级镜头启停 + 定向调度（server/app/routers/lenses.py 契约）。
// GET  /cases/{cid}/lenses                     镜头清单 + 案件生效状态（启停面板数据源）
// PUT  /cases/{cid}/lenses/{skill_id} 🔒       启停开关（落 lenses.json，入队 RESCAN）
// POST /cases/{cid}/lenses/{skill_id}/run 🔒   定向镜头带参调度（202 入队 LENS_RUN，
//                                              线索落 lens_runs 补充产物并线进线索列表）

/** 镜头参数声明（params_schema 条目；type 五值同 SkillSpec.PARAM_TYPES） */
export interface LensParamSpec {
  type: 'string' | 'integer' | 'decimal' | 'date' | 'boolean'
  required?: boolean
  description?: string
  [k: string]: unknown
}

/** 镜头清单条目（内置五技能不列；pack_id="_builtin"） */
export interface LensSpecItem {
  skill_id: string
  name: string
  stage: string
  mode: 'deterministic' | 'draft' | string
  pack_id: string
  pack_enabled: boolean
  /** 案件覆盖值；null=未覆盖（生效值回落包声明） */
  case_override: boolean | null
  /** 生效值：案件覆盖优先，未覆盖回落包声明 */
  enabled: boolean
  /** 有必填参数的定向镜头（批量检测跳过，画布/面板定向带参调度） */
  requires_params: boolean
  /** 参数声明（定向调度弹窗表单数据源） */
  params_schema: Record<string, LensParamSpec>
}

export interface LensListResult {
  lenses: LensSpecItem[]
  overrides_file: string
}

export interface LensSwitchBody {
  enabled: boolean
  /** 变更理由（落审计链 note） */
  reason?: string
}

export interface LensSwitchResult {
  skill_id: string
  enabled: boolean
  /** 启停影响批量检测结果，自动入队 RESCAN */
  rescan_task: TaskRow
}

export interface LensRunBody {
  params?: Record<string, unknown>
  reason?: string
}

export interface LensRunResult {
  skill_id: string
  task: TaskRow
}

/** 定向调度参数前端校验：必填非空 + 数值/整数类型；返回 {参数名: 错误} */
export function validateLensParams(
  lens: LensSpecItem,
  values: Record<string, unknown>,
): Record<string, string> {
  const errs: Record<string, string> = {}
  for (const [key, spec] of Object.entries(lens.params_schema)) {
    const v = values[key]
    const empty =
      v === null || v === undefined || (typeof v === 'string' && v.trim() === '')
    if (spec.required && empty) {
      errs[key] = '必填参数'
      continue
    }
    if (empty) continue
    if (spec.type === 'integer' || spec.type === 'decimal') {
      if (typeof v !== 'number' || Number.isNaN(v)) {
        errs[key] = '须为数值'
      } else if (spec.type === 'integer' && !Number.isInteger(v)) {
        errs[key] = '须为整数'
      }
    } else if (spec.type === 'date' && typeof v === 'string' && v.trim() !== ''
      && !/^\d{4}-\d{2}-\d{2}$/.test(v.trim())) {
      errs[key] = '格式须为 YYYY-MM-DD'
    }
  }
  return errs
}

export const lensesApi = {
  /** GET /cases/{cid}/lenses —— 镜头清单 + 案件生效状态 */
  async list(caseId: string): Promise<LensListResult> {
    const res = await api.get<LensListResult>(
      `/cases/${encodeURIComponent(caseId)}/lenses`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/lenses/{skillId} 🔒 偏将及以上；内置技能 400 */
  async switch(
    caseId: string,
    skillId: string,
    body: LensSwitchBody,
  ): Promise<LensSwitchResult> {
    const res = await api.put<LensSwitchResult>(
      `/cases/${encodeURIComponent(caseId)}/lenses/${encodeURIComponent(skillId)}`,
      body,
      { idempotencyAction: `lens-switch:${skillId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/lenses/{skillId}/run 🔒 定向调度（202；参数预检 400 不入队） */
  async run(
    caseId: string,
    skillId: string,
    body: LensRunBody,
  ): Promise<LensRunResult> {
    const res = await api.post<LensRunResult>(
      `/cases/${encodeURIComponent(caseId)}/lenses/${encodeURIComponent(skillId)}/run`,
      body,
      { idempotencyAction: `lens-run:${skillId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
