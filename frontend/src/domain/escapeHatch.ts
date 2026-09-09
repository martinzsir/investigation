// 代码逃生舱（FE-P-017）纯函数：四类扩展元数据、stats 归一化、表单校验。
// 纪律：仅生成文本（不写盘/不注册/不执行）；无角色门槛。
import type { ExtType } from '../api/endpoints/escapeHatch'

export const EXT_META: Record<ExtType, { label: string; desc: string }> = {
  function: { label: '只读 Function', desc: '新增只读计算函数（SQL 白名单 + 参数模板），检测器薄编排' },
  value_type: { label: '值类型', desc: '新增属性值类型（TYPE_SQL 物化列 + 结构化 source TRY_CAST）' },
  clean_rule: { label: '清洗规则', desc: '新增 bindings clean 清洗规则名（脏值降级 NULL + 诊断）' },
  side_effect: { label: 'Action 副作用', desc: '新增可写动作副作用（ActionExecutor 唯一写路径注册）' },
}

export const EXT_ORDER: ExtType[] = ['function', 'value_type', 'clean_rule', 'side_effect']

export interface StatBar {
  ext_type: ExtType
  label: string
  count: number
}

/** stats 归一化：补零缺失类型、按固定顺序输出（条形图用） */
export function normalizeStats(items: { ext_type: string; count: number }[] | undefined): StatBar[] {
  const map = new Map((items ?? []).map((i) => [i.ext_type, i.count]))
  return EXT_ORDER.map((t) => ({
    ext_type: t,
    label: EXT_META[t].label,
    count: map.get(t) ?? 0,
  }))
}

/** 条形最大值归一（CSS 宽度百分比） */
export function barPct(count: number, max: number): number {
  if (max <= 0) return 0
  return Math.round((count / max) * 100)
}

export function statsMax(bars: StatBar[]): number {
  return bars.reduce((m, b) => Math.max(m, b.count), 0)
}

/** 空态：四类计数全 0 */
export function isEmptyStats(bars: StatBar[]): boolean {
  return bars.every((b) => b.count === 0)
}

/** name 校验：1–64，标识符友好（字母数字下划线） */
export function stubNameError(name: string): string {
  const v = name.trim()
  if (!v) return '扩展名称不能为空'
  if (v.length > 64) return '名称最长 64 字符'
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(v)) return '名称须为标识符（字母/下划线开头，仅含字母数字下划线）'
  return ''
}

/** description 可选，≤500 */
export function stubDescError(desc: string): string {
  return desc.length > 500 ? '说明最长 500 字' : ''
}
