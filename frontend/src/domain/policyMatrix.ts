// 权限矩阵领域逻辑（FE-P-005）：角色 rank、对象/链接策略格态、属性遮蔽判定。
// 纯函数、确定性可测；红线九：策略 fail-closed——未声明=拒绝（斜纹），绝不是允许。
import type { MaskMode, ObjectPolicy, PropertyPolicy, ViewDef } from '../api/endpoints/policies'

/** 角色等级（与 core/access.py ROLE_RANK 同一份定义；system=旁路不在矩阵中） */
export const ROLE_RANK: Record<string, number> = {
  见习: 0,
  正兵: 1,
  偏将: 2,
  主办: 3,
  human: 4,
}

/** 矩阵列（角色）顺序：低→高 */
export const MATRIX_ROLES = ['见习', '正兵', '偏将', '主办', 'human'] as const

/** 配置写门槛：偏将及以上（后端 can_config 同口径） */
export const CONFIG_WRITE_CLEARANCE = 2

export function canWriteConfig(clearance: number): boolean {
  return clearance >= CONFIG_WRITE_CLEARANCE
}

export type CellState =
  | 'allowed'        // 角色在 roles 内且 clearance ≥ min_clearance
  | 'denied'         // 已声明但该角色不满足
  | 'undeclared'     // 未声明对象策略 → fail-closed 拒绝

/**
 * 对象策略格态（红线九）。
 * 未声明 object_policy → 'undeclared'（斜纹，实际查询被拒）；
 * 声明了但角色不在 roles 或 rank < min_clearance → 'denied'；
 * 否则 'allowed'。
 */
export function objectCell(policy: ObjectPolicy | undefined, role: string): CellState {
  if (!policy) return 'undeclared'
  const rank = ROLE_RANK[role] ?? 0
  if (rank < policy.min_clearance) return 'denied'
  if (!policy.roles.includes(role)) return 'denied'
  return 'allowed'
}

export type PropertyVisibility =
  | 'visible'        // 明文可读
  | 'masked'         // 命中 mask（partial/full），遮蔽后可读
  | 'hidden'         // default=deny 且不在 allow_roles → 拒绝该列

/**
 * 属性级可见性（REQ-011）。对象本身不可见由调用方先挡（objectCell≠allowed）。
 * 策略缺省 default=allow、无 mask → visible；
 * default=allow 且 allow_roles 不含本角色 → hidden；
 * default=deny 且 allow_roles 含本角色 → visible（白名单放行）；
 * 放行后若 mask 声明 → masked。
 */
export function propertyVisibility(p: PropertyPolicy | undefined, role: string): PropertyVisibility {
  if (!p) return 'visible'
  const inAllow = (p.allow_roles ?? []).includes(role)
  let permitted: boolean
  if (p.default === 'deny') permitted = inAllow
  else permitted = p.allow_roles && p.allow_roles.length > 0 ? inAllow : true
  if (!permitted) return 'hidden'
  if (p.mask && p.mask !== 'none') return 'masked'
  return 'visible'
}

export const MASK_MODE_LABEL: Record<MaskMode, string> = {
  partial: '部分遮蔽',
  full: '全遮蔽',
  none: '不遮蔽',
}

/** 视图对某角色是否可见（roles 为空/含该角色/含 human 通配） */
export function viewVisible(view: ViewDef, role: string): boolean {
  if (!view.roles || view.roles.length === 0) return false
  return view.roles.includes(role) || view.roles.includes('human')
}

/** 矩阵统计：已声明对象数、未声明（fail-closed）数 */
export function matrixCoverage(
  objectNames: string[],
  policies: ObjectPolicy[],
): { declared: number; undeclared: string[] } {
  const declared = new Set(policies.map((p) => p.object))
  const undeclared = objectNames.filter((n) => !declared.has(n))
  return { declared: declared.size, undeclared }
}
