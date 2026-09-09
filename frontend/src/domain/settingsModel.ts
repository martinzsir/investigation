// 系统设置（FE-P-025）纯函数：admin 闸门、白名单键元数据、values diff、reason 校验。
// 纪律（B2）：非 admin 连 GET 都 403——前端据 /auth/me is_admin 直接渲染锁定面板，
// 不靠 403 探测；红线键（llm_enabled 等）永不可写，UI 以 🔒 锁定行静态展示。

/** admin 闸门：is_admin=true（is_admin=1 或 system 角色，后端 A2 透出） */
export function canViewAdminSettings(isAdmin: boolean): boolean {
  return isAdmin === true
}

export interface KeyMeta {
  key: string
  label: string
  unit?: string
  min?: number
  max?: number
  /** enum 白名单 */
  options?: string[]
  /** 只读透出（storage_root 永不可经 API 改写） */
  readonly?: boolean
  hint?: string
}

export const QUEUE_KEYS: KeyMeta[] = [
  { key: 'max_workers', label: 'Worker 并发数', min: 1, max: 16, hint: '默认 2' },
  { key: 'poll_interval_ms', label: '轮询间隔', unit: 'ms', min: 10, hint: '默认 100' },
]

export const RESOURCE_KEYS: KeyMeta[] = [
  { key: 'max_rows_default', label: '默认行数上限', min: 1, max: 100000, hint: '默认 1000' },
  { key: 'query_timeout_ms', label: '查询超时', unit: 'ms', min: 100, max: 600000, hint: '默认 30000' },
  { key: 'storage_root', label: '案件存储根目录', readonly: true, hint: '只读透出，不可改写' },
]

export const THRESHOLD_KEYS: KeyMeta[] = [
  { key: 'cross_level_min_sources', label: '交叉升格最少来源数', min: 1, hint: '红线下限：单源不升格，默认 1' },
  { key: 'cross_level_min_clues', label: '交叉升格最少线索数', min: 2, hint: '默认 2' },
  { key: 'stale_days', label: '线索停滞天数', unit: '天', min: 1, max: 3650, hint: '默认 14' },
]

export const FEATURE_KEYS: KeyMeta[] = [
  { key: 'ui_density', label: '界面密度', options: ['comfortable', 'compact'], hint: '仅前端展示偏好' },
]

/** 平台值仅为新建案件默认；案件生效阈值以案件快照为准（配置中心 note） */
export const THRESHOLDS_NOTE =
  '平台值仅用于新建案件默认；案件生效阈值以案件快照 thresholds.json 为准'

/** 红线锁定行（后端白名单本就不含；UI 静态展示不可关） */
export const LOCKED_REDLINES: { key: string; label: string; reason: string }[] = [
  { key: 'llm_enabled', label: 'LLM 大模型开关', reason: '红线配置：默认关闭，仅案件包显式声明可启用，永不在此开放' },
  { key: 'audit_immutable', label: '审计合规（链不可变）', reason: '红线配置：审计链只追加，不可关闭' },
]

/** 数值键钳制（区间；非法值回退当前值由组件处理） */
export function clampKey(meta: KeyMeta, v: number): number {
  if (!Number.isFinite(v)) return v
  let n = v
  if (meta.min !== undefined) n = Math.max(meta.min, n)
  if (meta.max !== undefined) n = Math.min(meta.max, n)
  return n
}

/** 编辑值与当前值 diff：返回真正变更的键 */
export function diffValues(
  current: Record<string, unknown>,
  edited: Record<string, unknown>,
): string[] {
  return Object.keys(edited).filter((k) => {
    const a = current[k]
    const b = edited[k]
    return String(a) !== String(b)
  })
}

/** 写设置 reason 校验：必填（后端空则 400） */
export function settingsReasonError(reason: string): string {
  return reason.trim() ? '' : '修改平台设置必须填写原因（审计留痕）'
}

/** 编辑表单是否可提交：有变更 + reason 非空 */
export function canSubmit(changedKeys: string[], reason: string): boolean {
  return changedKeys.length > 0 && reason.trim().length > 0
}
