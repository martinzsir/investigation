// 处置看板领域逻辑（FE-P-009 / FE-C-033）：SLA 超期、停留天数、列分组、统计。
// 纯函数、确定性可测；组件只做薄编排。
// R6/D2/D5：列序与分态 SLA 取 ontology-config 声明；
// 旁路态（tone=muted，如已排除）固定置末，不直接吃 states 数组物理顺序；
// 常量仅为默认包快照（拉取失败/测试兜底）。
import type { OntologyConfig } from '../api/endpoints/ontologyConfig'
import type { ClueListItem } from '../api/endpoints/clues'
import { CLUE_STATUS, type ClueStatus } from './clue'

/** 看板默认五列顺序（已立案为受控终态金列、已排除为旁路灰列置末） */
export const BOARD_COLUMNS: ClueStatus[] = [
  CLUE_STATUS.PENDING,
  CLUE_STATUS.VERIFYING,
  CLUE_STATUS.CONFIRMED,
  CLUE_STATUS.FILED,
  CLUE_STATUS.EXCLUDED,
]

/**
 * 列序（D5）：按 states 声明序，但 tone==='muted' 的旁路态统一置末。
 * 受控终态（已立案）保持声明中的位置（金列在固证之后、旁路之前）。
 */
export function boardColumns(cfg?: OntologyConfig): string[] {
  if (!cfg) return BOARD_COLUMNS
  const normal = cfg.states.filter((s) => s.tone !== 'muted').map((s) => s.name)
  const bypass = cfg.states.filter((s) => s.tone === 'muted').map((s) => s.name)
  const ordered = [...normal, ...bypass]
  return ordered.length > 0 ? ordered : BOARD_COLUMNS
}

/**
 * 各状态停留 SLA（天）：超过即「超期」整卡红框（FE-C-033，D2）。
 * 权威值取 states.json sla_days；未声明（终态）= 不考核。
 */
const DEFAULT_SLA_DAYS: Partial<Record<ClueStatus, number>> = {
  待查: 3,
  查证中: 5,
  已固证: 7,
}

/** @deprecated 用 slaDays(status, cfg)；保留供默认快照/旧测试引用 */
export const SLA_DAYS: Partial<Record<ClueStatus, number>> = DEFAULT_SLA_DAYS

export function slaDays(status: string, cfg?: OntologyConfig): number | undefined {
  const d = cfg?.states.find((s) => s.name === status)?.sla_days
  return typeof d === 'number' ? d : DEFAULT_SLA_DAYS[status as ClueStatus]
}

const DAY_MS = 86_400_000

/** 停留天数（按 updated_at 计；非法日期回落 0） */
export function stayDays(updatedAt: string | undefined, now: Date = new Date()): number {
  if (!updatedAt) return 0
  const t = Date.parse(updatedAt.replace(' ', 'T'))
  if (Number.isNaN(t)) return 0
  return Math.max(0, Math.floor((now.getTime() - t) / DAY_MS))
}

/** 是否超期：未声明 SLA（终态）永不超期；考核态停留超过 SLA 即超期 */
export function isOverdue(
  status: ClueStatus | string,
  updatedAt: string | undefined,
  now: Date = new Date(),
  cfg?: OntologyConfig,
): boolean {
  const sla = slaDays(status, cfg)
  if (sla === undefined) return false
  return stayDays(updatedAt, now) > sla
}

export interface BoardCard extends ClueListItem {
  stayDays: number
  overdue: boolean
}

export function toBoardCard(c: ClueListItem, cfg?: OntologyConfig, now: Date = new Date()): BoardCard {
  return {
    ...c,
    stayDays: stayDays(c.updated_at, now),
    overdue: isOverdue(c.status, c.updated_at, now, cfg),
  }
}

export interface BoardStats {
  total: number
  byStatus: Record<string, number>
  overdue: number
  /** 平均停留（仅统计考核中状态，单位天、保留 1 位小数） */
  avgStay: number
}

export function boardStats(cards: BoardCard[], cfg?: OntologyConfig): BoardStats {
  const columns = boardColumns(cfg)
  const byStatus = Object.fromEntries(columns.map((s) => [s, 0])) as Record<string, number>
  let overdue = 0
  let staySum = 0
  let stayN = 0
  for (const c of cards) {
    if (byStatus[c.status] !== undefined) byStatus[c.status] += 1
    if (c.overdue) overdue += 1
    if (slaDays(c.status, cfg) !== undefined) {
      staySum += c.stayDays
      stayN += 1
    }
  }
  return {
    total: cards.length,
    byStatus,
    overdue,
    avgStay: stayN ? Math.round((staySum / stayN) * 10) / 10 : 0,
  }
}
