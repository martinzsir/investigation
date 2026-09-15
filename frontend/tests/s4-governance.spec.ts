import { describe, expect, it } from 'vitest'

import {
  ACTION_NAME_RE, actionDangerChanges, normalizeAction, reachableSources,
  unknownFieldSummary, validateActions,
  type ActionEnumsLike,
} from '../src/domain/actionEdit'
import {
  buildReferencedBy, formatChains, normalizeState, unterminalChanges,
  validateStates,
} from '../src/domain/stateEdit'
import type { ActionDecl, StateDecl } from '../src/api/endpoints/governance'

// S4 治理建模领域纯函数（视图层交互由 e2e/人工验收覆盖，这里只测确定性门禁）：
//  R2 危险字段 diff、R5 悬空引用、D1 only_from 不可达、枚举/参数校验、未知字段聚合；
//  R3 终态出边硬阻止、E3-2 删除被引用状态、取消终态 diff、死状态警告、文本链。

// ---- default 包风格夹具 ------------------------------------------------
const STATE_NAMES = ['待查', '查证中', '已排除', '已固证', '已立案']
const TRANSITIONS: Record<string, string[]> = {
  待查: ['查证中', '已排除', '已固证'],
  查证中: ['待查', '已排除', '已固证'],
  已排除: ['待查'],
  已固证: ['已排除', '已立案'],
  已立案: [],
}
const STATE_TERMINAL: Record<string, boolean> = {
  待查: false, 查证中: false, 已排除: false, 已固证: false, 已立案: true,
}
const ENUMS: ActionEnumsLike = {
  requires_role: ['any', 'human'],
  side_effects: ['set_clue_status', 'create_decision', 'merge_entity', 'dismiss_review'],
  derive: ['reverse_reach'],
}

function mkAction(partial: Partial<ActionDecl> & Pick<ActionDecl, 'name' | 'target_status'>): ActionDecl {
  return normalizeAction({
    parameters: [],
    requires_role: 'any',
    terminal: false,
    side_effects: ['set_clue_status'],
    ...partial,
  })
}

function mkState(name: string, partial: Partial<StateDecl> = {}): StateDecl {
  return normalizeState({ name, terminal: false, ...partial })
}

const DEFAULT_ACTIONS: ActionDecl[] = [
  mkAction({ name: 'verify', title: '开始查证', target_status: '查证中' }),
  mkAction({ name: 'reset', title: '退回待查', target_status: '待查' }),
  mkAction({
    name: 'confirm', title: '固证', target_status: '已固证', only_from: ['查证中'],
  }),
  mkAction({
    name: 'file', title: '立案', target_status: '已立案',
    requires_role: 'human', terminal: true,
    side_effects: ['set_clue_status', 'create_decision'],
    parameters: [{ name: 'legal_basis', type: 'string', required: true }],
  }),
]

const DEFAULT_STATES: StateDecl[] = [
  mkState('待查', { sla_days: 3 }),
  mkState('查证中', { tone: 'info', sla_days: 5 }),
  mkState('已排除', { tone: 'muted', requires_basis: true, outcome: 'excluded' }),
  mkState('已固证', { tone: 'success', requires_basis: true, outcome: 'verified' }),
  mkState('已立案', { tone: 'danger', terminal: true, requires_role: 'human', requires_basis: true }),
]
const DEFAULT_REFS = buildReferencedBy(DEFAULT_ACTIONS)

