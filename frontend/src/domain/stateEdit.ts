// S4-F3 状态机编辑领域逻辑（纯函数、确定性可测）。
// 🔴 R3 终态不得设为可转移出（硬阻止）；E3-2 删除被动作引用的状态阻止；
// 取消终态标记（terminal true→false）为危险变更（§8.4，理由必填）。
import type { ActionDecl, StateDecl } from '../api/endpoints/governance'

/** 归一化状态对象（补 terminal 布尔与可选字段） */
export function normalizeState(s: Partial<StateDecl>): StateDecl {
  return {
    name: s.name ?? '',
    label: s.label ?? s.name ?? '',
    tone: s.tone ?? 'info',
    terminal: Boolean(s.terminal),
    requires_role: s.requires_role ?? 'any',
    requires_basis: Boolean(s.requires_basis),
    ...(s.sla_days !== undefined ? { sla_days: s.sla_days } : {}),
    ...(s.outcome !== undefined ? { outcome: s.outcome } : {}),
    ...(s._unknown_keys ? { _unknown_keys: [...(s._unknown_keys ?? [])] } : {}),
  }
}

export interface StateValidation {
  errors: string[]
  warnings: string[]
}

/**
 * 状态机整体校验。
 * @param states        编辑后的状态数组
 * @param transitions   {from:[to]} 迁移表
 * @param oldStateNames 编辑前状态名（用于判删除）
 * @param referencedBy  状态名 → 引用它的动作名（states GET 返回）
 */
export function validateStates(
  states: StateDecl[],
  transitions: Record<string, string[]>,
  oldStateNames: string[],
  referencedBy: Record<string, string[]>,
): StateValidation {
  const errors: string[] = []
  const warnings: string[] = []
  const names: string[] = []
  const nameSet = new Set<string>()

  states.forEach((s, i) => {
    const ctx = `状态「${s.name || `#${i + 1}`}」`
    if (!s.name) {
      errors.push(`${ctx}：缺少状态名`)
    } else if (nameSet.has(s.name)) {
      errors.push(`${ctx}：状态名重复`)
    }
    if (s.name) {
      names.push(s.name)
      nameSet.add(s.name)
    }
  })

  if (states.length === 0) {
    errors.push('至少需要一个状态（states.json 不允许为空）')
  }

  const terminalSet = new Set(states.filter((s) => s.terminal).map((s) => s.name))

  for (const [frm, tos] of Object.entries(transitions)) {
    if (!nameSet.has(frm)) {
      errors.push(`迁移表来源「${frm}」未在状态列表声明`)
      continue
    }
    for (const to of tos) {
      if (!nameSet.has(to)) errors.push(`迁移表 ${frm}→「${to}」目标状态未声明`)
    }
    // 🔴 R3：终态不得被设为可转移出（硬阻止）
    if (terminalSet.has(frm) && tos.length > 0) {
      errors.push(
        `🔒 「${frm}」是终态，不可设置转出目标（终态一旦进入不可再转出）。` +
          `如需变更，请先取消其终态标记（该操作需危险确认）`,
      )
    }
  }

  // E3-2：删除的状态仍被动作引用 → 阻止（列引用动作）
  const next = new Set(names)
  for (const dn of oldStateNames) {
    if (next.has(dn)) continue
    const refs = referencedBy[dn]
    if (refs && refs.length > 0) {
      errors.push(`🔴 状态「${dn}」仍被动作 ${refs.join('、')} 引用，不能删除（请先改动作的目标/前置状态）`)
    }
  }

  // §14-3：死状态提示——既无入边、也不被任何动作目标引用（新增状态常见）
  const hasIncoming = new Set<string>()
  for (const tos of Object.values(transitions)) tos.forEach((t) => hasIncoming.add(t))
  for (const n of names) {
    if (!hasIncoming.has(n) && !(referencedBy[n] ?? []).length) {
      warnings.push(`状态「${n}」当前无任何迁入路径也未被动作引用，保存后将是不可达的死状态`)
    }
  }

  return { errors, warnings }
}

/** 取消终态标记（terminal true→false）的状态名（§8.4 危险变更，理由必填）。 */
export function unterminalChanges(before: StateDecl[], after: StateDecl[]): string[] {
  const oldTerm = new Map(before.map((s) => [s.name, Boolean(s.terminal)]))
  return after
    .filter((s) => oldTerm.get(s.name) === true && !s.terminal)
    .map((s) => s.name)
}

/** 渲染文本转移链（状态机只读总览用）。 */
export function formatChains(
  states: StateDecl[],
  transitions: Record<string, string[]>,
): string[] {
  return states.map((s) => {
    const tos = transitions[s.name] ?? []
    const lock = s.terminal ? ' 🔒终态' : ''
    const tail = tos.length ? tos.join('、') : tos.length === 0 && s.terminal ? '（终态，无转出）' : '（无转出）'
    return `${s.name}${lock} → ${tail}`
  })
}

/** 便捷：从动作列表构造 状态→引用动作 映射（测试/复用）。 */
export function buildReferencedBy(actions: ActionDecl[]): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  const add = (state: string, action: string) => {
    const list = out[state] ?? (out[state] = [])
    if (!list.includes(action)) list.push(action)
  }
  for (const a of actions) {
    if (a.target_status) add(a.target_status, a.name)
    for (const s of a.only_from ?? []) add(s, a.name)
  }
  return out
}
