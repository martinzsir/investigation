import { beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { setTransport } from '../src/api/transport'
import { errEnvelope, FakeTransport, okEnvelope } from './helpers'

import {
  LOCKED_KEYS, EDITABLE_FIELDS, sanitizeEditBody, containsLockedKey,
  isDangerousChange, triggersRescan, canEditMachineBehavior, validateRuleEdit,
  diffRule, ruleTextError,
} from '../src/domain/ruleEdit'
import {
  objectCell, matrixCoverage, propertyVisibility, viewVisible, canWriteConfig,
} from '../src/domain/policyMatrix'
import {
  groupConflicts, conflictSummary, PATH_GUIDE, isValidCastErrorPolicy, isValidNullPolicy,
  compositeList,
} from '../src/domain/etlConfig'
import {
  groupBySeverity, hasBlocking, heuristicCeilingViolations, healthLevel,
  SEVERITY_COLOR,
} from '../src/domain/qualityReport'
import { QUARANTINE_REASONS } from '../src/api/endpoints/quarantine'
import { PAGE_SIZE_DEFAULT, totalPages, clampPage } from '../src/domain/pagination'
import { rulesApi } from '../src/api/endpoints/rules'
import type { Rule } from '../src/api/endpoints/rules'
import type { QualityCheck, QualityReport } from '../src/api/endpoints/quality'
import type { ObjectPolicy, PropertyPolicy, ViewDef } from '../src/api/endpoints/policies'
import type { ValidateConflict } from '../src/api/endpoints/etl'

const here = dirname(fileURLToPath(import.meta.url))
const view = (p: string) => resolve(here, '..', 'src', 'views', p)
const comp = (p: string) => resolve(here, '..', 'src', 'components', p)
const ep = (p: string) => resolve(here, '..', 'src', 'api', 'endpoints', p)
const src = (p: string) => readFileSync(p, 'utf8')

beforeEach(() => {
  setTransport(null)
  sessionStorage.clear()
})

const baseRule: Rule = {
  id: 'R6',
  title: '时间窗碰撞',
  rule_text: '同一对手方在 60 分钟窗口内双向资金往来且金额近似，列为候选反常（说明性判据文本）。',
  function: 'time_window_collision',
  stage: 'xu_shi',
  enabled: true,
  params: { window_minutes: 60 },
}

// ---------------------------------------------------------------- FE-T-011
describe('FE-T-011 红线常量不可被配置覆盖', () => {
  it('L0 锁定键恰好 12 条，且与可编辑字段不相交', () => {
    expect(LOCKED_KEYS).toHaveLength(12)
    for (const k of LOCKED_KEYS) {
      expect((EDITABLE_FIELDS as readonly string[]).includes(k)).toBe(false)
    }
  })

  it('sanitizeEditBody：12 条 L0 键注入全部被剥离，只留白名单四字段', () => {
    const dirty: Record<string, unknown> = {
      id: 'R6', rule_id: 'R6', function: 'steal', function_catalog: ['x'],
      stage: 'nei_jian', hit_when: '1=1', jian_types: ['死间'], dimension: ['资金'],
      title: '被篡改标题', pack: 'evil', sql: 'DROP TABLE obj_transaction', action: 'file',
      rule_text: '合法的判据文本修订，二十字以上应该可以通过校验。',
      params: { window_minutes: 30 },
      enabled: false,
      reason: '基线复核收紧窗口',
    }
    expect(containsLockedKey(dirty)).toHaveLength(12)
    const out = sanitizeEditBody(dirty)
    expect(Object.keys(out).sort()).toEqual(['enabled', 'params', 'reason', 'rule_text'])
    expect(containsLockedKey(out as Record<string, unknown>)).toEqual([])
    const outRaw = out as Record<string, unknown>
    expect(outRaw.function).toBeUndefined()
    expect(outRaw.sql).toBeUndefined()
    expect(outRaw.action).toBeUndefined()
    expect(out.params).toEqual({ window_minutes: 30 })
  })

  it('sanitizeEditBody：类型不符的白名单字段同样忽略（不裸传出网）', () => {
    const out = sanitizeEditBody({
      rule_text: 42 as unknown as string,
      params: 'not-an-object',
      enabled: 'true' as unknown as boolean,
      reason: 'r',
    })
    expect(out.rule_text).toBeUndefined()
    expect(out.params).toBeUndefined()
    expect(out.enabled).toBeUndefined()
    expect(out.reason).toBe('r')
  })

  it('传输契约：PUT 注入 function 键 → 后端白名单拒绝 400（不落盘）', async () => {
    setTransport(
      new FakeTransport([
        {
          match: (r) => r.method === 'PUT' && /\/cases\/[^/]+\/rules\/[^/]+$/.test(r.path),
          respond: (r) => {
            const body = (r.body ?? {}) as Record<string, unknown>
            const structural = Object.keys(body).filter(
              (k) => !['rule_text', 'params', 'enabled', 'reason'].includes(k),
            )
            if (structural.length) {
              return errEnvelope(400, 'VALIDATION', `结构字段只读不可改：${structural.join(',')}`)
            }
            return okEnvelope({ rule_id: 'R6', changed: [], rescan_task: null })
          },
        },
      ]),
    )
    await expect(
      rulesApi.update('c1', 'R6', { function: 'evil_fn' } as unknown as Parameters<typeof rulesApi.update>[2]),
    ).rejects.toMatchObject({ code: 'VALIDATION', httpStatus: 400 })
  })

  it('结构断言：规则工坊结构字段以 NTag 只读渲染，不渲染编辑态', () => {
    const vue = src(view('RuleWorkshopView.vue'))
    expect(vue).toContain('<NTag')
    expect(vue).toContain('>{{ r.function }}</NTag>')
    // 出网前过白名单摘取
    expect(vue).toContain('sanitizeEditBody')
    // 结构字段不得出现在任何 v-model 绑定上
    expect(vue).not.toMatch(/v-model="[^"]*r\.(function|stage|hit_when|jian_types|dimension|title)"/)
  })
})

// ---------------------------------------------------------------- FE-T-012
describe('FE-T-012 配置变更写入审计链：危险确认 + 理由必填', () => {
  it('params/enabled 变更 = 危险项；纯 rule_text 修订 = 普通项', () => {
    expect(isDangerousChange(['params'])).toBe(true)
    expect(isDangerousChange(['enabled'])).toBe(true)
    expect(isDangerousChange(['params', 'rule_text'])).toBe(true)
    expect(isDangerousChange(['rule_text'])).toBe(false)
    expect(triggersRescan(['enabled'])).toBe(true)
    expect(triggersRescan(['rule_text'])).toBe(false)
  })

  it('机器行为下区偏将及以上可写（clearance≥2）', () => {
    expect(canEditMachineBehavior(0)).toBe(false)
    expect(canEditMachineBehavior(1)).toBe(false)
    expect(canEditMachineBehavior(2)).toBe(true)
    expect(canWriteConfig(2)).toBe(true)
  })

  it('validateRuleEdit：危险项无理由 → 拦截；有理由 → 放行', () => {
    const noReason = validateRuleEdit(baseRule, { params: { window_minutes: 30 } }, 3)
    expect(noReason).toContain('理由')
    const withReason = validateRuleEdit(
      baseRule, { params: { window_minutes: 30 }, reason: '基线复核收紧' }, 3,
    )
    expect(withReason).toBe('')
    // 低 clearance 改 params 直接拒
    expect(validateRuleEdit(baseRule, { params: { x: 1 }, reason: 'r' }, 1)).toContain('偏将')
  })

  it('rule_text 少于 20 字拒（判据须写明模式/理由/边界）', () => {
    expect(ruleTextError('太短')).not.toBe('')
    expect(ruleTextError(baseRule.rule_text)).toBe('')
    expect(diffRule(baseRule, { enabled: false })).toEqual(['enabled'])
  })

  it('传输契约：危险变更缺 reason → 400；带 reason → 出网 body 携带 reason（落审计链 note）', async () => {
    const seen: Array<Record<string, unknown>> = []
    setTransport(
      new FakeTransport([
        {
          match: (r) => r.method === 'PUT' && /\/cases\/[^/]+\/rules\/[^/]+$/.test(r.path),
          respond: (r) => {
            const body = (r.body ?? {}) as Record<string, unknown>
            seen.push(body)
            const changed = body.params !== undefined || body.enabled !== undefined
            if (changed && !String(body.reason ?? '').trim()) {
              return errEnvelope(400, 'VALIDATION', '机器行为变更必须填写变更理由')
            }
            return okEnvelope({ rule_id: 'R6', changed: ['params'], rescan_task: { id: 't-1' } })
          },
        },
      ]),
    )
    await expect(
      rulesApi.update('c1', 'R6', { params: { window_minutes: 30 } }),
    ).rejects.toMatchObject({ code: 'VALIDATION' })
    const res = await rulesApi.update('c1', 'R6', {
      params: { window_minutes: 30 },
      reason: '近 90 日基线复核后收紧',
    })
    expect(res.changed).toContain('params')
    expect(seen.at(-1)?.reason).toBe('近 90 日基线复核后收紧')
  })

  it('结构断言：确认对话框危险态红框 + 理由必填禁用确认 + 「确认变更并留痕」', () => {
    const vue = src(comp('config/ConfigConfirmDialog.vue'))
    expect(vue).toContain('reasonMissing')
    expect(vue).toContain(':disabled="reasonMissing"')
    expect(vue).toContain('确认变更并留痕')
    expect(vue).toContain('必填')
  })

  it('结构断言：六个配置写端点 body 均带 reason（审计链可查的前端侧保证）', () => {
    for (const f of ['rules.ts', 'model.ts', 'policies.ts', 'knowledge.ts', 'dataElements.ts', 'etl.ts']) {
      expect(src(ep(f)), f).toMatch(/reason\??:|reason:/)
    }
  })
})

// ---------------------------------------------------------------- FE-T-013
describe('FE-T-013 未声明 = 拒绝（fail-closed 矩阵斜纹态）', () => {
  const policy: ObjectPolicy = { object: 'transaction', roles: ['偏将', '主办', 'human'], min_clearance: 2 }

  it('未声明对象策略：五角色全部 undeclared（绝不是 allowed）', () => {
    for (const role of ['见习', '正兵', '偏将', '主办', 'human']) {
      expect(objectCell(undefined, role)).toBe('undeclared')
    }
  })

  it('已声明但不满足 → denied；满足 → allowed', () => {
    expect(objectCell(policy, '见习')).toBe('denied')
    expect(objectCell(policy, '正兵')).toBe('denied')
    expect(objectCell(policy, '偏将')).toBe('allowed')
    expect(objectCell({ ...policy, roles: ['human'] }, 'human')).toBe('allowed')
  })

  it('matrixCoverage 列出未声明对象（红条计数来源）', () => {
    const cov = matrixCoverage(['transaction', 'call', 'secret_obj'], [policy])
    expect(cov.undeclared.sort()).toEqual(['call', 'secret_obj'])
  })

  it('属性级 fail-closed：default=deny 且不在白名单 → hidden', () => {
    const p: PropertyPolicy = { object: 'person', property: 'id_card', default: 'deny', mask: 'partial' }
    expect(propertyVisibility(p, '正兵')).toBe('hidden')
    expect(propertyVisibility({ ...p, allow_roles: ['主办'] }, '主办')).toBe('masked')
    expect(propertyVisibility({ ...p, allow_roles: ['主办'] }, '见习')).toBe('hidden')
  })

  it('视图未声明角色 → 不可见（空 roles 恒 false）', () => {
    const v: ViewDef = { name: 'v1', base_object: 'transaction', properties: ['amount'], roles: [] }
    expect(viewVisible(v, '主办')).toBe(false)
    expect(viewVisible({ ...v, roles: ['human'] }, '见习')).toBe(true)
  })

  it('结构断言：未声明格 class=state-denied + 斜纹底 + 锁图标 + 「拒绝」', () => {
    const vue = src(view('PolicyMaskingView.vue'))
    expect(vue).toContain("'state-denied'")
    expect(vue).toContain('🔒 拒绝')
    expect(vue).toContain('repeating-linear-gradient')
    expect(vue).toContain('fail-closed')
  })
})

// ---------------------------------------------------------------- FE-T-016
describe('FE-T-016 1:1 映射阻断：冲突诊断 + 出路，无「忽略继续」', () => {
  const conflicts: ValidateConflict[] = [
    { type: 'one_to_one', message: '源列「交易金额」同时映射到 transaction.amount 与 call.duration', source_col: '交易金额', target_a: 'amount', target_b: 'duration' },
    { type: 'unknown_prop', message: '目标属性 transaction.not_a_field 未在对象上声明', target_prop: 'not_a_field' },
    { type: 'missing_column', message: '源表缺列：对手方账号', source_col: '对手方账号' },
  ]

  it('冲突按类型分组且顺序固定（one_to_one → unknown_prop → missing_column）', () => {
    const g = groupConflicts(conflicts)
    expect(g.map((x) => x.type)).toEqual(['one_to_one', 'unknown_prop', 'missing_column'])
    expect(g[0].items).toHaveLength(1)
    expect(conflictSummary(conflicts)).toContain('一对多')
    expect(groupConflicts([])).toEqual([])
  })

  it('出路只有 A/B 两条，键名与后端 paths 同构；不存在 force/ignore 出路', () => {
    expect(Object.keys(PATH_GUIDE).sort()).toEqual(['A_split_source_sql', 'B_degrade_column'])
    expect(PATH_GUIDE.A_split_source_sql).toContain('source_sql')
    expect(JSON.stringify(PATH_GUIDE)).not.toMatch(/force|ignore/i)
  })

  it('策略枚举白名单：脏值不入配置', () => {
    expect(isValidCastErrorPolicy('quarantine')).toBe(true)
    expect(isValidCastErrorPolicy('ignore_and_continue')).toBe(false)
    expect(isValidNullPolicy('reject')).toBe(true)
    expect(isValidNullPolicy('force')).toBe(false)
  })

  it('compositeList：composite_props 缺省/非数组 → 空（GET 恒返 [] 时不渲染诊断区）', () => {
    expect(compositeList(null)).toEqual([])
    expect(compositeList({ composite_props: [] })).toEqual([])
    expect(compositeList({ composite_props: [{ prop: 'mix' }] })).toHaveLength(1)
  })

  it('结构断言：映射预检页是只读页（无任何写动作），只给出路 A/B', () => {
    const vue = src(view('MappingView.vue'))
    expect(vue).toContain('出路 A')
    expect(vue).toContain('出路 B')
    // 预检页不允许落库：不挂保存管道动作、不带幂等写键
    expect(vue).not.toContain('savePipeline')
    expect(vue).not.toContain('idempotencyAction')
  })

  it('结构断言：ValidateResult 类型体内无 force/continue 字段；ETL 页拆分按钮恒 disabled', () => {
    const etlTs = src(ep('etl.ts'))
    const block = etlTs.slice(etlTs.indexOf('interface ValidateResult'), etlTs.indexOf('interface MissingColumnItem'))
    expect(block).not.toMatch(/force|ignore|continue/)
    const etlVue = src(view('EtlPipelineView.vue'))
    expect(etlVue).toMatch(/<NButton[^>]*disabled[^>]*>拆分/)
    expect(etlVue).toContain('忽略继续') // 仅出现在「不提供忽略继续」的禁止性说明里
  })
})

// ---------------------------------------------------------------- FE-T-017
describe('FE-T-017 丢弃可见：隔离区 + 清洗留痕 + 零隔离显式文案', () => {
  it('隔离原因恰好四类（stats 聚合口径）', () => {
    expect(QUARANTINE_REASONS.map((r) => r.value).sort()).toEqual(
      ['cast_error', 'dedup', 'null_value', 'other'].sort(),
    )
  })

  it('服务端分页：page_size 默认 50、页码钳制', () => {
    expect(PAGE_SIZE_DEFAULT).toBe(50)
    expect(totalPages(0, 50)).toBe(1)
    expect(totalPages(120, 50)).toBe(3)
    expect(clampPage(99, 120)).toBe(3)
    expect(clampPage(0, 120)).toBe(1)
  })

  it('结构断言：零隔离必须显式说「无数据被丢弃」，不允许沉默空表', () => {
    const vue = src(view('QuarantineView.vue'))
    expect(vue).toContain('empty_message')
    expect(vue).toContain('本次装载无数据被丢弃')
  })

  it('结构断言：隔离区与清洗留痕并排可见（stats + 清洗前后行数/丢弃率）', () => {
    const vue = src(view('QuarantineView.vue'))
    expect(vue).toContain('stats')
    expect(vue).toContain('cleanTrace')
    expect(vue).toContain('rows_before')
    expect(vue).toContain('rows_after')
    expect(vue).toContain('dropped_rows')
    expect(vue).toContain('samples_masked')
  })
})

// ---------------------------------------------------------------- FE-T-018
describe('FE-T-018 启发式只告警：封顶 suggest，永不出红阻断', () => {
  const checks: QualityCheck[] = [
    { category: 'sensitive', mode: 'heuristic', obj: 'transaction', prop: 'remark', severity: 'suggest', message: '备注疑似含手机号模式，建议人工确认', samples_masked: ['139****1234'] },
    { category: 'unit', mode: 'deterministic', obj: 'transaction', prop: 'amount', severity: 'ok', message: '单位一致', samples_masked: [] },
  ]

  it('heuristic 封顶 suggest：block/warn 越界被检出', () => {
    const bad: QualityCheck[] = [
      { category: 'sensitive', mode: 'heuristic', obj: 'o', prop: 'p', severity: 'block', message: '越界红牌', samples_masked: [] },
      { category: 'unit', mode: 'heuristic', obj: 'o', prop: 'p2', severity: 'warn', message: '越界警告', samples_masked: [] },
    ]
    expect(heuristicCeilingViolations(bad)).toHaveLength(2)
    expect(heuristicCeilingViolations(checks)).toEqual([])
  })

  it('heuristic suggest 不进入阻断/告警计数；确定性 block 不算启发式越界', () => {
    const g = groupBySeverity(checks)
    expect(g.suggest).toHaveLength(1)
    const detBlock: QualityCheck[] = [
      { category: 'compliance', mode: 'deterministic', obj: 'o', prop: 'p', severity: 'block', message: '确定性违规', samples_masked: [] },
    ]
    expect(heuristicCeilingViolations(detBlock)).toEqual([])
  })

  it('色阶：suggest=黄、block=红，启发式不得使用红色', () => {
    expect(SEVERITY_COLOR.suggest.toLowerCase()).toBe('#c9a227')
    expect(SEVERITY_COLOR.block.toLowerCase()).toBe('#d93026')
    expect(SEVERITY_COLOR.suggest).not.toBe(SEVERITY_COLOR.block)
  })

  it('结构断言：质量页启发式项标「建议」，与确定性标签区分', () => {
    const vue = src(view('QualityView.vue'))
    expect(vue).toContain('启发式·建议')
  })
})

// ---------------------------------------------------------------- FE-T-019
describe('FE-T-019 确定性检查不静默：block 必须处置 + NULL 计数可见', () => {
  const report: Extract<QualityReport, { available: true }> = {
    available: true,
    check_id: 'qc-1',
    created_at: '2025-07-01 10:00:00',
    created_by: '王检察官',
    data_version: 3,
    summary: { total: 3, passed: 1, warnings: 0, violations: 2 },
    checks: [
      { category: 'compliance', mode: 'deterministic', obj: 'person', prop: 'id_card', severity: 'block', count: 4, message: '身份证校验位错误 4 行', samples_masked: ['310115********1234'] },
      { category: 'freshness', mode: 'deterministic', obj: 'transaction', prop: 'date', severity: 'ok', message: '时效正常', samples_masked: [] },
    ],
  }

  it('确定性 block → hasBlocking=true 且总健康度=block（红牌）', () => {
    expect(hasBlocking(report)).toBe(true)
    expect(healthLevel(report)).toBe('block')
    expect(groupBySeverity(report.checks).block).toHaveLength(1)
  })

  it('无报告/零违规语义：available=false → empty；全 ok → pass', () => {
    expect(healthLevel(null)).toBe('empty')
    expect(healthLevel({ available: false })).toBe('empty')
    expect(healthLevel({ ...report, summary: { total: 2, passed: 2, warnings: 0, violations: 0 }, checks: [] })).toBe('pass')
  })

  it('结构断言：质量页红条明示「必须处置」，不允许静默通过', () => {
    const vue = src(view('QualityView.vue'))
    expect(vue).toContain('必须处置')
  })

  it('结构断言：CAST 策略缺省 = 降级 NULL 文案可见；缺列/转换失败诊断有种类展示', () => {
    const etlVue = src(view('EtlPipelineView.vue'))
    expect(etlVue).toContain('降级 NULL')
    const mapVue = src(view('MappingView.vue'))
    expect(mapVue).toContain('source_value_cast_failed')
    expect(mapVue).toContain('缺列')
  })
})
