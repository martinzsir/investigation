// 处置看板领域逻辑（FE-P-009 / FE-C-033）：SLA 超期、停留天数、五列分组、统计。
// 纯函数、确定性可测；组件只做薄编排。
import type { ClueListItem } from '../api/endpoints/clues'
import { CLUE_STATUS, type ClueStatus } from './clue'

/** 看板五列顺序（与五态状态机一致；已立案为终态金列、已排除为旁路灰列） */
export const BOARD_COLUMNS: ClueStatus[] = [
  CLUE_STATUS.PENDING,
  CLUE_STATUS.VERIFYING,
  CLUE_STATUS.CONFIRMED,
  CLUE_STATUS.FILED,
  CLUE_STATUS.EXCLUDED,
]

/**
 * 各状态停留 SLA（天）：超过即「超期」整卡红框（FE-C-033）。
 * 终态（已立案/已排除）不考核停留——不设阈值。
 */
export const SLA_DAYS: Partial<Record<ClueStatus, number>> = {
  待查: 3,
  查证中: 5,
  已固证: 7,
}

const DAY_MS = 86_400_000

/** 停留天数（按 updated_at 计；非法日期回落 0） */
export function stayDays(updatedAt: string | undefined, now: Date = new Date()): number {
  if (!updatedAt) return 0
  const t = Date.parse(updatedAt.replace(' ', 'T'))
  if (Number.isNaN(t)) return 0
  return Math.max(0, Math.floor((now.getTime() - t) / DAY_MS))
}

/** 是否超期：终态永不超期；考核态停留超过 SLA 即超期 */
export function isOverdue(
  status: ClueStatus,
  updatedAt: string | undefined,
  now: Date = new Date(),
): boolean {
  const sla = SLA_DAYS[status]
  if (sla === undefined) return false
  return stayDays(updatedAt, now) > sla
}

export interface BoardCard extends ClueListItem {
  stayDays: number
  overdue: boolean
}

export function toBoardCard(c: ClueListItem, now: Date = new Date()): BoardCard {
  return {
    ...c,
    stayDays: stayDays(c.updated_at, now),
    overdue: isOverdue(c.status, c.updated_at, now),
  }
}

export interface BoardStats {
  total: number
  byStatus: Record<ClueStatus, number>
  overdue: number
  /** 平均停留（仅统计考核中状态，单位天、保留 1 位小数） */
  avgStay: number
}

export function boardStats(cards: BoardCard[]): BoardStats {
  const byStatus = Object.fromEntries(BOARD_COLUMNS.map((s) => [s, 0])) as Record<ClueStatus, number>
  let overdue = 0
  let staySum = 0
  let stayN = 0
  for (const c of cards) {
    if (byStatus[c.status] !== undefined) byStatus[c.status] += 1
    if (c.overdue) overdue += 1
    if (SLA_DAYS[c.status] !== undefined) {
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