// ============================================================ actionEdit
describe('S4-F2 Action 领域逻辑', () => {
  it('ACTION_NAME_RE：小写字母开头 + 小写/数字/下划线', () => {
    expect(ACTION_NAME_RE.test('file')).toBe(true)
    expect(ACTION_NAME_RE.test('review_merge')).toBe(true)
    expect(ACTION_NAME_RE.test('a1_b2')).toBe(true)
    expect(ACTION_NAME_RE.test('File')).toBe(false)
    expect(ACTION_NAME_RE.test('1file')).toBe(false)
    expect(ACTION_NAME_RE.test('review-merge')).toBe(false)
  })

  it('normalizeAction 补默认且深拷贝数组（不共享入参引用）', () => {
    const params = [{ name: 'p1' }]
    const fx = ['set_clue_status']
    const a = mkAction({
      name: 'a1', target_status: '待查', parameters: params, side_effects: fx,
    })
    expect(a.derive).toBe('reverse_reach')
    expect(a.requires_role).toBe('any')
    expect(a.terminal).toBe(false)
    a.parameters.push({ name: 'p2' })
    expect(params).toHaveLength(1) // 改副本不影响原数组
  })

  it('actionDangerChanges：terminal / requires_role 改前→改后', () => {
    const after = DEFAULT_ACTIONS.map((a) => ({ ...a }))
    after.find((a) => a.name === 'file')!.terminal = false
    after.find((a) => a.name === 'verify')!.requires_role = 'human'
    const changes = actionDangerChanges(DEFAULT_ACTIONS, after)
    const fields = changes.map((c) => `${c.name}.${c.field}`)
    expect(fields).toContain('file.terminal')
    expect(fields).toContain('verify.requires_role')
    const term = changes.find((c) => c.field === 'terminal')!
    expect(term.before).toContain('是')
    expect(term.after).toBe('否')
  })

  it('actionDangerChanges：新增动作与无变更不产生 diff', () => {
    const added = [...DEFAULT_ACTIONS, mkAction({ name: 'new_one', target_status: '待查', terminal: true })]
    expect(actionDangerChanges(DEFAULT_ACTIONS, added)).toEqual([])
    expect(actionDangerChanges(DEFAULT_ACTIONS, DEFAULT_ACTIONS)).toEqual([])
  })

  it('reachableSources：按迁移表反推可一步到达 target 的来源', () => {
    expect(reachableSources('已固证', TRANSITIONS).sort()).toEqual(['待查', '查证中'])
    expect(reachableSources('已立案', TRANSITIONS)).toEqual(['已固证'])
    expect(reachableSources('不存在', TRANSITIONS)).toEqual([])
  })

  it('default 夹具整体合法：无 error', () => {
    const v = validateActions(DEFAULT_ACTIONS, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    })
    expect(v.errors).toEqual([])
  })

  it('R5：悬空 target_status 阻止保存', () => {
    const acts = DEFAULT_ACTIONS.map((a) => a.name === 'reset'
      ? { ...a, target_status: '已起诉' } : a)
    const v = validateActions(acts, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    })
    expect(v.errors.join('\n')).toContain('已起诉')
  })

  it('R5/E2-4：only_from 引用不存在状态阻止', () => {
    const acts = DEFAULT_ACTIONS.map((a) => a.name === 'confirm'
      ? { ...a, only_from: ['查证中', '已起诉'] } : a)
    const v = validateActions(acts, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    })
    expect(v.errors.join('\n')).toContain('已起诉')
  })

  it('D1：only_from 不可达（终态到不了目标）阻止', () => {
    const acts = DEFAULT_ACTIONS.map((a) => a.name === 'confirm'
      ? { ...a, only_from: ['已立案'] } : a)
    const v = validateActions(acts, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    })
    expect(v.errors.join('\n')).toContain('无法迁移')
  })

  it('枚举越界：非法 requires_role / 未注册副作用阻止', () => {
    const badRole = DEFAULT_ACTIONS.map((a) => a.name === 'reset'
      ? { ...a, requires_role: 'root' as ActionDecl['requires_role'] } : a)
    expect(validateActions(badRole, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    }).errors.join('\n')).toContain('requires_role')

    const badFx = DEFAULT_ACTIONS.map((a) => a.name === 'reset'
      ? { ...a, side_effects: ['set_clue_status', 'hack_the_planet'] } : a)
    expect(validateActions(badFx, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    }).errors.join('\n')).toContain('hack_the_planet')
  })

  it('参数缺名称 + 动作重名阻止', () => {
    const acts = [
      ...DEFAULT_ACTIONS,
      mkAction({ name: 'verify', target_status: '待查', parameters: [{ name: '' }] }),
    ]
    const v = validateActions(acts, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    })
    const text = v.errors.join('\n')
    expect(text).toContain('重复')
    expect(text).toContain('缺少名称')
  })

  it('F2.3：终态动作目标非终态 → 警告但不阻止', () => {
    const acts = DEFAULT_ACTIONS.map((a) => a.name === 'verify'
      ? { ...a, terminal: true, target_status: '查证中' } : a)
    const v = validateActions(acts, {
      stateNames: STATE_NAMES, stateTerminal: STATE_TERMINAL,
      transitions: TRANSITIONS, enums: ENUMS,
    })
    expect(v.errors).toEqual([])
    expect(v.warnings.join('\n')).toContain('不是终态')
  })

  it('unknownFieldSummary：只聚合有未知键的动作（R6）', () => {
    const acts = DEFAULT_ACTIONS.map((a) => ({ ...a }))
    acts.find((a) => a.name === 'verify')!._unknown_keys = ['custom_ticket']
    const sum = unknownFieldSummary(acts)
    expect(sum).toEqual([{ name: 'verify', keys: ['custom_ticket'] }])
  })
})

