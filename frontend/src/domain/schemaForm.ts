// S5-F2/F5 通用 schema 驱动表单领域逻辑（纯函数、确定性可测）。
// 只实现 draft-07 中本仓 19 个 schema 实际用到的子集：
//   type（含类型数组/null）、required、const、enum、pattern、minLength、
//   minimum、minItems、properties、patternProperties、additionalProperties、
//   items、oneOf/anyOf。
// F5：未声明字段（additionalProperties:false 下出现的键）不丢弃、不编辑，
//   由 collectUnknown 收集成只读 JSON 块原样展示；提交时整文档回传。
import type { JsonSchema } from '../api/endpoints/ontologyGeneric'

export interface FormError {
  /** 点分路径（数组下标用 [i]） */
  path: string
  message: string
}

export interface UnknownField {
  path: string
  key: string
  value: unknown
}

function typeName(v: unknown): string {
  if (v === null) return 'null'
  if (Array.isArray(v)) return 'array'
  if (Number.isInteger(v)) return 'integer'
  return typeof v
}

function matchesType(v: unknown, t: string): boolean {
  const actual = typeName(v)
  if (actual === t) return true
  // integer 也是 number
  if (t === 'number' && actual === 'integer') return true
  return false
}

function joinPath(base: string, seg: string | number): string {
  if (typeof seg === 'number') return `${base}[${seg}]`
  return base ? `${base}.${seg}` : seg
}

/** 按 schema 递归校验；返回全部错误（不短路，E2-2 类型冲突逐字段标红）。 */
export function validateDoc(schema: JsonSchema | null, doc: unknown): FormError[] {
  if (!schema) return []
  const errors: FormError[] = []
  validateNode(schema, doc, '', errors)
  return errors
}

function validateNode(
  schema: JsonSchema,
  value: unknown,
  path: string,
  errors: FormError[],
): void {
  // const / enum 优先
  if (Object.prototype.hasOwnProperty.call(schema, 'const')) {
    if (JSON.stringify(value) !== JSON.stringify(schema.const)) {
      errors.push({ path, message: `值必须为常量 ${JSON.stringify(schema.const)}` })
    }
    return
  }
  if (schema.enum && !schema.enum.some((o) => JSON.stringify(o) === JSON.stringify(value))) {
    errors.push({ path, message: `取值必须是：${schema.enum.map((x) => String(x)).join(' / ')}` })
    return
  }

  if (schema.oneOf) {
    const ok = schema.oneOf.some((sub) => validateDoc(sub, value).length === 0)
    if (!ok) errors.push({ path, message: '值不满足任一允许的类型组合' })
    return
  }
  if (schema.anyOf) {
    const ok = schema.anyOf.some((sub) => validateDoc(sub, value).length === 0)
    if (!ok) errors.push({ path, message: '值不满足任一允许的类型组合' })
    return
  }

  if (schema.type) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type]
    // 含 null 的联合类型且值为 null：其余约束不适用
    if (value === null) {
      if (!types.includes('null')) {
        errors.push({ path, message: `类型应为 ${types.join('|')}，实际为 null` })
      }
      return
    }
    if (!types.some((t) => matchesType(value, t))) {
      errors.push({ path, message: `类型应为 ${types.join('|')}，实际为 ${typeName(value)}` })
      return // 类型不符，后续结构校验只会制造噪声
    }
  }

  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      errors.push({ path, message: `至少 ${schema.minLength} 个字符` })
    }
    if (schema.pattern) {
      try {
        if (!new RegExp(schema.pattern).test(value)) {
          errors.push({ path, message: `格式不匹配 ${schema.pattern}` })
        }
      } catch {
        // schema pattern 非法时不阻塞编辑（后端 loader 兜底）
      }
    }
  } else if (typeof value === 'number' && schema.minimum !== undefined && value < schema.minimum) {
    errors.push({ path, message: `不能小于 ${schema.minimum}` })
  } else if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      errors.push({ path, message: `至少 ${schema.minItems} 项` })
    }
    if (schema.items) value.forEach((el, i) => validateNode(schema.items as JsonSchema, el, joinPath(path, i), errors))
  } else if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>
    const props = schema.properties ?? {}
    const hasObjectShape = Boolean(
      schema.properties ||
        schema.patternProperties ||
        (schema.additionalProperties !== undefined && schema.additionalProperties !== true),
    )
    if (!hasObjectShape) return
    for (const req of schema.required ?? []) {
      if (obj[req] === undefined) {
        errors.push({ path: joinPath(path, req), message: '必填字段缺失' })
      }
    }
    for (const [key, sub] of Object.entries(props)) {
      if (obj[key] !== undefined) validateNode(sub, obj[key], joinPath(path, key), errors)
    }
    // additionalProperties:false → 未声明键（且不匹配 patternProperties）报错
    if (schema.additionalProperties === false) {
      const patterns = Object.keys(schema.patternProperties ?? {}).map((pat) => {
        try {
          return new RegExp(pat)
        } catch {
          return null
        }
      })
      for (const key of Object.keys(obj)) {
        if (Object.prototype.hasOwnProperty.call(schema.properties, key)) continue
        if (patterns.some((re) => re?.test(key))) continue
        errors.push({ path: joinPath(path, key), message: '未在 schema 中声明的字段（additionalProperties:false）' })
      }
    } else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
      // 映射型（additionalProperties 是 schema）
      const declared = new Set(Object.keys(props))
      for (const [key, v] of Object.entries(obj)) {
        if (!declared.has(key)) {
          validateNode(schema.additionalProperties, v, joinPath(path, key), errors)
        }
      }
    }
    if (schema.patternProperties) {
      for (const [pat, sub] of Object.entries(schema.patternProperties)) {
        let re: RegExp
        try {
          re = new RegExp(pat)
        } catch {
          continue
        }
        for (const [key, v] of Object.entries(obj)) {
          if (re.test(key)) validateNode(sub, v, joinPath(path, key), errors)
        }
      }
    }
  }
}

