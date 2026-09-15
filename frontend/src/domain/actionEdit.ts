// S4-F2 Action Type 编辑领域逻辑（纯函数、确定性可测）。
// 🔴 安全门禁以服务端为准（governance.py）；本模块负责：
//  1. 预判 terminal/requires_role 危险变更，驱动危险确认弹窗（改前→改后对照）；
//  2. 保存前本地校验：R5 悬空状态引用、only_from 不可达、枚举越界、参数名缺失；
//  3. 未知字段聚合提示（R6：原样保留，不静默丢弃）。
import type { ActionDecl, DangerChange } from '../api/endpoints/governance'

/** 动作名规则（与文件中既有命名一致：小写字母开头+下划线/数字） */
export const ACTION_NAME_RE = /^[a-z][a-z0-9_]*$/

export const ROLE_LABEL: Record<string, string> = {
  any: '任意角色（AI / 正兵及以上）',
  human: 'human（仅具名人工，AI 不可）',
}

/** 副作用中文标签（core.ontology_loader.ALLOWED_SIDE_EFFECTS） */
export const SIDE_EFFECT_LABEL: Record<string, string> = {
  set_clue_status: '回写线索状态',
  create_decision: '创建决策对象 obj_decision',
  merge_entity: '合并实体',
  dismiss_review: '驳回复核候选',
}

export interface ActionEnumsLike {
  requires_role: string[]
  side_effects: string[]
  derive: string[]
}

/** 归一化：补齐数组字段，避免模板里 undefined.push */
export function normalizeAction(a: Partial<ActionDecl>): ActionDecl {
  return {
    name: a.name ?? '',
    title: a.title ?? '',
    target_status: a.target_status ?? '',
    derive: a.derive ?? 'reverse_reach',
    only_from: a.only_from ? [...a.only_from] : undefined,
    parameters: (a.parameters ?? []).map((p) => ({ ...p })),
    requires_role: a.requires_role ?? 'any',
    terminal: Boolean(a.terminal),
    side_effects: a.side_effects ? [...a.side_effects] : [],
    description: a.description ?? '',
    ...(a._unknown_keys ? { _unknown_keys: [...(a._unknown_keys ?? [])] } : {}),
  }
}

/** 终态/角色危险字段改前→改后（与后端 _action_danger_changes 同口径）。 */
export function actionDangerChanges(
  before: ActionDecl[],
  after: ActionDecl[],
): DangerChange[] {
  const oldByName = new Map(before.map((a) => [a.name, a]))
  const changes: DangerChange[] = []
  for (const a of after) {
    const o = oldByName.get(a.name)
    if (!o) continue
    if (Boolean(o.terminal) !== Boolean(a.terminal)) {
      changes.push({
        name: a.name,
        title: a.title || a.name,
        field: 'terminal',
        before: o.terminal ? '是（终态）' : '否',
        after: a.terminal ? '是（终态）' : '否',
      })
    }
    if ((o.requires_role ?? 'any') !== (a.requires_role ?? 'any')) {
      changes.push({
        name: a.name,
        title: a.title || a.name,
        field: 'requires_role',
        before: o.requires_role ?? 'any',
        after: a.requires_role ?? 'any',
      })
    }
  }
  return changes
}

/** 反推：迁移表里可一步到达 target 的来源状态集合（loader reverse_reach 同口径）。 */
export function reachableSources(
  target: string,
  transitions: Record<string, string[]>,
): string[] {
  return Object.entries(transitions)
    .filter(([, tos]) => tos.includes(target))
    .map(([frm]) => frm)
}

export interface ActionValidation {
  /** 阻止保存（R5 / loader 必拦项） */
  errors: string[]
  /** 警告但允许（F2.3 终态不一致等） */
  warnings: string[]
}

export function validateActions(
  actions: ActionDecl[],
  opts: {
    stateNames: string[]
    stateTerminal: Record<string, boolean>
    transitions: Record<string, string[]>
    enums: ActionEnumsLike
  },
): ActionValidation {
  const errors: string[] = []
  const warnings: string[] = []
  const { stateNames, stateTerminal, transitions, enums } = opts
  const seen = new Set<string>()

  actions.forEach((a, i) => {
    const ctx = `动作「${a.name || `#${i + 1}`}」`
    if (!a.name) {
      errors.push(`${ctx}：缺少名称 name`)
    } else if (!ACTION_NAME_RE.test(a.name)) {
      errors.push(`${ctx}：name 只能小写字母开头，含小写字母/数字/下划线`)
    } else if (seen.has(a.name)) {
      errors.push(`${ctx}：动作名重复`)
    }
    seen.add(a.name)

    if (!a.target_status) {
      errors.push(`${ctx}：缺少目标状态 target_status`)
    } else if (!stateNames.includes(a.target_status)) {
      errors.push(
        `🔴 ${ctx}：目标状态「${a.target_status}」不存在于 states.json，` +
          `请先在状态机创建该状态（保存已被阻止）`,
      )
    }

    if (a.only_from && a.only_from.length === 0) {
      errors.push(`${ctx}：only_from 必须为非空数组（不额外收紧请整体留空）`)
    }
    if (a.only_from) {
      const bad = a.only_from.filter((s) => !stateNames.includes(s))
      if (bad.length) {
        errors.push(`🔴 ${ctx}：前置状态引用了不存在的状态：${bad.join('、')}`)
      } else if (a.target_status && stateNames.includes(a.target_status)) {
        const reachable = new Set(reachableSources(a.target_status, transitions))
        const unreachable = a.only_from.filter((s) => !reachable.has(s))
        if (unreachable.length) {
          errors.push(
            `${ctx}：前置状态 ${unreachable.join('、')} 在状态机中无法迁移到「${a.target_status}」`,
          )
        }
      }
    }

    if (!enums.requires_role.includes(a.requires_role)) {
      errors.push(`${ctx}：requires_role='${a.requires_role}' 非法`)
    }
    const badFx = a.side_effects.filter((fx) => !enums.side_effects.includes(fx))
    if (badFx.length) {
      errors.push(`${ctx}：未注册副作用 ${badFx.join('、')}`)
    }

    a.parameters.forEach((p, j) => {
      if (!p.name) errors.push(`${ctx}：参数第 ${j + 1} 行缺少名称`)
    })

    // F2.3：终态动作的目标状态在 states 中非终态 → 不一致但允许
    if (a.terminal && a.target_status && stateTerminal[a.target_status] === false) {
      warnings.push(
        `${ctx} 标记为终态动作，但其目标状态「${a.target_status}」在状态机中不是终态，请确认是否一致`,
      )
    }
  })

  return { errors, warnings }
}

/** 未知字段聚合（GET 附带的 _unknown_keys；R6 提示原样保留）。 */
export function unknownFieldSummary(actions: ActionDecl[]): { name: string; keys: string[] }[] {
  return actions
    .filter((a) => (a._unknown_keys ?? []).length > 0)
    .map((a) => ({ name: a.name, keys: [...(a._unknown_keys ?? [])] }))
}
