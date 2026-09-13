// 「下一步可核查」建议生成器（M4 P3 / canvas-verify-suggest.ts）
//
// 设计意图：事实 / 对象 / 数据行三类节点，本身不是「待核实」状态，
// 但它们的语义往往需要人工去「印证」——这正是「下一步可核查」的语义。
//
// 与 M4 RC-105（手册建议）的关系：
//   - RC-105 由「规则节点」发起，调用 .../suggestions 拿回一组 pb: 虚节点；
//   - 本模块由「事实/对象/数据行」发起，前端按节点 props 模板化生成建议文本。
//     模板是「业务语言」（用户能直接读懂的核查项描述），不暴露技术标识。
//
// 与入队的关系：
//   - 用户采纳后 → 走 POST .../nodes/{id}/to-verify（202 add_manual）→ 入队
//     生成「待核实」节点 → 核查工作台可见。
//   - 这里把这条路径抽象为 addManualVerify 通用方法（替代 hypothesisToVerify
//     命名上的局限）。
//
// 纯函数：
//   - buildVerifySuggestions(node, ctx) → VerifySuggestion[]  （同步、纯）
//   - sortVerifySuggestions(list)                          （按优先级稳定排序）
//   - filterAdopted(list, adoptedTexts)                    （剔除已采纳）
//   - 模板生成（factText / objectText / rowText）          （按节点 props 提取）

import type { CanvasDoc, CanvasNode } from './canvas'

// ======================================================================
// 类型
// ======================================================================

/** 核实渠道（与 M4 RC-105 envelope 中 channel 对齐） */
export type VerifyChannel = 'external' | 'function' | 'manual'

/** 建议优先级（数字越小越靠前） */
export const VERIFY_PRIORITY = {
  /** 必核：这条不核，链路断了 */
  critical: 0,
  /** 重要：节点属性明确但需要二次确认 */
  high: 1,
  /** 一般：常规核对 */
  normal: 2,
} as const
export type VerifyPriority = (typeof VERIFY_PRIORITY)[keyof typeof VERIFY_PRIORITY]

export interface VerifySuggestion {
  /** 前端稳定 id（同一节点同一模板重复点采纳 = 同一 id，但后端用幂等键去重） */
  id: string
  /** 提交给后端的核查项文本（= NInput textarea 默认值） */
  text: string
  /** 渠道标签，决定提交按钮展示与兜底逻辑 */
  channel: VerifyChannel
  /** 当 channel='function' 时挂查询名（M4 RC-204 同口径） */
  ref_function?: string
  /** 优先级（数字越小越靠前） */
  priority: VerifyPriority
  /** 一句话理由（抽屉里 chip 旁边展示，让用户秒懂「为啥建议」） */
  reason: string
  /** 证伪口径：用户确认采纳时显示在抽屉里 */
  falsification?: string
}

export interface SuggestionContext {
  /** 当前画布文档：用于过滤已采纳文本、参考邻接节点 */
  doc: CanvasDoc
  /** 已采纳核查项文本集合（前端去重，不重复建议） */
  adoptedTexts: ReadonlySet<string>
}

// ======================================================================
// 公开函数
// ======================================================================

/**
 * 给定一个节点 + 上下文，生成「下一步可核查」建议列表。
 * 适用于 fact / object / source_row；其他节点返回 []。
 *
 * 实现要点：
 *   - 按业务优先级排序（critical → normal），同优先级按 text 字典序稳定
 *   - 剔除已采纳文本（前端过滤；后端幂等会兜底，但能少一次 202 调用）
 *   - 空文本自动剔除（防御）
 */
