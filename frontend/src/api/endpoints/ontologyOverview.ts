import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// S1 本体管理器（server/app/routers/ontology_overview.py 契约）。
// GET /cases/{cid}/ontology/overview —— 19 个本体声明文件总览（语义六组）。

export type SourceLayer = 'shared' | 'industry' | 'case'

export type FileStatus = 'ok' | 'missing' | 'parse_error'

export interface OntologyFileEntry {
  /** 文件名（不含 .json，如 objects） */
  name: string
  /** 语义分组（对象模型/数据/研判/治理/知识/计分） */
  group: string
  /** 是否有后端写路由（8 可写 / 11 只读） */
  writable: boolean
  /** schemas/ 下是否有对应 JSON Schema */
  has_schema: boolean
  /** 来源层（存在才列，顺序 全域→行业→案件；E3-1 缺层不报错） */
  source_layer: SourceLayer[]
  /** 条目数（缺失/解析失败为 null） */
  item_count: number | null
  /** 最后修改时间（ISO；缺失/解析失败为 null） */
  updated_at: string | null
  /** E1-3/E1-4 局部降级：ok / missing / parse_error */
  status: FileStatus
}

export interface OntologyOverviewDto {
  case_id: string
  case_name: string
  pack_id: string
  /** 行业包名（pack_meta.json industry；无行业层为 null） */
  industry: string | null
  /** 本体声明指纹（12 位；前端展示前 8 位 + …） */
  ontology_version: string
  files: OntologyFileEntry[]
}

export const ontologyOverviewApi = {
  async overview(caseId: string): Promise<OntologyOverviewDto> {
    const res = await api.get<OntologyOverviewDto>(
      `/cases/${encodeURIComponent(caseId)}/ontology/overview`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
