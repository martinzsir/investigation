import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// Ontology 配置下发（server/app/routers/ontology_config.py 契约）。
// GET /cases/{cid}/ontology-config —— 兵法五间/侦查五维/交叉等级/状态机/动作/计分
// 一份下发；名称全量返回（含受护间），命中数据仍由线索读面按角色过滤。

/** 兵法五间声明（线索 jian_types / 内间权限口径） */
export interface OntologyJian {
  name: string
  default_clearance: number
  weight?: number
  source_object_types?: string[]
}

/** 交叉等级（1/2/3 映射红线在前后端各自保留，名称可配） */
export interface CrossLevelDecl {
  min_independent_sources: number
  name: string
}

/** 侦查五维/数据通道声明（线索 detail.dimension / 雷达 / 房间色板口径） */
export interface OntologyDimension {
  /** 机器标识符（rules.json / hypothesis_patterns.json 引用它；如 fund/comm） */
  code?: string
  /** 展示名（中文，UI/报告显示；如 资金/通讯） */
  name: string
  note?: string
  source_object_types?: string[]
}

/** 维度 code → 展示名（线索详情/热力图把机器标识符翻译给人看） */
export function dimensionLabelMap(dims: OntologyDimension[]): Record<string, string> {
  const m: Record<string, string> = {}
  for (const d of dims ?? []) {
    const code = d.code || d.name
    m[code] = d.name
    m[d.name] = d.name // 兼容旧产物里的中文维度值
  }
  return m
}

export type StateTone = 'warning' | 'info' | 'muted' | 'success' | 'danger'

export interface StateDecl {
  name: string
  label: string
  tone: StateTone | string
  terminal: boolean
  /** any=AI/正兵均可；human=仅具名正兵（受控红线） */
  requires_role: 'any' | 'human' | string
  requires_basis: boolean
  /** D2：分态 SLA（天）；终态省略 = 不考核 */
  sla_days?: number
  /** 结论态映射（verified/excluded，案例沉淀用；可重开的旁路态也可有） */
  outcome?: 'verified' | 'excluded' | string
}

export interface ActionParameterDecl {
  name: string
  type: string
  required: boolean
  description?: string
}

export interface ActionDecl {
  name: string
  /** 权威按钮文案（actions.json title） */
  title: string
  target_status: string
  /** 状态机反推可达来源（全集） */
  allowed_from: string[]
  requires_role: 'any' | 'human' | string
  terminal: boolean
  parameters: ActionParameterDecl[]
  /** D1：显式收紧的可用来源（缺省 = 以 transitions 为准） */
  only_from?: string[]
}

export interface ScoringDimensionDecl {
  name: string
  weight: number
  source?: string
  normalize?: number
  cap?: number
}

/** 业务事件时间字段（本体 semantic:event_time 声明；空=本体未声明） */
export interface OntologyTimeFields {
  /** 对象名 → 该对象的业务时间属性列表 */
  objects: Record<string, string[]>
  /** 全量去重（跨对象） */
  event_time: string[]
}

export interface OntologyConfig {
  pack: string
  jians: OntologyJian[]
  cross_levels: CrossLevelDecl[]
  dimensions: OntologyDimension[]
  states: StateDecl[]
  transitions: Record<string, string[]>
  actions: ActionDecl[]
  scoring: {
    dimensions: ScoringDimensionDecl[]
    assumption_confidence: Record<string, number>
  }
  /** 业务事件时间字段（画布业务时间轴依据；空结构=本体未声明→回落过程时间） */
  time_fields?: OntologyTimeFields
}

export const ontologyConfigApi = {
  /** GET /cases/{cid}/ontology-config —— 只读声明，未 BUILD 案件回落内置包 */
  async get(caseId: string): Promise<OntologyConfig> {
    const res = await api.get<OntologyConfig>(
      `/cases/${encodeURIComponent(caseId)}/ontology-config`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
