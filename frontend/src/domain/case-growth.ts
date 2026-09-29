/**
 * 研判画布渐进式揭示集（v3 §6）——纯逻辑，不碰 HTTP/G6。
 *
 * 一个「可生长单元」= (靶心节点 id, 镜头 id)，与后端结论节点
 * ``lens_id@靶心ref`` 一一对应。揭示集持久化在案件画布文档
 * ``meta.revealed_groups = [{target, lens}]``（与后端
 * canvas_case_doc.META_REVEALED 同键同序口径）：
 *
 * - 缺省/空集 = 最小画布：打开只见人工提升的主体；
 * - 每揭示一组，GET 重建才长出该组结论节点；首个支撑结论被揭示时，
 *   关联假设才出现（假设层只从已揭示结论反查）；
 * - 前端只维护这一份 meta，观察档案（只追加真源）保持干净。
 *
 * 集合内部用 ``target␟lens`` 字符串键（Unit Separator 不会出现在 id 里），
 * 避免到处传 tuple、Set 相等性也按引用坑人。
 */
import type { CaseCanvasLensGroup } from './canvas'

/** 与 server canvas_case_doc.META_REVEALED 一致 */
export const META_REVEALED_GROUPS = 'revealed_groups'
const SEP = '␟'

/** 揭示集条目的落库形态（meta.revealed_groups 数组元素） */
export interface RevealedGroupMeta {
  target: string
  lens: string
}

/** 带可选 meta 的画布文档（CanvasDoc 类型未声明 meta，这里局部放宽） */
export type DocWithMeta = {
  nodes: unknown[]
  edges: unknown[]
  meta?: Record<string, unknown> | null
}

export function revealKey(target: string, lens: string): string {
  return `${target}${SEP}${lens}`
}

export function parseRevealKey(key: string): RevealedGroupMeta | null {
  const i = key.indexOf(SEP)
  if (i <= 0 || i === key.length - 1) return null
  return { target: key.slice(0, i), lens: key.slice(i + 1) }
}

/**
 * 从 doc.meta 读揭示集。缺省/脏值一律降级空集（fail-closed：
 * 读不出就不多长一个结论节点——与后端 parse_revealed_groups 同纪律）。
 */
export function readRevealedGroups(
  meta: Record<string, unknown> | null | undefined,
): Set<string> {
  const out = new Set<string>()
  const raw = meta?.[META_REVEALED_GROUPS]
  if (!Array.isArray(raw)) return out
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const target = String((item as Record<string, unknown>).target ?? '').trim()
    const lens = String((item as Record<string, unknown>).lens ?? '').trim()
    if (target && lens) out.add(revealKey(target, lens))
  }
  return out
}

/** 集合 → 落库数组；排序保证文档稳定（diff/审计可读，同后端 serialize） */
export function serializeRevealedGroups(keys: Set<string>): RevealedGroupMeta[] {
  return [...keys]
    .map(parseRevealKey)
    .filter((x): x is RevealedGroupMeta => x !== null)
    .sort((a, b) => a.target === b.target
      ? a.lens.localeCompare(b.lens)
      : a.target.localeCompare(b.target))
}

/** 增删一组，返回新集合（不改入参，Vue 响应式安全） */
export function toggleRevealedGroup(
  keys: Set<string>,
  target: string,
  lens: string,
  revealed: boolean,
): Set<string> {
  const next = new Set(keys)
  const k = revealKey(target, lens)
  if (revealed) next.add(k)
  else next.delete(k)
  return next
}

/** 返回带规范化 meta.revealed_groups 的**新文档**（不动入参） */
export function withRevealedGroups<T extends DocWithMeta>(
  doc: T,
  keys: Set<string>,
): T {
  const meta: Record<string, unknown> = { ...(doc.meta ?? {}) }
  meta[META_REVEALED_GROUPS] = serializeRevealedGroups(keys)
  return { ...doc, meta }
}

// ---- 清单（lens_layer.groups）派生 -----------------------------------

/** 每个靶心节点还有多少组未揭示（节点角标用） */
export function unrevealedCountByTarget(
  groups: CaseCanvasLensGroup[],
): Map<string, number> {
  const out = new Map<string, number>()
  for (const g of groups) {
    if (g.revealed) continue
    out.set(g.target_node_id, (out.get(g.target_node_id) ?? 0) + 1)
  }
  return out
}

/** 全部揭示：仅收集清单里存在的组（不凭空长清单没有的东西） */
export function allRevealedKeys(groups: CaseCanvasLensGroup[]): Set<string> {
  return new Set(groups.map((g) => revealKey(g.target_node_id, g.lens_id)))
}

/** 清单按靶心聚合成面板分区：保持后端顺序，同靶心连续 */
export function groupRowsByTarget(
  groups: CaseCanvasLensGroup[],
): Array<{ target: string; targetLabel: string; rows: CaseCanvasLensGroup[] }> {
  const order: string[] = []
  const labels = new Map<string, string>()
  const map = new Map<string, CaseCanvasLensGroup[]>()
  for (const g of groups) {
    if (!map.has(g.target_node_id)) {
      map.set(g.target_node_id, [])
      order.push(g.target_node_id)
    }
    map.get(g.target_node_id)!.push(g)
    if (g.target_label && !labels.has(g.target_node_id)) {
      labels.set(g.target_node_id, g.target_label)
    }
  }
  return order.map((target) => ({
    target,
    targetLabel: labels.get(target) || target,
    rows: map.get(target)!,
  }))
}
