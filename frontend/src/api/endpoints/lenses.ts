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
  /**
   * 画布可用（与 enabled **分离**）：只影响正兵能否在画布手动带参跑。
   * 案件未覆盖 → 回落包级 enabled（包被吊销则画布也不可用）。
   */
  canvas_enabled: boolean
  /** 有必填参数的定向镜头（批量检测跳过，画布/面板定向带参调度） */
  requires_params: boolean
  /** 数据就绪度（前置提示：缺哪些数据 → 跑了也会降级；null=未能判定） */
  readiness?: LensReadiness | null
  /** 参数声明（定向调度弹窗表单数据源） */
  params_schema: Record<string, LensParamSpec>
  /** 用途说明（pack.json 声明；空则由 UI 按维度/依赖兜底描述） */
  description?: string
  /** 产出维度（维度 code，如 relation/time；停用即丢失这些维度的线索） */
  produces_dims?: string[]
  /**
   * 产出维度的中文名。
   * 来源是本体的 **dimensions.json 的 name**（不是 objects/links 的 title——
   * 维度不在对象/链接层）。后端按 code→name 翻译，兼容存量包写中文 name。
   */
  produces_dims_labels?: string[]
  /** 数据依赖（机器名） */
  consumes_objects?: string[]
  /** 数据依赖的中文名（本体 objects/links title，换本体自动跟随） */
  consumes_labels?: string[]
}

/** 停用后果摘要（供二次确认弹窗使用） */
export function lensDisableImpact(l: LensSpecItem): string {
  const dims = (l.produces_dims_labels ?? l.produces_dims ?? []).filter(Boolean)
  const dimPart = dims.length
    ? `本案件将不再产出「${dims.join('、')}」维度线索`
    : '本案件将不再产出该镜头的线索'
  const rescan = '停用后自动入队重扫（RESCAN），已生效版本的检测结果会重算'
  return `${dimPart}；${rescan}。`
}

/** 用途说明兜底：pack.json 未声明 description 时按本体维度/依赖自动生成 */
export function lensFallbackDesc(l: LensSpecItem): string {
  const dims = (l.produces_dims_labels ?? l.produces_dims ?? []).filter(Boolean)
  const dep = (l.consumes_labels ?? []).filter(Boolean)
  const dimPart = dims.length ? `产出「${dims.join('、')}」维度线索` : '产出线索'
  const depPart = dep.length ? `，依赖 ${dep.length} 类数据（${dep.slice(0, 3).join('、')}${dep.length > 3 ? ' 等' : ''}）` : ''
  return `按已接入数据${dimPart}${depPart}。`
}

/** 展示用用途说明：声明优先，缺失兜底（保证任何镜头都不会"无说明可看"） */
export function lensDesc(l: LensSpecItem): string {
  return (l.description ?? '').trim() || lensFallbackDesc(l)
}

export interface LensListResult {
  lenses: LensSpecItem[]
  overrides_file: string
}

export interface LensSwitchBody {
  enabled: boolean
  /** 变更理由（落审计链 note） */
  reason?: string
  /**
   * 画布可用开关（可选）。不传 → 按 enabled 推导（停用即两处都不用）。
   * 传了则与批量开关独立：可不自动跑但仍能在画布手动跑。
   */
  canvas_enabled?: boolean | null
}

export interface LensSwitchResult {
  skill_id: string
  enabled: boolean
  canvas_enabled: boolean
  /** 批量开关是否变化：只有它变了才影响自动产出 → 才入队 RESCAN */
  batch_changed: boolean
  /**
   * 仅批量开关变化时入队；只改画布可用不重扫（改了立即生效），故可为 null。
   */
  rescan_task: TaskRow | null
}

export interface LensRunBody {
  params?: Record<string, unknown>
  reason?: string
  /** 零填写提交：必填参数由后端按 auto_from 推导（画布上下文 / 案件登记） */
  auto?: boolean
  /**
   * 发起来源（画布深挖）。
   * 后端据此前把结果**回到发起线索的画布**（原地并入「深挖结果」层），
   * 避免正兵跑完镜头后要跳去线索列表找结果、研判被打断。
   * 缺失表示无发起画布（如从启停面板发起），行为同旧版。
   */
  origin?: {
    /** 发起线索 ID（必填，后端据此回挂） */
    clue_id: string
    /** 画布选中节点 ID（把结果挂在选中主体下，建立视觉关联） */
    node_id?: string
    /** 发起主体名 */
    subject?: string
    /** 发起面标识 */
    surface?: string
  }
}