/**
 * F5：收集「schema 未声明、但实际文档里存在」的字段（含嵌套）。
 * 这些字段只读展示、提交时原样回传，绝不静默丢失。
 */
export function collectUnknown(
  schema: JsonSchema | null,
  doc: unknown,
): UnknownField[] {
  if (!schema || !doc || typeof doc !== 'object') return []
  const out: UnknownField[] = []
  walkUnknown(schema, doc, '', out)
  return out
}

function walkUnknown(
  schema: JsonSchema,
  value: unknown,
  path: string,
  out: UnknownField[],
): void {
  if (!value || typeof value !== 'object') return
  if (Array.isArray(value)) {
    if (schema.items) value.forEach((el, i) => walkUnknown(schema.items as JsonSchema, el, joinPath(path, i), out))
    return
  }
  const obj = value as Record<string, unknown>
  const props = schema.properties ?? {}
  for (const [key, v] of Object.entries(obj)) {
    const childPath = joinPath(path, key)
    if (Object.prototype.hasOwnProperty.call(props, key)) {
      walkUnknown(props[key], v, childPath, out)
      continue
    }
    const patEntry = Object.entries(schema.patternProperties ?? {}).find(([pat]) => {
      try {
        return new RegExp(pat).test(key)
      } catch {
        return false
      }
    })
    if (patEntry) {
      walkUnknown(patEntry[1], v, childPath, out)
      continue
    }
    if (schema.additionalProperties === false) {
      out.push({ path: childPath, key, value: v })
    } else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
      walkUnknown(schema.additionalProperties, v, childPath, out)
    }
    // additionalProperties: true / 缺省 → 自由键，不视为未知
  }
}

/** 「新增条目」时按 schema 构造默认值（required 链递归补齐）。 */
export function buildDefault(schema: JsonSchema): unknown {
  if (Object.prototype.hasOwnProperty.call(schema, 'const')) return schema.const
  if (schema.enum && schema.enum.length) return schema.enum[0]
  if (schema.default !== undefined) return schema.default
  if (schema.oneOf?.length) return buildDefault(schema.oneOf[0])
  if (schema.anyOf?.length) return buildDefault(schema.anyOf[0])
  const type = Array.isArray(schema.type) ? schema.type.find((t) => t !== 'null') ?? schema.type[0] : schema.type
  switch (type) {
    case 'object': {
      const out: Record<string, unknown> = {}
      for (const key of schema.required ?? []) {
        const sub = schema.properties?.[key]
        if (sub) out[key] = buildDefault(sub)
      }
      return out
    }
    case 'array':
      return []
    case 'integer':
    case 'number':
      return null
    case 'boolean':
      return false
    case 'string':
    default:
      return ''
  }
}

/** 是否渲染为多行文本（启发式：键名/描述含 SQL、长文本、note 等）。 */
export function isLongText(key: string, schema: JsonSchema | null): boolean {
  if (/(_sql|sql|rule_text|^text$|falsification|description|_note$|^note$|reason)/.test(key)) {
    return true
  }
  return Boolean(schema?.minLength && schema.minLength >= 20)
}

/** 标量原始值编辑辅助：NInput 永远产出字符串，提交前按 schema 转回数字。 */
export function coerceScalar(raw: string, schema: JsonSchema | null): unknown {
  const type = schema?.type
  const types = Array.isArray(type) ? type : type ? [type] : []
  if (raw === '' && types.includes('null')) return null
  if (types.includes('integer') && /^-?\d+$/.test(raw)) return Number(raw)
  if (types.includes('number') && raw.trim() !== '' && !Number.isNaN(Number(raw))) {
    return Number(raw)
  }
  return raw
}
