/**
 * 主体选择器纯逻辑（CAN-13 / CAN-14）。
 *
 * 为什么抽成 domain 而不是写在 .vue 里
 * -----------------------------------
 * 选择器有三条会咬人的规则（重名必选、自由输入必标未锚定、空名录要说明
 * 原因），它们与渲染无关，却最容易被写错。抽出来可在 node 下真跑断言——
 * 沙盒装不上 vitest 时，"看着没问题"不算验证。
 */

/**
 * 名录条目：字段与后端 GET /cases/{cid}/canvas/subjects 对齐
 * （api/endpoints/canvas.ts 的 SubjectEntry；结构化类型保证可直入）。
 *
 * 一个姓名可能对应多个主键（同名异人），也可能只有 1 个候选但身份
 * 证据互斥（homonym_pending，实体未分列）——歧义判定一律以后端
 * person_pk_ambiguous 为准，前端不得自己数候选数。
 */
export interface SubjectEntry {
  name: string
  sub_type?: string
  /** 关系型指代（"张卫国配偶"）：非独立主体，不作为研判主体 */
  relational?: boolean
  /** 唯一候选主键；多候选时为 null，待分列时只是"暂用"，不得直接当锚定用 */
  person_pk?: string | null
  /** 后端已判指代不明（多候选已分列，或同名异人待分列） */
  person_pk_ambiguous?: boolean
  /** multi | homonym_pending | unique | none */
  pk_status?: string | null
  pk_candidates?: string[]
  /** 后端裁决说明（关系型指代时承载原因） */
  pk_resolution?: string
}

export interface SubjectSelection {
  name: string
  person_pk: string | null
  ambiguous: boolean
  anchored: boolean
  /** 名录外手打：已知未锚定，仍可建节点但必须显式标注 */
  manual: boolean
  reason: string
}

export type SubjectPickStatus =
  | 'ok'                 // 已锚定，可发起研判
  | 'need_disambiguate'  // 同名异人，须在候选里选一个
  | 'unanchored'         // 名录外手打，可建但无可用研判
  | 'empty'              // 空输入

/**
 * 搜索：子串命中、大小写不敏感、去重保序；空查询返回前 limit 条。
 *
 * 去重按 name——名录里同名多条已在 pks 里合并，不重复占位。
 */
export function filterCandidates(
  entries: SubjectEntry[],
  query: string,
  limit = 50,
): SubjectEntry[] {
  const q = String(query ?? '').trim().toLowerCase()
  const seen = new Set<string>()
  const out: SubjectEntry[] = []
  for (const e of entries ?? []) {
    const nm = String(e?.name ?? '').trim()
    if (!nm || seen.has(nm)) continue
    if (q && !nm.toLowerCase().includes(q)) continue
    seen.add(nm)
    out.push(e)
    if (out.length >= limit) break
  }
  return out
}

/**
 * 依据"选中的名录条目 + 可选的指定主键"解析出最终主体。
 *
 * R-1 重名不自裁：以后端 person_pk_ambiguous 判定为准——多候选已分列
 *     固然歧义，同名异人待分列（homonym_pending）时候选可能只有 1 个，
 *     照候选数判断会静默错锚定。歧义时 person_pk 置 null，绝不取第一个。
 * R-2 手打名录外姓名：允许建节点（走访听到的人确实可能不在库里），
 *     但必须 anchored=false + manual=true，由界面写明"无可用研判"。
 * R-4 关系型指代（"张卫国配偶"）不是独立主体：可建描述性节点，
 *     但原因照后端口径写明，不得显示成"无主键的普通人"。
 */
export function resolveSelection(
  entry: SubjectEntry | null,
  manualName: string,
  requestedPk?: string | null,
): SubjectSelection {
  const typed = String(manualName ?? '').trim()

  if (entry) {
    const nm = String(entry.name ?? '').trim()
    const cands = (entry.pk_candidates ?? []).filter(Boolean)
    // 人工裁决优先：pk 必须落在候选集合内才接受，防拿 A 的候选去锚 B
    if (requestedPk && cands.includes(requestedPk)) {
      return {
        name: nm, person_pk: requestedPk, ambiguous: false,
        anchored: true, manual: false,
        reason: '已按人工指定锚定主键',
      }
    }
    // 关系型指代（"张卫国配偶"）：非独立主体，不作为研判主体（后端口径）
    if (entry.relational) {
      const why = String(entry.pk_resolution ?? '').trim()
      return {
        name: nm, person_pk: null, ambiguous: false, anchored: false,
        manual: true,
        reason: why || `「${nm}」是关系型指代（非独立主体），不作为研判主体`,
      }
    }
    if (entry.person_pk_ambiguous || cands.length > 1) {
      return {
        name: nm, person_pk: null, ambiguous: true, anchored: false,
        manual: false,
        reason: cands.length > 1
          ? `「${nm}」同名异人（${cands.length} 个候选主键），须选择其一后再研判`
          : `「${nm}」存在同名异人（身份证据互斥、实体未分列），须裁决归属后再研判`,
      }
    }
    if (cands.length === 1) {
      return {
        name: nm, person_pk: cands[0], ambiguous: false, anchored: true,
        manual: false, reason: '已锚定唯一主键',
      }
    }
    // 名录里有名但无主键：与手打同处理，不假装锚定
    return {
      name: nm, person_pk: null, ambiguous: false, anchored: false,
      manual: true,
      reason: `「${nm}」在名录中但无语义层主键，未锚定，无可用研判`,
    }
  }

  if (!typed) {
    return {
      name: '', person_pk: null, ambiguous: false, anchored: false,
      manual: false, reason: '',
    }
  }
  return {
    name: typed, person_pk: null, ambiguous: false, anchored: false,
    manual: true,
    reason: `「${typed}」不在主体名录中，未锚定，无可用研判（可先建节点，补证后再锚定）`,
  }
}

export function pickStatus(sel: SubjectSelection): SubjectPickStatus {
  if (!sel.name) return 'empty'
  if (sel.ambiguous) return 'need_disambiguate'
  if (!sel.anchored) return 'unanchored'
  return 'ok'
}

/** 是否可提交：ok 与 unanchored 都可建节点，歧义必须先裁决 */
export function canSubmit(sel: SubjectSelection): boolean {
  return pickStatus(sel) !== 'empty' && pickStatus(sel) !== 'need_disambiguate'
}

/** 提交给后端的 props（与 canvas_case.build_subject_node 字段对齐） */
export function buildSubjectProps(
  sel: SubjectSelection,
  subType: SubjectEntry['sub_type'] = 'person',
): Record<string, unknown> {
  return {
    name: sel.name,
    person_pk: sel.person_pk,
    person_pk_ambiguous: sel.ambiguous,
    pk_candidates: sel.person_pk ? [sel.person_pk] : [],
    anchored: sel.anchored,
    sub_type: subType ?? 'person',
    pk_resolution: sel.reason,
  }
}