/** 单个候选主体（core.focus.FocusSubject） */
export interface FocusCandidate {
  name: string
  type: string
  score: number
  source: string
  evidence: number
}

/** 单个参数的候选集（后端按「画布选中 > 画布可见 > 案件登记 > 线索 > 语义层」排序） */
export interface LensParamCandidates {
  param: string
  candidates: FocusCandidate[]
  recommended: FocusCandidate | null
  /** 推荐值来源（如 focus:case_aliases#1:张卫国），随线索落 detail 可审计 */
  source: string
  count: number
  /** 候选唯一 → 静默带入，不展示 */
  auto_only: boolean
  /** 候选 ≤100 → 下拉直列（默认选推荐值） */
  listable: boolean
  /** 支持按需搜索（候选过多时） */
  searchable: boolean
}

export interface LensParamsResult {
  skill_id: string
  params: Record<string, LensParamCandidates>
}

/** 候选来源徽标文案（让正兵一眼看出"系统替我选了谁"，且可覆盖） */
export function candidateSourceLabel(src: string): string {
  if (!src) return ''
  if (src.includes('canvas_selected')) return '画布选中'
  if (src.includes('canvas_visible')) return '画布节点'
  if (src.includes('case_knowledge')) return '案件指定'
  if (src.includes('case_aliases')) return '案件登记'
  if (src.includes('clue_subjects')) return '线索推导'
  if (src.includes('semantic_table')) return '数据枚举'
  if (src.includes('semantic_search')) return '搜索命中'
  if (src.includes('projects')) return '项目枚举'
  return '自动推荐'
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

/** 数据就绪度：跑之前就知道会不会因数据未接入而降级（不禁止运行） */
export interface LensReadiness {
  skill_id: string
  name: string
  ready: boolean
  /** 未接入的依赖（对象或链接名） */
  missing: string[]
  /** 声明的全部依赖 */
  deps: string[]
  note: string
}

/** 单个镜头对某假设的贴合度 */
export interface LensRecommendation {
  hypothesis_id: string
  hypothesis_desc: string
  dimension: string[]
  lenses: {
    skill_id: string
    name: string
    description: string
    score: number
    reasons: string[]
    /** recommended=贴切 / possible=沾边 / unrelated=无交集（不排除，仅排序） */
    recommendation: 'recommended' | 'possible' | 'unrelated'
  }[]
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

  /**
   * POST /cases/{cid}/lenses/{skillId}/params —— 参数候选（自动推荐数据源）。
   * 传画布上下文（selected_node / canvas_nodes）让候选聚焦当前研判范围，
   * 语义层不做全量返回（真实案件可达数万主体，全量进下拉会 DOM 爆炸）。
   */
  /**
   * GET /cases/{cid}/lenses/recommendations?clue_id=
   * 按**当前线索的假设**给镜头排贴合度（维度交集 + 对象交集）。
   * 线索无假设链 → 后端返回空，前端按默认顺序展示，不硬凑。
   */
  async recommendations(
    caseId: string,
    clueId: string,
  ): Promise<{ recommendations: LensRecommendation[] }> {
    const q = encodeURIComponent(clueId)
    const res = await api.get<{ recommendations: LensRecommendation[] }>(
      `/cases/${encodeURIComponent(caseId)}/lenses/recommendations?clue_id=${q}`,
    )
    return res.data
  },

  async paramCandidates(
    caseId: string,
    skillId: string,
    body: {
      canvas_nodes?: string[]
      selected_node?: string | null
      keyword?: string | null
    },
  ): Promise<LensParamsResult> {
    const res = await api.post<LensParamsResult>(
      `/cases/${encodeURIComponent(caseId)}/lenses/${encodeURIComponent(skillId)}/params`,
      body,
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