// ============================================================ stateEdit
describe('S4-F3 状态机领域逻辑', () => {
  it('normalizeState：默认值与可选字段', () => {
    expect(normalizeState({ name: 'X' }).tone).toBe('info')
    expect(normalizeState({ name: 'X' }).terminal).toBe(false)
    const withSla = normalizeState({ name: 'X', sla_days: 0 })
    expect(withSla.sla_days).toBe(0)
    expect(normalizeState({ name: 'X' }).sla_days).toBeUndefined()
  })

  it('default 状态机合法：无 error（已立案为终态且无出边）', () => {
    const v = validateStates(DEFAULT_STATES, TRANSITIONS, STATE_NAMES, DEFAULT_REFS)
    expect(v.errors).toEqual([])
  })

  it('R3：终态设出边硬阻止（UC-S4-15）', () => {
    const tr: Record<string, string[]> = {
      ...TRANSITIONS, 已立案: ['待查'],
    }
    const v = validateStates(DEFAULT_STATES, tr, STATE_NAMES, DEFAULT_REFS)
    expect(v.errors.join('\n')).toContain('终态')
  })

  it('R3：终态空出边列表允许（to:[] 是合法终态）', () => {
    const tr: Record<string, string[]> = { ...TRANSITIONS, 已立案: [] }
    const v = validateStates(DEFAULT_STATES, tr, STATE_NAMES, DEFAULT_REFS)
    expect(v.errors.join('\n')).not.toContain('已立案')
  })

  it('E3-2：删除仍被动作引用的状态阻止并列动作（UC-S4-16）', () => {
    const states = DEFAULT_STATES.filter((s) => s.name !== '已立案')
    const names = states.map((s) => s.name)
    const tr: Record<string, string[]> = {
      待查: TRANSITIONS.待查.filter((t) => names.includes(t)),
      查证中: TRANSITIONS.查证中.filter((t) => names.includes(t)),
      已排除: TRANSITIONS.已排除,
      已固证: TRANSITIONS.已固证.filter((t) => names.includes(t)),
    }
    const v = validateStates(states, tr, STATE_NAMES, DEFAULT_REFS)
    const text = v.errors.join('\n')
    expect(text).toContain('已立案')
    expect(text).toContain('file')
  })

  it('迁移目标/来源悬空阻止；状态列表为空阻止；重名阻止', () => {
    const danglingTo = validateStates(
      DEFAULT_STATES,
      { ...TRANSITIONS, 待查: [...TRANSITIONS.待查, '火星'] },
      STATE_NAMES, DEFAULT_REFS,
    )
    expect(danglingTo.errors.join('\n')).toContain('火星')

    expect(validateStates([], {}, STATE_NAMES, DEFAULT_REFS).errors.join('\n'))
      .toContain('至少需要一个状态')

    const dup = [mkState('待查'), mkState('待查')]
    expect(validateStates(dup, { 待查: [] }, STATE_NAMES, DEFAULT_REFS).errors.join('\n'))
      .toContain('重复')
  })

  it('§14-3 死状态：无入边且无动作引用 → 警告但不阻止', () => {
    const states = [...DEFAULT_STATES, mkState('孤岛')]
    const tr = { ...TRANSITIONS, 孤岛: [] }
    const v = validateStates(states, tr, STATE_NAMES, DEFAULT_REFS)
    expect(v.errors).toEqual([])
    expect(v.warnings.join('\n')).toContain('孤岛')
  })

  it('unterminalChanges：仅 true→false 检出（§8.4 危险变更）', () => {
    const after = DEFAULT_STATES.map((s) => ({ ...s }))
    after.find((s) => s.name === '已立案')!.terminal = false
    expect(unterminalChanges(DEFAULT_STATES, after)).toEqual(['已立案'])
    // false→true（新设终态）不算取消终态
    const setTerminal = DEFAULT_STATES.map((s) => s.name === '查证中' ? { ...s, terminal: true } : s)
    expect(unterminalChanges(DEFAULT_STATES, setTerminal)).toEqual([])
    // 新增的终态/非终态都不算
    expect(unterminalChanges(DEFAULT_STATES, [...DEFAULT_STATES, mkState('新增', { terminal: false })])).toEqual([])
  })

  it('formatChains：终态锁定文案与普通无转出', () => {
    const chains = formatChains(DEFAULT_STATES, TRANSITIONS)
    expect(chains.find((c) => c.startsWith('已立案'))).toContain('终态，无转出')
    expect(chains.find((c) => c.startsWith('已固证'))).toContain('已排除')
    const isolated = formatChains([mkState('孤岛')], { 孤岛: [] })
    expect(isolated[0]).toContain('（无转出）')
  })

  it('buildReferencedBy：target + only_from 同动作去重', () => {
    const refs = buildReferencedBy([
      mkAction({ name: 'go', target_status: 'A', only_from: ['A', 'B'] }),
      mkAction({ name: 'to_b', target_status: 'B' }),
    ])
    expect(refs.A).toEqual(['go']) // target 与 only_from 都指 A，只记一次
    expect(refs.B).toEqual(['go', 'to_b'])
  })
})