export function buildVerifySuggestions(
  node: CanvasNode,
  ctx: SuggestionContext,
): VerifySuggestion[] {
  if (!node || node.kind === 'hypothesis' || node.kind === 'note') return []
  if (ctx.doc.nodes.some((n) => n.id === node.id) === false) return []

  const out: VerifySuggestion[] = []
  const props = (node.props ?? {}) as Record<string, unknown>
  const label = String(node.label ?? '').trim() || '该节点'

  if (node.kind === 'fact') {
    const src = String(props.source ?? '')
    out.push({
      id: `${node.id}:source`,
      text: `核验事实「${label}」的来源${src ? `（${src}）` : ''}是否与原始记录一致`,
      channel: src ? 'function' : 'manual',
      ref_function: src ? `verify_fact_source:${node.ref}` : undefined,
      priority: VERIFY_PRIORITY.critical,
      reason: '事实是链路锚点；来源不一致会让下游假设失效',
      falsification: '原始记录与本事实条目不一致即视为证伪',
    })
    if (props.timestamp) {
      out.push({
        id: `${node.id}:timestamp`,
        text: `确认事实「${label}」的时间戳（${String(props.timestamp)}）是否在合法区间`,
        channel: 'function',
        ref_function: 'verify_fact_timewindow',
        priority: VERIFY_PRIORITY.normal,
        reason: '时间窗碰撞类规则依赖事实时间精度',
      })
    }
  } else if (node.kind === 'object') {
    const typeTitle = String(props.type_title ?? props.type ?? '')
    out.push({
      id: `${node.id}:completeness`,
      text: `确认实体「${label}」${typeTitle ? `（${typeTitle}）` : ''}的关键属性是否完整`,
      channel: 'manual',
      priority: VERIFY_PRIORITY.high,
      reason: '实体属性缺漏会让邻接判断与扩展查询失真',
    })
    out.push({
      id: `${node.id}:alias`,
      text: `核对实体「${label}」的别名/同义标识是否需要合并`,
      channel: 'manual',
      priority: VERIFY_PRIORITY.normal,
      reason: '跨源合并时同名异实体是最常见的污染源',
    })
  } else if (node.kind === 'source_row') {
    const fields = Array.isArray(props.fields)
      ? (props.fields as Array<Record<string, unknown>>).map((f) => String(f.name ?? ''))
      : []
    out.push({
      id: `${node.id}:fields`,
      text: fields.length
        ? `取回并核对数据行「${label}」的关键字段：${fields.slice(0, 3).join('、')}${fields.length > 3 ? '…' : ''}`
        : `取回并核对数据行「${label}」的字段是否完整`,
      channel: 'function',
      ref_function: 'fetch_row_fields',
      priority: VERIFY_PRIORITY.high,
      reason: '数据行字段缺失会让溯源终止在源头',
    })
    out.push({
      id: `${node.id}:masking`,
      text: `检查数据行「${label}」的敏感字段是否按规则脱敏`,
      channel: 'manual',
      priority: VERIFY_PRIORITY.normal,
      reason: '脱敏规则偏离会污染下游展示与导出',
    })
  }

  // 过滤 + 排序
  return sortVerifySuggestions(
    out.filter((s) => s.text.trim().length > 0 && !ctx.adoptedTexts.has(s.text)),
  )
}

/** 按优先级稳定排序；同优先级按 text 字典序（让列表稳定不抖动） */
export function sortVerifySuggestions(list: VerifySuggestion[]): VerifySuggestion[] {
  return [...list].sort((a, b) => {
    if (a.priority !== b.priority) return a.priority - b.priority
    return a.text < b.text ? -1 : a.text > b.text ? 1 : 0
  })
}

/** 剔除已采纳的文本（独立导出，便于抽屉按需重过滤） */
export function filterAdopted(
  list: VerifySuggestion[],
  adoptedTexts: ReadonlySet<string>,
): VerifySuggestion[] {
  return list.filter((s) => !adoptedTexts.has(s.text))
}

/** 给定 doc + 选中节点 + 抽屉状态，构建一个 SuggestionContext */
export function makeSuggestionContext(
  doc: CanvasDoc,
  adoptedTexts: ReadonlySet<string> = new Set(),
): SuggestionContext {
  return { doc, adoptedTexts }
}

/** 渠道中文标签 + 抽屉 chip 颜色（color class 名映射到设计 tokens） */
export const VERIFY_CHANNEL_LABELS: Record<VerifyChannel, string> = {
  external: '外部调取',
  function: '只读查询',
  manual: '手工跟踪',
}

/** 给定节点集合，提取已采纳的核查项文本（去掉来源节点以外的 prop 引用） */
export function collectAdoptedVerifyTexts(doc: CanvasDoc): Set<string> {
  const out = new Set<string>()
  for (const n of doc.nodes) {
    if (n.kind !== 'verify_item' || n.adopted !== true) continue
    const t = String((n.props ?? {}).text ?? '').trim()
    if (t) out.add(t)
  }
  return out
}
