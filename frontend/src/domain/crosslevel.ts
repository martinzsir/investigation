// 五间交叉升格的信号计数（FE-T-007 LLM 同源去重红线）。
// core/functions.py jian_cross_level 以「命中间类数 n」升格；本模块在前端把
// 证据信号折算为独立间数——同一 LLM 多次判读（备注可疑/聊天可疑/多轮复算）
// 只算一个间，绝不因模型复读而升格。结构化数据源（银行流水/通话记录/轨迹…）
// 按命中间类各自计数。纯函数、确定性可测。
import type { OntologyConfig } from '../api/endpoints/ontologyConfig'
import { crossLevel, dimensionRooms, type CrossLevel } from './clue'

/** 一条交叉信号：命中某侦查维度（资金/通讯/行为/关系/时间），来自某个数据源 */
export interface CrossSignal {
  /** 维度名（dimensions.json 已声明名之一；未知维度不计入） */
  room: string
  /** 数据源名（结构化源，如 银行流水/通话记录/住宿轨迹）；LLM 信号给模型归属标记 */
  source: string
  /** 存在即表示该信号是 LLM 判读（非结构化证据），值为模型标识 */
  llmModel?: string
}

/**
 * 独立间数（FE-T-007）：
 * - 结构化信号：按命中维度去重计数（同一维多个结构化源只算 1 间）；
 * - LLM 信号：同一模型（llmModel 缺失时归入 'llm' 同源桶）的多次判读，
 *   无论判读几维、换什么 prompt（备注/聊天/复算），至多贡献 1 个独立间；
 *   且只在该模型声称的维度未被结构化证据覆盖时才占额。
 * 红线用例：LLM 判「备注可疑→资金」+ LLM 判「聊天可疑→通讯」≠ 双源，n=1。
 *
 * 注意：这是前端对 LLM 信号的同源合并；与 R9 后端 jians.json
 * source_independence.related_pairs 的结构化源合并是两套口径。
 */
export function countIndependentJians(signals: CrossSignal[], cfg?: OntologyConfig): number {
  const knownRooms = new Set(dimensionRooms(cfg))
  const structuredRooms = new Set<string>()
  const llmClaims = new Map<string, Set<string>>()

  for (const s of signals) {
    if (!knownRooms.has(s.room)) continue
    if (s.llmModel) {
      const bucket = s.llmModel.trim() || 'llm'
      const rooms = llmClaims.get(bucket) ?? new Set<string>()
      rooms.add(s.room)
      llmClaims.set(bucket, rooms)
    } else {
      structuredRooms.add(s.room)
    }
  }

  let n = structuredRooms.size
  for (const rooms of llmClaims.values()) {
    // 同一 LLM 的多个维度声称：至多新增 1 个独立间
    const onlyLlm = [...rooms].some((r) => !structuredRooms.has(r))
    if (onlyLlm) n += 1
  }
  return n
}

/** 信号 → 交叉等级（FE-T-006/007：n>=3 候选 / n==2 线索 / 否则观察；名称取声明） */
export function crossLevelFromSignals(signals: CrossSignal[], cfg?: OntologyConfig): CrossLevel {
  return crossLevel(countIndependentJians(signals, cfg), cfg)
}
