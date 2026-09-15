import type { ObjectType, PropertySpec, ValueType } from '../api/endpoints/model'

// 可视化本体建模器纯域逻辑（OntologyModelerView 消费；独立于视图可测）。

export interface DegradedBinding {
  obj: string
  prop: string
  /** 已失联的数据元码 */
  de: string
}

/**
 * E2-1 数据元失联降级（PRD §10 / 线框 D3 / UC-02-4）：
 * 凡 properties 绑定的 data_element 不在全域层 ∪ 案件层 knownDE 中 → 就地降级为自由类型：
 *   - 移除 data_element 键；
 *   - type 缺省补 'string'（显式 type 保留）；
 *   - 其余未知键原样保留（R2）。
 *
 * 就地修改传入数组——调用方传工作副本，保存快照不动 → dirty 如实反映文件尚存脏绑定；
 * 返回降级清单供 UI 提示。UC-02-4 FAIL 判据「静默保存成功」由此封死：
 * 载入即降级，脏绑定不可能再被原样保存。
 */
export function degradeStaleBindings(
  objects: ObjectType[],
  knownDE: Record<string, unknown>,
): DegradedBinding[] {
  const out: DegradedBinding[] = []
  for (const o of objects) {
    const props = o.properties
    if (!props) continue
    for (const k of Object.keys(props)) {
      const v = props[k]
      if (typeof v !== 'object' || v === null) continue
      const de = v.data_element
      // Object.hasOwn 防 'toString'/'constructor' 等原型键被误判为有效数据元
      // 跳过无 data_element / 空值 / 有效绑定（knownDE 中存在）；仅失联的进入降级
      if (typeof de !== 'string' || de === '' || Object.hasOwn(knownDE, de)) continue
      const rest: PropertySpec & Record<string, unknown> = { ...v }
      delete rest.data_element
      const hadType = rest.type !== undefined
      if (!hadType) rest.type = 'string'
      const keys = Object.keys(rest)
      // 原本有 type → 保留对象形式；降级补的 type 且无其他键 → 简化为裸字符串
      props[k] = (!hadType && keys.length === 1 ? rest.type : rest) as ValueType | PropertySpec
      out.push({ obj: o.name, prop: k, de })
    }
  }
  return out
}
