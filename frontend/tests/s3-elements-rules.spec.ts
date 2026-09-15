import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import {
  DE_NAME_RE, PRESET_SAMPLES, deNameError, diffOverride, formatRegexError,
  formToSpec, isMultiCleanRule, specToForm, testMatch, unknownFields, upsertElement,
  type ElementForm,
} from '../src/domain/dataElementEdit'
import { FIVE_JIAN } from '../src/api/endpoints/model'

// S3 数据元与规则建模：
//  - F1 数据元编辑器领域逻辑（编码正则/format 试匹配/表单互转/未知字段保留）；
//  - F2 三层视图（全域/行业/案件 + 覆盖差异）；
//  - F3 规则表单化（jian_types 五勾选解锁 + params 表格 + RESCAN 语义）；
//  - F4 函数声明只读可视化（D7/E4-1：无编辑控件，不假装可编辑）。
// 视图层用结构断言（与 config-redlines 同口径），领域逻辑用纯函数直测。

const here = dirname(fileURLToPath(import.meta.url))
const view = (p: string) => resolve(here, '..', 'src', 'views', p)
const ep = (p: string) => resolve(here, '..', 'src', 'api', 'endpoints', p)
const src = (p: string) => readFileSync(p, 'utf8')

// ---------------------------------------------------------------- S3-F1 领域逻辑
describe('S3-F1 数据元编辑器领域逻辑', () => {
  it('编码白名单 ^DE_[A-Z_]+$：合法/非法/空', () => {
    expect(DE_NAME_RE.test('DE_IDCARD')).toBe(true)
    expect(DE_NAME_RE.test('DE_BANK_ACCT_NO')).toBe(true)
    expect(DE_NAME_RE.test('de_idcard')).toBe(false)
    expect(DE_NAME_RE.test('DE_1ABC')).toBe(false)
    expect(deNameError('DE_OK')).toBe('')
    expect(deNameError('bad_name')).toContain('DE_')
    expect(deNameError('')).toContain('不能为空')
  })

  it('format 正则校验：合法/非法/留空（D2 非法阻止保存）', () => {
    expect(formatRegexError('^1[3-9]\\d{9}$')).toBe('')
    expect(formatRegexError('')).toBe('')
    expect(formatRegexError('   ')).toBe('')
    const bad = formatRegexError('^1[3-9')
    expect(bad).toContain('正则非法')
    expect(bad).toContain('阻止')
  })

  it('试匹配：命中/未命中/无法判定', () => {
    expect(testMatch('^1[3-9]\\d{9}$', '13812345678')).toBe(true)
    expect(testMatch('^1[3-9]\\d{9}$', '12345')).toBe(false)
    expect(testMatch('^1[3-9]\\d{9}$', '')).toBe(null)
    expect(testMatch('', '138')).toBe(null)
    expect(testMatch('^1[3-9', '138')).toBe(null)
  })

  it('预置试匹配样例齐备（身份证/手机号/金额）', () => {
    expect(PRESET_SAMPLES.map((p) => p.label)).toEqual(['身份证', '手机号', '金额'])
  })

  it('specToForm/formToSpec 往返：声明字段进表单，未知字段原样保留（R5）', () => {
    const spec = {
      name: '公民身份号码', type: 'string', length: 18,
      format: '^\\d{18}$', checksum: 'idcard_mod11', sensitive: true,
      mask: 'partial', clean_rule: 'despace', range: { min: 0 },
    }
    const form = specToForm('DE_IDCARD', spec)
    expect(form.key).toBe('DE_IDCARD')
    expect(form.name).toBe('公民身份号码')
    expect(form.length).toBe(18)
    expect(form.sensitive).toBe(true)
    expect(unknownFields(spec)).toEqual(['range'])

    const built = formToSpec(form, spec)
    expect(built.range).toEqual({ min: 0 }) // 未知字段不丢
    expect(built.name).toBe('公民身份号码')
  })

  it('多段 clean_rule（op 链）表单只读保留，不误删', () => {
    const spec = { name: '金额', type: 'decimal', clean_rule: ['strip_thousands', 'strip_currency'] }
    expect(isMultiCleanRule(spec)).toBe(true)
    const form = specToForm('DE_AMOUNT', spec)
    expect(form.cleanRule).toBe('') // 多段不进表单
    const built = formToSpec(form, spec)
    expect(built.clean_rule).toEqual(['strip_thousands', 'strip_currency'])
  })

  it('表单清空字段 → spec 移除该键；override 勾选显式落 spec', () => {
    const original = { name: 'n', type: 'string', length: 11, format: '^1\\d{10}$', sensitive: true }
    const form: ElementForm = {
      key: 'DE_X', name: 'n', type: 'string', length: null, format: '',
      checksum: '', sensitive: false, mask: '', cleanRule: '', override: true,
    }
    const built = formToSpec(form, original)
    expect('length' in built).toBe(false)
    expect('format' in built).toBe(false)
    expect('sensitive' in built).toBe(false)
    expect(built.override).toBe(true)
  })

  it('覆盖差异（UC-S3-8）：下层相对上层改了哪些字段；上层缺失返回空', () => {
    const upper = { name: '手机号', type: 'string', length: 11 }
    const lower = { name: '手机号', type: 'string', length: 13, format: '^1\\d{10}$' }
    const diffs = diffOverride(upper, lower)
    const fields = diffs.map((d) => d.field).sort()
    expect(fields).toEqual(['format', 'length'])
    const len = diffs.find((d) => d.field === 'length')
    expect(len?.upper).toBe('11')
    expect(len?.lower).toBe('13')
    expect(diffOverride(undefined, lower)).toEqual([])
  })

  it('upsertElement：全量文档内替换/新增/删除，其余条目原样', () => {
    const doc = { elements: { DE_A: { name: 'a' }, DE_B: { name: 'b' } } }
    const out = upsertElement(doc, 'DE_A', { name: 'a2' })
    expect(out.elements.DE_A).toEqual({ name: 'a2' })
    expect(out.elements.DE_B).toEqual({ name: 'b' })
    const added = upsertElement(doc, 'DE_C', { name: 'c' })
    expect(added.elements.DE_C).toEqual({ name: 'c' })
    const removed = upsertElement(doc, 'DE_A', null)
    expect('DE_A' in removed.elements).toBe(false)
  })
})

