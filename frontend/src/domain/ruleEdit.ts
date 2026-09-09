// 规则工坊领域逻辑（FE-P-003）：可编辑字段门禁、双层编辑、RESCAN 影响、危险项判定。
// 纯函数、确定性可测；红线一：function/stage/hit_when 等结构字段只读，组件不得渲染编辑态。
import type { Rule, RuleEditBody } from '../api/endpoints/rules'

/** PUT 允许修改的字段（与后端 _EDITABLE_FIELDS 一致） */
export const EDITABLE_FIELDS = ['rule_text', 'params', 'enabled'] as const

/** 结构字段（只读展示；提交即 400） */
export const STRUCTURE_FIELDS = [
  'id',
  'function',
  'stage',
  'hit_when',
  'jian_types',
  'dimension',
  'title',
] as const

/** rule_text 最少字数（与后端 _RULE_TEXT_MIN 一致） */
export const RULE_TEXT_MIN = 20

/**
 * L0 红线键（FE-T-011，共 12 条）：规则身份与机器挂钩字段，
 * 是检测器/编译器的常量锚点——任何配置修改请求注入这些键都必须失败
 * （前端白名单摘取剥离 + 后端提交即 400 双保险），永远不可被配置覆盖。
 */
export const LOCKED_KEYS = [
  'id',
  'rule_id',
  'function',
  'function_catalog',
  'stage',
  'hit_when',
  'jian_types',
  'dimension',
  'title',
  'pack',
  'sql',
  'action',
] as const

/**
 * 从任意脏输入中只摘取白名单字段（PUT /rules/{rid} 出网前最后一道）。
 * L0 锁定键一律剥离（不随请求出网）；类型不符的字段忽略。
 */
export function sanitizeEditBody(raw: Record<string, unknown>): RuleEditBody {
  const out: RuleEditBody = {}
  if (typeof raw.rule_text === 'string') out.rule_text = raw.rule_text
  if (raw.params !== undefined && typeof raw.params === 'object' && raw.params !== null && !Array.isArray(raw.params)) {
    out.params = raw.params as Record<string, unknown>
  }
  if (typeof raw.enabled === 'boolean') out.enabled = raw.enabled
  if (typeof raw.reason === 'string') out.reason = raw.reason
  return out
}

/** 出网 body 是否含任何 L0 锁定键（用于防御断言/测试） */
export function containsLockedKey(raw: Record<string, unknown>): string[] {
  return LOCKED_KEYS.filter((k) => k in raw)
}

/** 上区：判据文本（自由编辑，不触发重跑） */
export function isRuleTextEditable(): boolean {
  return true
}

/** 下区：机器行为（params/enabled）——偏将及以上可写（后端 clearance≥2） */
export function canEditMachineBehavior(clearance: number): boolean {
  return clearance >= 2
}

/** 字段是否结构只读 */
export function isStructureField(field: string): boolean {
  return (STRUCTURE_FIELDS as readonly string[]).includes(field)
}

/** rule_text 校验：返回错误文案（'' = 合法） */
export function ruleTextError(text: string): string {
  const t = text.trim()
  if (!t) return '判据文本不能为空'
  if (t.length < RULE_TEXT_MIN) {
    return `判据须写明模式/反常理由/边界排除（至少 ${RULE_TEXT_MIN} 字，当前 ${t.length} 字）`
  }
  return ''
}

/** 计算本次提交实际变更的字段（与后端 changed 口径一致） */
export function diffRule(rule: Rule, body: RuleEditBody): Array<'rule_text' | 'params' | 'enabled'> {
  const changed: Array<'rule_text' | 'params' | 'enabled'> = []
  if (body.rule_text !== undefined && body.rule_text.trim() !== (rule.rule_text ?? '').trim()) {
    changed.push('rule_text')
  }
  if (body.params !== undefined) changed.push('params')
  if (body.enabled !== undefined && Boolean(body.enabled) !== Boolean(rule.enabled)) {
    changed.push('enabled')
  }
  return changed
}

/** 是否触发 RESCAN：params/enabled 变更影响机器结果；纯 rule_text 文本修订不重跑 */
export function triggersRescan(changed: Array<'rule_text' | 'params' | 'enabled'>): boolean {
  return changed.includes('params') || changed.includes('enabled')
}

/**
 * 危险项判定（FE-T-012）：params/enabled 改动改变机器行为 → 🔴 危险确认 + 理由必填；
 * 纯 rule_text 文本修订为普通确认。
 */
export function isDangerousChange(changed: Array<'rule_text' | 'params' | 'enabled'>): boolean {
  return triggersRescan(changed)
}

/** 提交前校验：返回错误文案（'' = 可提交） */
export function validateRuleEdit(
  rule: Rule,
  body: RuleEditBody,
  clearance: number,
): string {
  if (body.rule_text === undefined && body.params === undefined && body.enabled === undefined) {
    return '未提供可修改字段'
  }
  if ((body.params !== undefined || body.enabled !== undefined) && !canEditMachineBehavior(clearance)) {
    return '阈值/启停变更需偏将及以上（clearance≥2）'
  }
  if (body.rule_text !== undefined) {
    const err = ruleTextError(body.rule_text)
    if (err) return err
  }
  const changed = diffRule(rule, body)
  if (isDangerousChange(changed) && !(body.reason ?? '').trim()) {
    return '机器行为变更必须填写变更理由（审计留痕）'
  }
  return ''
}
