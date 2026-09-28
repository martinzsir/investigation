// 三维交汇（人-时-地锚点上，关系/时间/空间三类证据的归集视图）。
//
// 与观察档案的关系（server/app/routers/convergence.py 同口径）
// ------------------------------------------------------------
// 交汇**不新增事实**：输入是观察档案，输出是"同一个锚点上有几个维度命中"。
// 它不是线索——不带假设链、不走处置流程；要变成命题须经观察提升。
//
// 三条读面红线（前端不得破坏）
// ----------------------------
// 1. **分数不是结论。** score 只用于排序；每条必须带三维各自的
//    count/precision/weight。只渲染一个总分就等于把精度加权重新掩盖掉——
//    正兵看不出"时间维是日期级、只有 0.6 权重"。
// 2. **重名不猜。** person_ambiguous=true 的锚点单列，不混进普通锚点排序。
// 3. **裁决未生效要看得见。** diagnostics.homonym_resolution.status 非 ok
//    时，主体按名归一、重名会被静默合并——必须显式告警。

import { api } from '../client'
import { noteDataVersion } from '../query-keys'

/** 单个维度在该锚点上的命中情况 */
export interface ConvergenceDim {
  hit: boolean
  /** 命中条数（**不是强度**：次数会骗人，须与 precision 同看） */
  count: number
  /** 时间精度档：second/minute/hour/date/unknown——决定权重 */
  precision: string
  /** 精度分布：{minute:3,hour:1}，混合档时看得出木桶效应 */
  precision_dist?: Record<string, number>
  /** 加权后的维度分（dim_weight × precision_weight × repeat_factor） */
  weight: number
  /** 未命中原因（null 表示命中） */
  reason: string | null
  /** 命中类型（如 accompany / anomaly:off_route / segment:stay） */
  kinds: string[]
  /** 累计停留分钟（空间维 segment 有值） */
  duration_minutes?: number | null
  /** 事件类型（时间维有值） */
  types?: string[]
  /** 边的类型（关系维有值） */
  edge_kinds?: string[]
}

/** 锚点坐标——**必须带来源自陈**，否则质心会被读成门牌精度 */
export interface ConvergenceCoord {
  location_id: string
  std_address: string | null
  lat: number
  lng: number
  coord_sys: string | null
  geocode_source: string | null
  geocode_confidence: number | null
  /**
   * true = 区划质心坐标（非门牌位置）。地图上**不得**画成实心精确点——
   * 地图天生看上去精确，若不区分就等于把「区级推算」伪装成精确位置，
   * 是空间侧 distance_m=0.0 那类伪精确在地图上的翻版。
   */
  coord_degraded: boolean
  coord_note: string | null
}

export interface ConvergenceItem {
  key: string
  /** 主体键；重名待裁决时前缀 `?`——前端须与确定主体区分 */
  person_key: string
  person_names: string[]
  /** 同名异人未裁决：此锚点的主体指代不明，**不可当作单一自然人** */
  person_ambiguous: boolean
  ambiguous_candidates: string[]
  date: string
  location_ids: string[]
  std_addresses: string[]
  /** 同现主体（不构成"接触"结论，仅陈述空间/时间接近） */
  co_present: string[]
  dimensions: {
    space: ConvergenceDim
    time: ConvergenceDim
    relation: ConvergenceDim
  }
  /** 命中维度数 1..3；1 = 单维发现（非交汇） */
  dim_hit_count: number
  hit_dimensions: string[]
  score: number
  max_score: number
  /** 给正兵看的陈述（不作定性） */
  claims: string[]
  /** 证伪条件 */
  falsification: string
  /** 锚点坐标（后端挂；无语义层连接时为空数组） */
  coords?: ConvergenceCoord[]
}

export interface ConvergenceStats {
  by_dimension: Record<string, number>
  by_hit_count: Record<string, number>
  /** 重名待裁决的锚点数——单列，不混进普通排序 */
  ambiguous: number
  max_score: number
}

/** 权重模型：后端声明，前端展示——不硬编码，改权重自动跟随 */
export interface WeightModel {
  dim_weight: Record<string, number>
  precision_weight: Record<string, number>
  repeat_step: number
  repeat_cap: number
  formula: string
}

export interface ConvergenceListResult {
  available: boolean
  convergences: ConvergenceItem[]
  total: number
  returned: number
  page: number
  page_size: number
  min_dims: number
  stats: ConvergenceStats | null
  weight_model: WeightModel | null
  note: string
  diagnostics: {
    homonym_resolution?: {
      status: string
      detail: string
      effect?: string
    }
    observations_used?: number
    [k: string]: unknown
  }
}

/** 支撑观察：交汇凭什么成立——可顺 obs_id 回到观察档案 */
export interface ConvergenceSupport {
  observation_id: string
  skill_id: string
  lens_name: string
  title: string
  subject: string
  basis: string
  falsification: string
  degraded: boolean
  degraded_reason: string
  facts: Record<string, unknown>[]
  facts_total: number
}

export interface ConvergenceDetailResult {
  available: boolean
  convergence: ConvergenceItem
  support: {
    space: ConvergenceSupport[]
    time: ConvergenceSupport[]
    relation: ConvergenceSupport[]
  }
  observations_index: number
  weight_model: WeightModel | null
  note: string
  diagnostics: Record<string, unknown>
}

export const convergenceApi = {
  /** GET /cases/{cid}/convergence —— 维度/主体/日期/歧义筛选 + 统计 */
  async list(
    caseId: string,
    params: {
      person?: string
      dim?: string
      date_from?: string
      date_to?: string
      ambiguous?: boolean
      min_dims?: number
      page?: number
      page_size?: number
    } = {},
  ): Promise<ConvergenceListResult> {
    const q = new URLSearchParams()
    if (params.person) q.set('person', params.person)
    if (params.dim) q.set('dim', params.dim)
    if (params.date_from) q.set('date_from', params.date_from)
    if (params.date_to) q.set('date_to', params.date_to)
    if (params.ambiguous !== undefined) q.set('ambiguous', String(params.ambiguous))
    if (params.min_dims) q.set('min_dims', String(params.min_dims))
    if (params.page) q.set('page', String(params.page))
    if (params.page_size) q.set('page_size', String(params.page_size))
    const qs = q.toString()
    const res = await api.get<ConvergenceListResult>(
      `/cases/${encodeURIComponent(caseId)}/convergence${qs ? `?${qs}` : ''}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/convergence/{conv_key} —— key 含 `|`，须编码 */
  async detail(caseId: string, convKey: string): Promise<ConvergenceDetailResult> {
    const res = await api.get<ConvergenceDetailResult>(
      `/cases/${encodeURIComponent(caseId)}/convergence/${encodeURIComponent(convKey)}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}

/** 维度中文名（展示用；键是机器码，与本体一致） */
export const DIM_LABEL: Record<string, string> = {
  space: '空间',
  time: '时间',
  relation: '关系',
}

/** 精度档中文名 + 说明（防止"date 档"被误读成精确同时间） */
export const PRECISION_LABEL: Record<string, { label: string; hint: string }> = {
  second: { label: '秒级', hint: '可判同一秒内' },
  minute: { label: '分钟级', hint: '可判时间窗真重叠' },
  hour: { label: '小时级', hint: '可判同小时，不可判同时' },
  date: { label: '日期级', hint: '仅知前后一天，是同地异时' },
  unknown: { label: '未知', hint: '精度不明，权重最低' },
}

/** 维度的读面顺序：空间 → 时间 → 关系（与三维研判工作台一致） */
export const DIM_ORDER = ['space', 'time', 'relation'] as const
