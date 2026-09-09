// MVP-3 任务域：任务状态机、SSE 进度单调合并（重连不跳变）、断线提示阈值、统计。
// 纯函数，无 IO，全部可单测。事件体 = 后端 task_dto（asdict(TaskRow)，snake_case）。

export type TaskStatus = 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'

/** 后端任务行（server/app/meta/models.py TaskRow，直出 snake_case） */
export interface TaskRow {
  id: string
  case_id: string
  task_type: string
  params: Record<string, unknown>
  status: TaskStatus
  progress_pct: number
  progress_stage: string
  progress_label: string
  /** 纯文本明细（非 object；如「目标 v3（基线 v2）」「银行流水 1203 行 → BUILD 已入队」） */
  progress_detail: string
  retry_count: number
  max_retries: number
  idem_key: string
  created_at: string
  updated_at: string
  started_at: string
  finished_at: string
  error_code: string
  error_message: string
  created_by: string
}

export const TASK_TERMINAL: ReadonlySet<TaskStatus> = new Set([
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
])

/** 进行中（占用运行中区）：排队 + 运行 */
export function isActive(status: TaskStatus | string): boolean {
  return status === 'PENDING' || status === 'RUNNING'
}

export function isTerminal(status: TaskStatus | string): boolean {
  return TASK_TERMINAL.has(status as TaskStatus)
}

/** 仅排队中可取消（后端 W-P-014：RUNNING/终态 409，不协作中断） */
export function canCancel(status: TaskStatus | string): boolean {
  return status === 'PENDING'
}

export function isFailed(status: TaskStatus | string): boolean {
  return status === 'FAILED'
}

export interface StatusMeta {
  label: string
  tone: 'ok' | 'warn' | 'error' | 'info' | 'muted'
}

export const TASK_STATUS_META: Record<TaskStatus, StatusMeta> = {
  PENDING: { label: '排队中', tone: 'muted' },
  RUNNING: { label: '运行中', tone: 'info' },
  SUCCEEDED: { label: '已完成', tone: 'ok' },
  FAILED: { label: '失败', tone: 'error' },
  CANCELLED: { label: '已取消', tone: 'muted' },
}

/** 任务类型中文映射（server/app/worker/tasks.py TASK_*） */
const TASK_TYPE_LABELS: Record<string, string> = {
  BUILD: '语义层构建',
  PING: '心跳探测',
  ARCHIVE: '版本封存',
  DISPOSE: '线索处置',
  RESCAN: '规则重扫',
  IMPORT: '数据导入',
  REVIEW: '实体裁决',
  EXPORT: '案件包导出',
  IMPORT_PACKAGE: '案件包导入',
  QUALITY_CHECK: '数据质量检查',
  DIAGNOSE: '运行诊断',
  DE_RECOMMEND: '数据元推荐',
  DE_RECO_DECIDE: '建议裁决',
}

export function taskTypeLabel(t: string): string {
  return TASK_TYPE_LABELS[t] ?? t
}

/**
 * FE-I-006 红线：进度只增不减。
 * 重连后即便收到序号更靠前/快照更旧的帧，进度条也不得回退。
 * - 终态帧（SUCCEEDED/FAILED/CANCELLED）权威，直接采用（失败必须如实显示）；
 * - 非终态：pct 取较大值；stage/label/detail 跟随 pct 较大的那一帧；
 * - pct 相同则保留信息更丰富（stage 非空）的帧。
 */
export function mergeProgress(prev: TaskRow | null, next: TaskRow): TaskRow {
  if (!prev) return next
  if (isTerminal(next.status)) return next
  if (isTerminal(prev.status)) return prev
  const prevPct = normPct(prev.progress_pct)
  const nextPct = normPct(next.progress_pct)
  if (nextPct > prevPct) return next
  if (nextPct < prevPct) return prev
  // 等 pct：优先保留有阶段文案的
  return next.progress_stage || next.progress_label ? next : prev
}

/** pct 归一：null/非数/越界 → -1（表示 indeterminate「计算中」） */
export function normPct(pct: number | null | undefined): number {
  if (typeof pct !== 'number' || Number.isNaN(pct)) return -1
  if (pct < 0 || pct > 100) return -1
  return pct
}

/** 进度条是否走 indeterminate（pct 缺失/越界 → 「计算中」） */
export function isIndeterminate(pct: number | null | undefined): boolean {
  return normPct(pct) < 0
}

export interface TaskStats {
  total: number
  pending: number
  running: number
  succeeded: number
  failed: number
  cancelled: number
  active: number
}

export function taskStats(tasks: TaskRow[]): TaskStats {
  const s: TaskStats = {
    total: tasks.length, pending: 0, running: 0, succeeded: 0,
    failed: 0, cancelled: 0, active: 0,
  }
  for (const t of tasks) {
    if (t.status === 'PENDING') s.pending++
    else if (t.status === 'RUNNING') s.running++
    else if (t.status === 'SUCCEEDED') s.succeeded++
    else if (t.status === 'FAILED') s.failed++
    else if (t.status === 'CANCELLED') s.cancelled++
    if (isActive(t.status)) s.active++
  }
  return s
}

/**
 * FE-I-006：断线 >3s 才显示重连提示，避免瞬时抖动闪烁。
 * @param downMs 已断线毫秒数
 * @returns 提示文案；≤3s 返回空串（不提示）
 */
export function reconnectHint(downMs: number): string {
  if (downMs <= 3000) return ''
  return '连接中断，正在自动重连…'
}

/** 失败任务摘要：error_code + message + 重试次数 */
export function failureSummary(t: TaskRow): string {
  const parts: string[] = []
  if (t.error_code) parts.push(t.error_code)
  if (t.error_message) parts.push(t.error_message)
  if (t.retry_count > 0) parts.push(`已重试 ${t.retry_count}/${t.max_retries} 次`)
  return parts.join(' · ') || '任务失败'
}
