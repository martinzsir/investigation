/**
 * 物品登记纯逻辑（ITM 画布层）。
 *
 * 为什么抽成 domain 而不是写在 .vue 里
 * -----------------------------------
 * 登记入口有三条会咬人的规则：无凭证不静默合并、半截坐标不塞 0、
 * 敏感标识符明文不入库。它们与渲染无关却极易写错，抽出来可在 node 下
 * 真跑断言——沙盒装不上 vitest 时，"看着没问题"不算验证。
 */

export type ItemTypeCode =
  | 'vehicle'
  | 'realestate'
  | 'invoice'
  | 'drug_batch'
  | 'device'
  | 'portable'
  | 'goods_other'

export interface ItemTypeOption {
  code: ItemTypeCode
  label: string
}

/**
 * 物品类型。与后端 ITEM_TYPE_LABELS 对齐（后端为权威，未知类型会 400
 * 并给出允许列表，所以前端少列一类只是"不提供"，不会静默错位）。
 */
export const ITEM_TYPES: ItemTypeOption[] = [
  { code: 'vehicle', label: '车辆' },
  { code: 'realestate', label: '房产' },
  { code: 'invoice', label: '票据' },
  { code: 'drug_batch', label: '药品批号' },
  { code: 'device', label: '设备' },
  { code: 'portable', label: '便携物' },
  { code: 'goods_other', label: '其他物品' },
]

export interface IdentifierOption {
  kind: string
  label: string
  /** 属个人信息/可定位设备：明文不入库，仅存摘要 */
  sensitive: boolean
}

export const ITEM_IDENTIFIER_OPTIONS: IdentifierOption[] = [
  { kind: 'plate', label: '车牌号', sensitive: false },
  { kind: 'property_cert', label: '不动产权证号', sensitive: false },
  { kind: 'invoice_code_no', label: '发票代码+号码', sensitive: false },
  { kind: 'batch_no', label: '药品批号', sensitive: false },
  { kind: 'serial', label: '出厂序列号', sensitive: false },
  { kind: 'msisdn', label: '手机号码', sensitive: true },
  { kind: 'imei', label: '设备识别码', sensitive: true },
  { kind: 'none', label: '无凭证（仅描述）', sensitive: false },
]

export interface ItemIdentifierDraft {
  kind: string
  value: string
}

export interface ItemDraft {
  title: string
  itemType: ItemTypeCode
  identifiers: ItemIdentifierDraft[]
  /** 无凭证物品靠特征描述区分（"银色 U 盘、内有账本扫描件"） */
  descriptorText: string
  lat: string
  lng: string
  holderRaw: string
}

export type ItemStatus =
  | 'empty' // 什么都填不出来：提交必然被后端拒
  | 'ok' // 有凭证，可参与消歧与研判
  | 'unanchored' // 无凭证，只能作描述性实体

/** 有效凭证：种类不是 none 且值非空。none 是"明确无凭证"，不算凭证。 */
export function filledIdentifiers(draft: ItemDraft): ItemIdentifierDraft[] {
  return (draft?.identifiers ?? []).filter(
    (i) => String(i?.kind ?? '') !== 'none' && String(i?.value ?? '').trim() !== '',
  )
}

export function hasCredential(draft: ItemDraft): boolean {
  return filledIdentifiers(draft).length > 0
}

export function hasDescriptor(draft: ItemDraft): boolean {
  return String(draft?.descriptorText ?? '').trim() !== ''
}

/**
 * 提交可行性。
 *
 * 无凭证**且**无特征描述时直接拦在前端：后端 build_item_node 会拒绝
 * （否则所有无凭证物品会共用一个空摘要，被静默合并成同一个实体）。
 * 但这条必须在前端就拦住——后端拒是 400，正兵看到的是通用错误提示，
 * 而实际只是他忘了填凭证或特征。
 */
export function itemStatus(draft: ItemDraft): ItemStatus {
  if (!hasCredential(draft) && !hasDescriptor(draft)) return 'empty'
  return hasCredential(draft) ? 'ok' : 'unanchored'
}

export function canSubmitItem(draft: ItemDraft): boolean {
  return itemStatus(draft) !== 'empty'
}

export interface CoordResult {
  lat?: number
  lng?: number
  /** 只填了一半：按"都没有"处理，绝不把缺的那一个补成 0 */
  partial: boolean
}

/**
 * 坐标归一化。
 *
 * 半截坐标（只填纬度）不会报错——补 0 会把"不知道在哪"画成"在几内亚湾"，
 * 而地图天生看上去精确，正兵无从判断这是真坐标还是补出来的。
 * 所以要么成对传，要么都不传。
 */
export function normalizeCoord(latRaw: string, lngRaw: string): CoordResult {
  const la = String(latRaw ?? '').trim()
  const ln = String(lngRaw ?? '').trim()
  if (!la && !ln) return { partial: false }
  if (!la || !ln) return { partial: true }
  const lat = Number(la)
  const lng = Number(ln)
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return { partial: true }
  return { lat, lng, partial: false }
}

/** 提交体：与 POST /cases/{cid}/case-canvas/items 的入参对齐 */
export function buildItemPayload(draft: ItemDraft): Record<string, unknown> {
  const c = normalizeCoord(draft.lat, draft.lng)
  const body: Record<string, unknown> = {
    title: String(draft?.title ?? '').trim(),
    item_type: draft?.itemType ?? 'goods_other',
    identifiers: filledIdentifiers(draft).map((i) => ({
      kind: i.kind,
      value: String(i.value).trim(),
    })),
    descriptors: hasDescriptor(draft)
      ? { description: String(draft.descriptorText).trim() }
      : {},
  }
  if (c.lat !== undefined && c.lng !== undefined) {
    body.lat = c.lat
    body.lng = c.lng
  }
  const holder = String(draft?.holderRaw ?? '').trim()
  if (holder) body.holder_raw = holder
  return body
}

/** 命中敏感种类：界面需写明"明文不入库、仅存摘要" */
export function sensitiveKinds(draft: ItemDraft): string[] {
  const kinds = new Set(filledIdentifiers(draft).map((i) => i.kind))
  return ITEM_IDENTIFIER_OPTIONS.filter((o) => o.sensitive && kinds.has(o.kind)).map(
    (o) => o.label,
  )
}