// ---------------------------------------------------------------- S3-F2 三层视图
describe('S3-F2 数据元三层视图（结构断言）', () => {
  it('三层 tab：全域/行业/本案件，无行业层降级不报错', () => {
    const vue = src(view('DataElementsView.vue'))
    expect(vue).toContain("activeLayer = ref<Layer>('case')")
    expect(vue).toContain("'shared' | 'industry' | 'case'")
    expect(vue).toContain('全域')
    expect(vue).toContain('行业')
    expect(vue).toContain('本案件')
  })

  it('层写权限：案件层偏将+；全域/行业层本体管理员双条件（后端门禁兜底）', () => {
    const vue = src(view('DataElementsView.vue'))
    expect(vue).toContain('auth.isOntologyAdmin')
    expect(vue).toContain('canWriteLayer')
  })

  it('试匹配联动 + 覆盖标注 + 引用扫描接线', () => {
    const vue = src(view('DataElementsView.vue'))
    expect(vue).toContain('testMatch')
    expect(vue).toContain('PRESET_SAMPLES')
    expect(vue).toContain('caseOverrides')
    expect(vue).toContain('overrideDiffs')
    expect(vue).toContain('refsByElement')
  })
})

// ---------------------------------------------------------------- S3-F3 规则表单化
describe('S3-F3 规则工坊表单化（结构断言）', () => {
  it('间类五勾选：NCheckboxGroup × FIVE_JIAN，锁定键不再含 jian_types', () => {
    const vue = src(view('RuleWorkshopView.vue'))
    expect(vue).toContain('NCheckboxGroup')
    expect(vue).toContain('jianOptions = FIVE_JIAN.map')
    // 出网前过白名单摘取（结构字段 L0 剥离）
    expect(vue).toContain('sanitizeEditBody')
    expect(vue).toContain('validateRuleEdit')
    // 间类校验兜底接线
    expect(vue).toContain('jianTypesError')
  })

  it('参数表格按函数声明渲染：paramRows + 未声明参数标红', () => {
    const vue = src(view('RuleWorkshopView.vue'))
    expect(vue).toContain('paramRows')
    expect(vue).toContain('未声明')
    expect(vue).toContain('params-table')
  })

  it('RESCAN 语义：params/enabled/jian_types 变更入队提示，纯文本修订不重跑', () => {
    const vue = src(view('RuleWorkshopView.vue'))
    expect(vue).toContain('triggersRescan')
    expect(vue).toContain('已入队 RESCAN')
  })
})

// ---------------------------------------------------------------- S3-F4 函数只读可视化
describe('S3-F4 函数声明只读可视化', () => {
  it('FunctionsView：只读目录，无写调用（D7/E4-1 不假装可编辑）', () => {
    const vue = src(view('FunctionsView.vue'))
    expect(vue).toContain('functionsApi')
    expect(vue).toContain('🔒 只读')
    expect(vue).toContain('requires')
    expect(vue).toContain('inputs')
    // sql 实现文本不出现在展示面（后端已 pop）
    expect(vue).not.toMatch(/\.sql/)
    // 无任何写端点调用（api.put/post）
    expect(vue).not.toContain('api.put')
    expect(vue).not.toContain('api.post')
  })

  it('本体管理器壳挂载 functions：只读文件 + 只读编辑器并存', () => {
    const vue = src(view('OntologyManagerView.vue'))
    expect(vue).toContain("functions: () => import('../views/FunctionsView.vue')")
    // S5 起只读集合扩 llm_policy（R6：只读 JSON、永不渲染表单）
    expect(vue).toContain("'functions', 'llm_policy'")
    expect(vue).toContain('READONLY_EDITORS.has(selected.value.name)')
  })

  it('functions API 契约：GET only + sql 不在声明类型中', () => {
    const ts = src(ep('functions.ts'))
    expect(ts).toContain('api.get')
    expect(ts).not.toContain('api.put')
    expect(ts).not.toContain('api.post')
    expect(ts).toMatch(/interface FunctionDecl/)
    expect(ts).toMatch(/requires\?/)
  })
})

// ---------------------------------------------------------------- S3 契约面
describe('S3 数据元 API 契约（端点齐备）', () => {
  it('enums/references/shared/industry 读写端点全接线', () => {
    const ts = src(ep('dataElements.ts'))
    for (const path of [
      '/data-elements/enums',
      '/data-elements/references',
      '/data-elements-shared',
      '/data-elements-industry',
    ]) {
      expect(ts).toContain(path)
    }
  })

  it('五间常量与后端 jians.json 白名单同源', () => {
    expect([...FIVE_JIAN].sort()).toEqual(['内间', '反间', '因间', '死间', '生间'].sort())
  })
})
