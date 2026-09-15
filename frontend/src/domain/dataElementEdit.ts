// 数据元编辑器领域逻辑（S3-F1/F2）：编码正则、format 正则校验/试匹配、
// 未知字段原样保留（R5）、表单 ↔ spec 互转、三层覆盖差异。纯函数、确定性可测。
// 红线：R1 已有项编码不可改（隐式断链）；R2 正则非法阻止保存（后端 loader 兜底）。

/** 数据元编码白名单（PRD F1.1：^DE_[A-Z_]+$） */
export const DE_NAME_RE = /^DE_[A-Z_]+$/

/** 表单渲染的字段（其余字段原样保留 + §8.6 提示，R5/D6） */
export const FORM_FIELDS = [
  'name', 'type', 'length', 'format', 'checksum', 'sensitive', 'mask', 'clean_rule',
] as const

/** 预置试匹配样例（PRD F1.2：身份证/手机号/金额各一） */
export const PRESET_SAMPLES: Array<{ label: string; value: string }> = [
  { label: '身份证', value: '110101199003071234' },
  { label: '手机号', value: '13812345678' },
  { label: '金额', value: '12345.67' },
]

export type DataElementSpec = Record<string, unknown>

/** 编码校验：返回错误文案（'' = 合法）。已有项编码不可改由 UI 置灰（R1） */
export function deNameError(key: string): string {
  const k = key.trim()
  if (!k) return '编码不能为空'
  if (!DE_NAME_RE.test(k)) return '编码须匹配 ^DE_[A-Z_]+$（如 DE_IDCARD）'
  return ''
}

/** format 正则合法性（D2：非法阻止保存；后端 loader re.compile 兜底） */
export function formatRegexError(fmt: string): string {
  const f = fmt.trim()
  if (!f) return '' // 空 = 未声明，合法
  try {
    // eslint-disable-next-line no-new
    new RegExp(f)
    return ''
  } catch (e) {
    return `🔴 正则非法：${(e as Error).message}。保存已被阻止`
  }
}

/** 试匹配（F1.2）：true=✅ 命中 / false=❌ 未命中（允许保存）/ null=无法判定 */
export function testMatch(fmt: string, sample: string): boolean | null {
  const f = fmt.trim()
  if (!f || !sample) return null
  try {
    return new RegExp(f).test(sample)
  } catch {
    return null
  }
}

/** 界面未覆盖字段（R5/§8.6）：不进表单但必须原样保留的字段名 */
export function unknownFields(spec: DataElementSpec): string[] {
  const known = FORM_FIELDS as readonly string[]
  return Object.keys(spec).filter((k) => !known.includes(k))
}

export interface ElementForm {
  key: string
  name: string
  type: string
  length: number | null
  format: string
  /** '' = 未声明 */
  checksum: string
  sensitive: boolean
  /** '' = 未声明 */
  mask: string
  /** '' = 未声明；多段 clean_rule（数组）不进表单，保持原样 */
  cleanRule: string
  /** 新建且与上层同名时才相关（v1.2 §3.0.7/P2-7） */
  override: boolean
}

/** 多段 clean_rule（op 链）在表单中只读保留 */
export function isMultiCleanRule(spec: DataElementSpec): boolean {
  return Array.isArray(spec.clean_rule) && spec.clean_rule.length > 1
}

/** spec → 表单（多段 clean_rule 显示为空并保持原样） */
export function specToForm(key: string, spec: DataElementSpec): ElementForm {
  const cr = spec.clean_rule
  const cleanList = Array.isArray(cr) ? cr.map(String) : cr ? [String(cr)] : []
  return {
    key,
    name: String(spec.name ?? ''),
    type: String(spec.type ?? 'string'),
    length: typeof spec.length === 'number' ? spec.length : null,
    format: typeof spec.format === 'string' ? spec.format : '',
    checksum: typeof spec.checksum === 'string' ? spec.checksum : '',
    sensitive: Boolean(spec.sensitive),
    mask: typeof spec.mask === 'string' ? spec.mask : '',
    cleanRule: cleanList.length === 1 ? cleanList[0] : '',
    override: spec.override === true,
  }
}

/** 表单 → spec：original 未渲染字段原样保留（R5）；表单清空的字段移除 */
export function formToSpec(
  form: ElementForm,
  original: DataElementSpec | null,
): DataElementSpec {
  const spec: DataElementSpec = { ...(original ?? {}) }
  const keepMultiClean = original !== null && isMultiCleanRule(original)
  spec.name = form.name.trim()
  spec.type = form.type
  if (form.length !== null && Number.isFinite(form.length) && form.length > 0) {
    spec.length = form.length
  } else {
    delete spec.length
  }
  if (form.format.trim()) spec.format = form.format.trim()
  else delete spec.format
  if (form.checksum) spec.checksum = form.checksum
  else delete spec.checksum
  if (form.sensitive) spec.sensitive = true
  else delete spec.sensitive
  if (form.mask) spec.mask = form.mask
  else delete spec.mask
  if (keepMultiClean) {
    // 多段 clean_rule 表单只读，原样保留（已随 original 展开带入）
  } else if (form.cleanRule) {
    spec.clean_rule = form.cleanRule
  } else {
    delete spec.clean_rule
  }
  if (form.override) spec.override = true
  else delete spec.override
  return spec
}

export interface FieldDiff {
  field: string
  upper: string
  lower: string
}

/** 覆盖差异（UC-S3-8）：下层相对上层改了哪些字段 */
export function diffOverride(
  upper: DataElementSpec | undefined,
  lower: DataElementSpec,
): FieldDiff[] {
  if (!upper) return []
  const fields = new Set([...Object.keys(upper), ...Object.keys(lower)])
  const out: FieldDiff[] = []
  for (const f of fields) {
    const a = JSON.stringify(upper[f] ?? null)
    const b = JSON.stringify(lower[f] ?? null)
    if (a !== b) out.push({ field: f, upper: String(upper[f] ?? '—'), lower: String(lower[f] ?? '—') })
  }
  return out
}

/** 全量文档内替换/新增/删除一个数据元（R5：其余条目原样保留） */
export function upsertElement(
  doc: { elements: Record<string, DataElementSpec> } | null,
  key: string,
  spec: DataElementSpec | null,
): { elements: Record<string, DataElementSpec> } {
  const elements = { ...(doc?.elements ?? {}) }
  if (spec === null) delete elements[key]
  else elements[key] = spec
  return { elements }
}
