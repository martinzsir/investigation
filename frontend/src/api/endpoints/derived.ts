// R8 派生属性端点（REQ-028）：按对象/属性按需查询，不进线索详情。
// GET /cases/{cid}/objects/{objType}/{objId}/derived/{prop}
import { api } from '../client'
import { noteDataVersion } from '../query-keys'

/** 单派生属性响应（cache=hit/miss 为调试字段，随声明 cache_policy 语义变化） */
export interface DerivedPropertyDto {
  available: boolean
  object_type: string
  object_id: string
  /** 实体代理键（仅 available 时） */
  object_pk?: number | string
  property: string
  /** 绑定的白名单 Function（可审计） */
  function?: string
  /** never | ttl | until_source_change | materialized */
  cache_policy?: string
  /** 派生结果：rows 型 Function 为行数组，标量型为原始值 */
  value?: unknown
  computed_at?: number
  source_version_set?: string
  params_hash?: string
  cache?: 'hit' | 'miss' | string
  /** available:false 时的说明（语义层未构建） */
  note?: string
}

export const derivedApi = {
  async get(
    caseId: string,
    objType: string,
    objId: string,
    prop: string,
  ): Promise<DerivedPropertyDto> {
    const res = await api.get<DerivedPropertyDto>(
      `/cases/${encodeURIComponent(caseId)}`
      + `/objects/${encodeURIComponent(objType)}/${encodeURIComponent(objId)}`
      + `/derived/${encodeURIComponent(prop)}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
