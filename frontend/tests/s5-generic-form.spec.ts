import { describe, expect, it } from 'vitest'

import {
  buildDefault,
  coerceScalar,
  collectUnknown,
  validateDoc,
} from '../src/domain/schemaForm'
import {
  canDiscardProposal,
  canPublishProposal,
  impactBlocksPublish,
  impactWarnings,
  isZeroImpact,
  solidifiedClues,
  uncertainItems,
} from '../src/domain/impact'
import type { JsonSchema } from '../src/api/endpoints/ontologyGeneric'
import type { ImpactResult, ProposalDto } from '../src/api/endpoints/ontologyGeneric'

// S5 通用表单（F2/F5）+ 影响面（F3）+ 提案门禁（F4）领域纯函数。
// 视图层交互（naive-ui 组件/弹窗）由人工验收覆盖，这里只测确定性门禁。

const ENUM_SPACE_SCHEMA: JsonSchema = {
  type: 'object',
  required: ['schema_version', 'space'],
  properties: {
    schema_version: { const: 2 },
    _note: { type: 'string' },
    space: {
      type: 'object',
      additionalProperties: { type: 'array', items: { type: 'string' } },
    },
  },
  additionalProperties: false,
}

const PLAYBOOK_SCHEMA: JsonSchema = {
  type: 'object',
  required: ['schema_version', 'playbooks'],
  properties: {
    schema_version: { const: 1 },
    playbooks: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'channel', 'text'],
        properties: {
          id: { type: 'string', minLength: 1 },
          channel: { enum: ['function', 'external'] },
          text: { type: 'string', minLength: 1 },
          assumption: {
            oneOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }],
          },
        },
        additionalProperties: true,
      },
    },
  },
  additionalProperties: false,
}

// ---- F2：校验 ---------------------------------------------------------------
describe('schemaForm.validateDoc', () => {
  it('合法 enum_space 文档零错误', () => {
    const doc = { schema_version: 2, space: { 动机: ['贪利', '徇情'] }, _note: 'x' }
    expect(validateDoc(ENUM_SPACE_SCHEMA, doc)).toEqual([])
  })

  it('const / required / 元素类型 / 枚举越界逐一报错（E2-2 类型冲突）', () => {
    const errs = validateDoc(ENUM_SPACE_SCHEMA, { space: { 动机: ['贪利', 3] } })
    const paths = errs.map((e) => e.path)
    expect(paths).toContain('schema_version') // const 缺失且值不等
    expect(paths.some((p) => p.startsWith('space.动机[1]'))).toBe(true)
  })

  it('顶层 additionalProperties:false 拦未知根键，但条目级自由键放行', () => {
    const bad = validateDoc(ENUM_SPACE_SCHEMA, {
      schema_version: 2,
      space: {},
      rogue_root: 1,
    })
    expect(bad.some((e) => e.path === 'rogue_root')).toBe(true)

    const playErrs = validateDoc(PLAYBOOK_SCHEMA, {
      schema_version: 1,
      playbooks: [
        { id: 'pb1', channel: 'function', text: 't', custom_key: { whatever: true } },
      ],
    })
    expect(playErrs).toEqual([])
  })

  it('oneOf：assumption 接受 string 或 string[]，拒绝数字', () => {
    const ok1 = validateDoc(PLAYBOOK_SCHEMA, {
      schema_version: 1,
      playbooks: [{ id: 'p', channel: 'external', text: 't', assumption: 'a' }],
    })
    const ok2 = validateDoc(PLAYBOOK_SCHEMA, {
      schema_version: 1,
      playbooks: [{ id: 'p', channel: 'external', text: 't', assumption: ['a', 'b'] }],
    })
    const bad = validateDoc(PLAYBOOK_SCHEMA, {
      schema_version: 1,
      playbooks: [{ id: 'p', channel: 'external', text: 't', assumption: 3 }],
    })
    expect(ok1).toEqual([])
    expect(ok2).toEqual([])
    expect(bad.some((e) => e.path.includes('assumption'))).toBe(true)
  })

  it('联合类型 number|string|null 三态均合法', () => {
    const sch: JsonSchema = {
      type: 'object',
      properties: { range: { type: 'object', properties: { min: { type: ['number', 'string'] } } } },
    }
    expect(validateDoc(sch, { range: { min: 0 } })).toEqual([])
    expect(validateDoc(sch, { range: { min: '0~100' } })).toEqual([])
    expect(validateDoc(sch, { range: { min: true } }).length).toBeGreaterThan(0)
  })
})

// ---- F5：未知字段兜底 --------------------------------------------------------
describe('schemaForm.collectUnknown', () => {
  it('收集顶层未声明键（原样保留、只读展示）', () => {
    const unk = collectUnknown(ENUM_SPACE_SCHEMA, {
      schema_version: 2,
      space: {},
      future_field: { x: 1 },
    })
    expect(unk.map((u) => u.path)).toEqual(['future_field'])
    expect(unk[0].value).toEqual({ x: 1 })
  })

  it('映射型 additionalProperties 的键不算未知；条目级 additionalProperties:true 也不算', () => {
    expect(collectUnknown(ENUM_SPACE_SCHEMA, { schema_version: 2, space: { 任意键: ['a'] } })).toEqual([])
    const unk = collectUnknown(PLAYBOOK_SCHEMA, {
      schema_version: 1,
      playbooks: [{ id: 'p', channel: 'function', text: 't', 自由键: 1 }],
    })
    expect(unk).toEqual([])
  })
})

