// 案件画布镜头工具箱纯函数（P3 生长能力，PRD V1.0.0 功能 4）。
//
// 职责：把「选中画布节点」翻译成「可跑哪些镜头 / 参数自动带入哪些」——
//   ① 可用性：案件画布看 pack_canvas_enabled（pack.json canvas_enabled 声明）
//      与案件覆盖（case_override），与自动批量跑（enabled）彻底分离；
//   ② 靶心带入：按 params_schema 的 auto_from 与选中节点 kind 自动填参——
//      subject 节点 → 主体类参数（person_pk 优先、名称兜底）；
//      place 节点 → 坐标/地点标识类参数；
//   ③ 缺失必填：自动带入后仍缺的必填参数 → 禁用运行并提示，不让必败任务入队。
//
// 纪律：纯函数、不发请求、不碰 store；LensSpecItem 只是只读入参，
// 与 CanvasToolbox.vue（表单态）/CanvasView.vue（任务态）解耦，便于单测。

import type { LensSpecItem } from '../api/endpoints/lenses'

/** 工具箱轻量节点视图（宿主从画布文档节点裁剪而来，避免依赖 G6 内部结构） */
export interface ToolboxNode {
  id: string
  kind: string
  label: string
  props: Record<string, unknown>
}

/** auto_from 声明中工具箱关心的字段（pack.json params_schema.<p>.auto_from） */
interface AutoFromSpec {
  object_types?: string[]
  /** 双主体参数（subject_b ← pair_with: "subject_a"）：靶心只带入主参数 */
  pair_with?: string
}

function autoFromOf(spec: { auto_from?: unknown } | undefined): AutoFromSpec | null {
  const af = spec?.auto_from
  return af && typeof af === 'object' ? (af as AutoFromSpec) : null
}

/**
 * 案件画布工具箱可用性：案件覆盖优先，未覆盖回落包级画布白名单。
 * pack 未声明 canvas_enabled（默认 false）→ 不进工具箱。
 */
export function toolboxLensAvailable(l: LensSpecItem): boolean {
  if (l.case_override !== null && l.case_override !== undefined) {
    return l.case_override
  }
  return l.pack_canvas_enabled === true
}

/** 禁用原因（'' = 可运行）。草案镜头禁跑由后端 400 兜底，前端先拒。 */
export function lensDisabledReason(l: LensSpecItem): string {
  if (!toolboxLensAvailable(l)) return '已在启停面板停用（案件级覆盖）'
  if (l.mode !== 'deterministic') {
    return '草案镜头：产出须经人验，不支持直接调度'
  }
  return ''
}

/** 就绪度提示（只提示不禁跑：正兵有权在缺数据时试跑）；'' = 无提示 */
export function lensReadyHint(l: LensSpecItem): string {
  const r = l.readiness
  if (!r || r.ready) return ''
  const missing = (r.missing ?? []).filter(Boolean)
  return missing.length ? `缺数据：${missing.slice(0, 3).join('、')}${missing.length > 3 ? ' 等' : ''}` : r.note || '数据未就绪'
}

/** 主体节点可用标识：person_pk 优先（代理键精确），名称兜底（auto 消歧） */
function subjectIdentity(node: ToolboxNode): string {
  const pk = node.props?.person_pk
  if (typeof pk === 'string' && pk.trim()) return pk.trim()
  const name = node.props?.person_name
  if (typeof name === 'string' && name.trim()) return name.trim()
  return node.label?.trim() || ''
}

/**
 * 靶心自动带入：按 auto_from 与节点 kind 把选中节点标识填进参数表单。
 * 返回 values（建议值全量）与 autoKeys（自动填入的参数名，UI 置灰只读，
 * 用户可清除自动值改手填——置灰的是「建议」，不是「锁定」）。
 *
 * 规则：
 *   - subject 节点 → object_types 含主体类的参数（target_subject/subject_a）；
 *     pair_with 的从参数（subject_b）不带（需要第二个主体，用户手选）；
 *   - place 节点 → 参数名含 location 的带 location_id；center_lng/center_lat
 *     带坐标（lng/lat 尾缀匹配）；
 *   - 其余参数不猜（整数窗口/日期等让用户显式填）。
 */
export function autoFillParams(
  l: LensSpecItem,
  node: ToolboxNode | null,
): { values: Record<string, string>; autoKeys: string[] } {
  const values: Record<string, string> = {}
  const autoKeys: string[] = []
  if (!node) return { values, autoKeys }

  for (const [key, spec] of Object.entries(l.params_schema)) {
    const af = autoFromOf(spec)
    if (node.kind === 'subject' && af?.object_types?.length) {
      // 靶心只带入主参数：pair_with 从参数需要第二个主体，留手填
      if (af.pair_with && af.pair_with !== key) continue
      const identity = subjectIdentity(node)
      if (!identity) continue
      values[key] = identity
      autoKeys.push(key)
      continue
    }
    if (node.kind === 'place') {
      const locId = node.props?.location_id
      const lng = node.props?.lng
      const lat = node.props?.lat
      const k = key.toLowerCase()
      if (k.includes('location') && typeof locId === 'string' && locId.trim()) {
        values[key] = locId.trim()
        autoKeys.push(key)
      } else if (/lng$|longitude$/.test(k) && typeof lng === 'number') {
        values[key] = String(lng)
        autoKeys.push(key)
      } else if (/lat$|latitude$/.test(k) && typeof lat === 'number') {
        values[key] = String(lat)
        autoKeys.push(key)
      }
    }
  }
  return { values, autoKeys }
}

/** 自动带入后仍缺失的必填参数（禁用运行的唯一依据） */
export function missingRequiredParams(
  l: LensSpecItem,
  values: Record<string, unknown>,
): string[] {
  return Object.entries(l.params_schema)
    .filter(([key, spec]) => {
      if (!spec.required) return false
      const v = values[key]
      return v === null || v === undefined || (typeof v === 'string' && v.trim() === '')
    })
    .map(([key]) => key)
}

/** 一键可运行判定：可用 ∧ 确定性 ∧ 必填齐 */
export function isToolboxRunnable(
  l: LensSpecItem,
  values: Record<string, unknown>,
): boolean {
  return !lensDisabledReason(l) && missingRequiredParams(l, values).length === 0
}
