// 实体裁决领域逻辑（FE-P-005 / FE-C-019 / FE-C-023）。
// 红线：同名不同人禁止自动合并——本模块只做键盘流/校验/差异标记，
// 合并与驳回一律由人工按键触发并落操作者+理由（server review.py W-021）。
// 纯函数、确定性可测；组件只做薄编排。

// ---------- 裁决动作（server review.py：review_merge / review_reject） ----------

export type VerdictAction = 'merge' | 'reject'

export const VERDICT_API_ACTION: Record<VerdictAction, 'review_merge' | 'review_reject'> = {
  merge: 'review_merge',
  reject: 'review_reject',
}

// ---------- 键盘流 A/R/D（FE-P-005：10 条/分钟不打断） ----------
// A=accept 确认为同一人（merge）；R=reject 确认为不同人（reject）；
// D=defer 跳过（仅前端游标前进，不调 API、不留裁决）。

export type VerdictKey = VerdictAction | 'defer'

export const VERDIT_KEY_BINDINGS: Record<string, VerdictKey> = {
  a: 'merge',
  r: 'reject',
  d: 'defer',
}

/**
 * 解析键盘事件：返回裁决意图；在输入框/文本域内打字时不触发（快捷键只在
 * 非编辑态生效），按修饰键（ctrl/meta/alt）组合也不触发。
 */
export function parseVerdictKey(e: {
  key: string
  target?: EventTarget | null
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
}): VerdictKey | null {
  if (e.altKey || e.ctrlKey || e.metaKey) return null
  const el = e.target as HTMLElement | null
  const tag = el?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || el?.isContentEditable) return null
  const k = e.key.toLowerCase()
  return VERDIT_KEY_BINDINGS[k] ?? null
}

// ---------- 理由校验（驳回理由必填，server 400 双保险） ----------

export const REASON_MAX = 200

/** 裁决理由校验：驳回必须填写且不少于 4 个字；合并可选但超长拦截 */
export function reasonError(action: VerdictAction | 'defer', reason: string): string {
  const t = reason.trim()
  if (action === 'reject' && !t) return '驳回（确认为不同人）必须填写裁决理由'
  if (t.length > REASON_MAX) return `理由不超过 ${REASON_MAX} 字`
  return ''
}

// ---------- 无限加载队列（候选累积去重 by entity_id） ----------

export interface QueueItem {
  entity_id: string
}

export interface QueuePage<T extends QueueItem> {
  items: T[]
  total: number
}

/**
 * 累积页数据：按 entity_id 去重（服务端已裁决候选会从队列消失，分页窗口滑动
 * 时可能重复出现同一候选），保持服务端顺序。
 */
export function mergeQueuePage<T extends QueueItem>(acc: T[], page: QueuePage<T>): T[] {
  const seen = new Set(acc.map((c) => c.entity_id))
  const out = [...acc]
  for (const it of page.items) {
    if (!seen.has(it.entity_id)) {
      seen.add(it.entity_id)
      out.push(it)
    }
  }
  return out
}

/** 是否还有更多未加载（已加载去重后仍少于 total） */
export function hasMore(loadedCount: number, total: number): boolean {
  return loadedCount < total
}

// ---------- 证据双栏对比（FE-C-019：差异行琥珀金 warn 高亮） ----------

export interface CompareRow {
  label: string
  left: string
  right: string
  /** 相似依据行（如统一社会信用代码一致）——同样 warn 高亮提示人工核对 */
  basis?: boolean
}

export interface DiffRow extends CompareRow {
  /** 左右值不一致（归一化后比较；两侧皆空视为无差异） */
  diff: boolean
}

function norm(v: string): string {
  return (v ?? '').trim().replace(/\s+/g, '')
}

/** 标记差异行：值不同且至少一侧非空 → diff=true（FE-C-019 warn 高亮依据）。
 *  泛型保留调用方扩展字段（mask/policy 等遮蔽属性）。 */
export function diffRows<T extends CompareRow>(rows: T[]): Array<T & { diff: boolean }> {
  return rows.map((r) => {
    const l = norm(r.left)
    const rr = norm(r.right)
    const bothEmpty = l === '' && rr === ''
    return { ...r, diff: !bothEmpty && l !== rr }
  })
}

// ---------- 置信度色阶（FE-C-023 LlmInferenceCard / 裁决页相似度） ----------

export type Band = 'ok' | 'warn' | 'error'

/** 置信度/画像分色阶：≥85 青(ok) / 60–84 琥珀(warn) / <60 红(error) */
export function confidenceBand(v: number): Band {
  if (v >= 0.85) return 'ok'
  if (v >= 0.6) return 'warn'
  return 'error'
}

// ---------- FE-C-023 LLM 判读卡三件套：缺一不渲染 ----------

export interface LlmInference {
  /** ① 判读结论文本 */
  text?: string
  /** ② 溯源行（推断必挂溯源，FE-T-004；空数组等同缺失） */
  source_rows?: Array<{ row_uri: string; source?: string }>
  /** ③ 模型归属（同源去重 FE-T-007 的归属标记）+ 置信度 */
  llm_model?: string
  confidence?: number
  /** 归属间（资金/通讯/…），用于标签展示 */
  room?: string
}

/**
 * 三件套齐备才允许渲染：结论、溯源（≥1 行）、模型归属（含置信度数值）。
 * 任一缺失 → 组件不渲染（v-if），防止无主推断/无溯源推断上屏。
 */
export function completeLlmInference(i: LlmInference): boolean {
  return (
    !!i.text &&
    i.text.trim().length > 0 &&
    Array.isArray(i.source_rows) &&
    i.source_rows.length > 0 &&
    !!i.llm_model &&
    i.llm_model.trim().length > 0 &&
    typeof i.confidence === 'number' &&
    Number.isFinite(i.confidence)
  )
}