// ---- F2：默认值 / 标量还原 ---------------------------------------------------
describe('schemaForm.buildDefault / coerceScalar', () => {
  it('新增对象条目时按 required 链补齐', () => {
    const d = buildDefault(PLAYBOOK_SCHEMA.properties!.playbooks!.items!) as Record<string, unknown>
    expect(d.id).toBe('')
    expect(d.channel).toBe('function') // enum 取首项
    expect(d.text).toBe('')
  })

  it('coerceScalar 按 schema 把文本还原为数字，null 联合支持空串→null', () => {
    expect(coerceScalar('12', { type: 'integer' })).toBe(12)
    expect(coerceScalar('1.5', { type: 'number' })).toBe(1.5)
    expect(coerceScalar('', { type: ['integer', 'null'] })).toBeNull()
    expect(coerceScalar('abc', { type: 'integer' })).toBe('abc')
  })
})

// ---- F3：影响面聚合/警示 -----------------------------------------------------
function mkImpact(over: Partial<ImpactResult> = {}): ImpactResult {
  const cat = () => ({ status: 'ok' as const, items: [] })
  return {
    file: 'rules',
    status: 'complete',
    changes: [],
    categories: { clues: cat(), rules: cat(), views: cat(), tables: cat(), functions: cat() },
    dynamic_refs: [],
    failures: [],
    totals: {},
    coverage_note: '覆盖说明',
    ...over,
  }
}

describe('impact domain', () => {
  it('已固证线索单独计数（R2），uncertain 项独立聚合', () => {
    const imp = mkImpact({
      categories: {
        clues: {
          status: 'ok',
          items: [
            { category: 'clues', kind: 'clue', id: 'c1', title: '线索1', certainty: 'certain', refs: ['R1'], via: '', status: '已固证', solidified: true },
            { category: 'clues', kind: 'clue', id: 'c2', title: '线索2', certainty: 'uncertain', refs: ['x'], via: '', status: '待查', solidified: false },
          ],
        },
        rules: { status: 'ok', items: [] },
        views: { status: 'ok', items: [] },
        tables: { status: 'ok', items: [] },
        functions: {
          status: 'ok',
          items: [
            { category: 'functions', kind: 'function', id: 'f_bad_degree', title: 'f', certainty: 'uncertain', refs: ['obj_x'], via: '', status: null, solidified: false },
          ],
        },
      },
    })
    expect(solidifiedClues(imp).map((i) => i.id)).toEqual(['c1'])
    expect(uncertainItems(imp).map((i) => i.id).sort()).toEqual(['c2', 'f_bad_degree'])
    const warns = impactWarnings(imp).map((w) => w.text)
    expect(warns.some((t) => t.includes('已固证'))).toBe(true)
    expect(warns.some((t) => t.includes('不确定引用'))).toBe(true)
  })

  it('零命中 complete 才是真空；partial 有失败类不算零', () => {
    expect(isZeroImpact(mkImpact())).toBe(true)
    const partial = mkImpact({
      status: 'partial',
      failures: [{ category: 'clues', reason: '产物损坏' }],
    })
    expect(isZeroImpact(partial)).toBe(false)
    const warns = impactWarnings(partial).map((w) => w.level)
    expect(warns).toContain('danger')
  })

  it('D10：failed 影响面阻断发布', () => {
    expect(impactBlocksPublish(mkImpact({ status: 'failed' }))).toBe(true)
    expect(impactBlocksPublish(null)).toBe(true)
    expect(impactBlocksPublish(mkImpact({ status: 'partial' }))).toBe(false)
  })
})

// ---- F4：提案状态机门禁（前端同口径，硬门禁在后端 E4-1/E4-3）------------------
describe('proposal gating', () => {
  function mkProposal(p: Partial<ProposalDto>): ProposalDto {
    return {
      proposal_id: 'op-1',
      file: 'rules',
      status: 'draft',
      reason: 'r',
      author: 'zhang',
      created_at: '',
      updated_at: '',
      published_at: null,
      published_by: null,
      discarded_by: null,
      impact: null,
      ...p,
    }
  }

  it('draft 不能发布（E4-1：未看影响面）；impact_ready + complete 可发布', () => {
    expect(canPublishProposal(mkProposal({}))).toBe(false)
    const ready = mkProposal({ status: 'impact_ready', impact: mkImpact() })
    expect(canPublishProposal(ready)).toBe(true)
  })

  it('impact_ready 但影响面 failed → 禁止发布（D10）', () => {
    const ready = mkProposal({ status: 'impact_ready', impact: mkImpact({ status: 'failed' }) })
    expect(canPublishProposal(ready)).toBe(false)
  })

  it('published/discarded 终态不能废弃；draft/impact_ready 可废弃（UC-S5-18）', () => {
    expect(canDiscardProposal(mkProposal({ status: 'draft' }))).toBe(true)
    expect(canDiscardProposal(mkProposal({ status: 'impact_ready', impact: mkImpact() }))).toBe(true)
    expect(canDiscardProposal(mkProposal({ status: 'published' }))).toBe(false)
    expect(canDiscardProposal(mkProposal({ status: 'discarded' }))).toBe(false)
  })
})
