import { describe, expect, it } from 'vitest'
import type { CaseDto, CaseSummaryDto } from '../src/api/endpoints/cases'
import type { HypothesisCandidate } from '../src/api/endpoints/research'
import type { VerifyStep } from '../src/api/endpoints/packageCase'

// ---- 门户 portal ----
import {
  canArchive, caseIdError, caseNameError, filterCases, healthTone,
  portalViewState, sumTodos,
} from '../src/domain/portal'

// ---- 图谱 graphModel ----
import {
  clampEdgeLimit, clampNodeLimit, edgesToRows, hasGraph, nodesByDegree,
  truncatedBanner,
} from '../src/domain/graphModel'

// ---- 庙算 miaoSuan（含 FE-T-014 红线） ----
import {
  applyCandidates, candidateHasNoLevelMutation, dualTrackGaps, gapCounts,
  heatLevel, heatMax,
} from '../src/domain/miaoSuan'

// ---- 跨案件 crossCase（全有或全无红线） ----
import {
  authorizeSelection, buildUnionTemplate, canExecute, caseIdsError,
  clampMaxRows, clampTimeoutMs, executeBlockReason, hasSourceCaseColumn,
  isSafeCaseId, reasonError,
} from '../src/domain/crossCase'

// ---- 案件包 packageFlow ----
import {
  canImport, chainWarning, downloadZipName, failCount, orderedSteps,
  sensitiveRedList, stepTone, taskPhase,
} from '../src/domain/packageFlow'

// ---- 逃生舱 escapeHatch ----
import {
  barPct, EXT_META, isEmptyStats, normalizeStats, statsMax, stubNameError,
} from '../src/domain/escapeHatch'

// ---- 系统设置 settingsModel（fail-closed 红线） ----
import {
  canSubmit, canViewAdminSettings, clampKey, diffValues, LOCKED_REDLINES,
  QUEUE_KEYS, settingsReasonError,
} from '../src/domain/settingsModel'

function caseDto(over: Partial<CaseDto> = {}): CaseDto {
  return {
    id: 'c1', tenant_id: 't1', name: '海州专案', status: '侦查中',
    pack_id: 'default', pack_snapshot_at: '', created_at: '', created_by: 'u1',
    ...over,
  }
}

function summary(over: Partial<CaseSummaryDto> = {}): CaseSummaryDto {
  return {
    case: caseDto(),
    data_version: 1,
    todos: { clues_pending: 0, review_pending: 0, anomalies_pending: 0 },
    health: { chain_ok: true, degraded: false, diagnostics_warn: 0 },
    recent_tasks: [],
    ...over,
  }
}

describe('MVP-5 门户 portal', () => {
  it('三态：空库/筛选无结果/有数据可区分', () => {
    expect(portalViewState([], [], false)).toBe('empty')
    const all = [caseDto({ id: 'c1' })]
    expect(portalViewState(all, [], false)).toBe('no-result')
    expect(portalViewState(all, all, false)).toBe('data')
  })

  it('客户端筛选：status 精确 + q 匹配 id/name', () => {
    const cases = [
      caseDto({ id: 'c1', name: '海州专案', status: '侦查中' }),
      caseDto({ id: 'c2', name: '已结旧案', status: '已结案' }),
    ]
    expect(filterCases(cases, { status: 'all', q: '' })).toHaveLength(2)
    expect(filterCases(cases, { status: '已结案', q: '' })).toHaveLength(1)
    expect(filterCases(cases, { status: 'all', q: '海州' })).toHaveLength(1)
    expect(filterCases(cases, { status: 'all', q: 'C2' })).toHaveLength(1)
    expect(filterCases(cases, { status: '侦查中', q: '旧案' })).toHaveLength(0)
  })

  it('建案表单校验', () => {
    expect(caseIdError('')).toContain('不能为空')
    expect(caseIdError('a b')).toContain('仅支持')
    expect(caseIdError('c-1_2')).toBe('')
    expect(caseNameError('')).toContain('不能为空')
    expect(caseNameError('x'.repeat(129))).toContain('128')
  })

  it('归档门槛 clearance≥2；待办聚合；健康点', () => {
    expect(canArchive(1)).toBe(false)
    expect(canArchive(2)).toBe(true)
    const todos = sumTodos([
      summary({ todos: { clues_pending: 3, review_pending: 1, anomalies_pending: 2 } }),
      summary({ todos: { clues_pending: 2, review_pending: 0, anomalies_pending: 0 } }),
    ])
    expect(todos).toEqual({ clues: 5, review: 1, anomalies: 2 })
    expect(healthTone(summary())).toBe('ok')
    expect(healthTone(summary({ health: { chain_ok: false, degraded: false, diagnostics_warn: 0 } }))).toBe('warn')
    expect(healthTone(undefined)).toBe('none')
  })
})

describe('MVP-5 图谱 graphModel', () => {
  const g = {
    available: true,
    truncated: { nodes: true, edges: true, dropped_edges: 7 },
    nodes: [
      { id: 'person:1', label: '张三', type: 'person', type_title: '人员', jian: [] },
      { id: 'person:2', label: '李四', type: 'person', type_title: '人员', jian: [] },
      { id: 'person:3', label: '王五', type: 'person', type_title: '人员', jian: [] },
    ],
    edges: [
      { source: 'person:1', target: 'person:2', label: '通话', type: 'call' },
      { source: 'person:1', target: 'person:3', label: '转账', type: 'transfer' },
    ],
  }
  it('可用判定/区间钳制/横幅文案', () => {
    expect(hasGraph(g as never)).toBe(true)
    expect(hasGraph({ available: false, nodes: [], edges: [], truncated: g.truncated } as never)).toBe(false)
    expect(clampNodeLimit(0)).toBe(1)
    expect(clampNodeLimit(99999)).toBe(1000)
    expect(clampEdgeLimit(5000)).toBe(3000)
    expect(truncatedBanner(g as never)).toContain('丢弃 7 条边')
  })
  it('降级关系表格：端点缺失补 id-only；度数排序', () => {
    const rows = edgesToRows(g.edges, g.nodes)
    expect(rows[0]).toMatchObject({ sourceLabel: '张三', targetLabel: '李四', relation: '通话' })
    const orphan = edgesToRows([{ source: 'person:1', target: 'x:9', label: '转', type: 't' }], g.nodes)
    expect(orphan[0].targetLabel).toBe('x:9')
    const deg = nodesByDegree(g as never)
    expect(deg[0].node.id).toBe('person:1')
  })
})

describe('MVP-5 庙算 miaoSuan', () => {
  it('双轨缺口合并/计数', () => {
    const coverage = {
      declared: [{ dimension: '因间', covered: 4, total: 5, missing: ['因间'], reason: 'r', severity: 'warning', created_at: null }],
      empirical: [{ dimension: null, covered: 5, total: 5, missing: [], reason: null, severity: null, created_at: null }],
    }
    const gaps = dualTrackGaps(coverage as never)
    expect(gaps.declared.missing).toEqual(['因间'])
    expect(gapCounts(coverage as never)).toEqual({ declared: 1, empirical: 0 })
  })

  it('热力色阶五档', () => {
    expect(heatLevel(0, 10)).toBe(0)
    expect(heatLevel(2, 10)).toBe(1)
    expect(heatLevel(3, 10)).toBe(2)
    expect(heatLevel(6, 10)).toBe(3)
    expect(heatLevel(9, 10)).toBe(4)
    expect(heatMax([[0, 3], [5, 0]])).toBe(5)
  })

  it('FE-T-014 红线二：候补池注入不改交叉等级快照', () => {
    const levels = { clue1: '观察', clue2: '线索' }
    const snapshot = JSON.stringify(levels)
    const candidates: HypothesisCandidate[] = [
      { clue_id: 'cand1', title: '候补甲', jian_types: ['因间'], level: '观察', priority_score: 80, reason: 'r' },
    ]
    // candidate 结构本身无升格字段
    expect(candidates.every(candidateHasNoLevelMutation)).toBe(true)
    const out = applyCandidates(levels, candidates)
    // 注入后正式等级快照逐键不变
    expect(JSON.stringify(out.levels)).toBe(snapshot)
    expect(out.levels).toEqual({ clue1: '观察', clue2: '线索' })
    // 候补独立成区，不进等级表
    expect(out.levels).not.toHaveProperty('cand1')
    expect(out.candidates).toHaveLength(1)
    // 原对象不被突变
    expect(levels).toEqual({ clue1: '观察', clue2: '线索' })
  })

  it('带升格字段的伪 candidate 被红线探测函数识别', () => {
    const fake = { clue_id: 'x', title: 't', jian_types: [], level: null, priority_score: 1, reason: '', cross_level: '确认' } as unknown as HypothesisCandidate
    expect(candidateHasNoLevelMutation(fake)).toBe(false)
  })
})

describe('MVP-5 跨案件 crossCase（全有或全无红线）', () => {
  it('N/M 授权计算：无权案件进 denied', () => {
    const authz = authorizeSelection(['c1', 'c2', 'c3'], ['c1', 'c2'])
    expect(authz).toMatchObject({ m: 2, n: 3 })
    expect(authz.denied).toEqual(['c3'])
    expect(canExecute(authz)).toBe(false)
    expect(executeBlockReason(authz)).toContain('无权案件')
  })

  it('全部有权且 ≥2 才可执行；不足 2 案拦截', () => {
    expect(canExecute(authorizeSelection(['c1', 'c2'], ['c1', 'c2']))).toBe(true)
    expect(executeBlockReason(authorizeSelection(['c1'], ['c1']))).toContain('至少选择 2 个')
  })

  it('case_ids/reason 校验与区间钳制', () => {
    expect(caseIdsError(['c1'])).toContain('2 个')
    expect(caseIdsError(['c1', 'c1'])).toContain('重复')
    expect(caseIdsError(Array.from({ length: 51 }, (_, i) => `c${i}`))).toContain('50')
    expect(caseIdsError(['c 1', 'c2'])).toContain('非法字符')
    expect(caseIdsError(['c1', 'c2'])).toBe('')
    expect(reasonError('')).toContain('必填')
    expect(reasonError('串并案分析')).toBe('')
    expect(clampMaxRows(0)).toBe(1)
    expect(clampMaxRows(99999)).toBe(10000)
    expect(clampTimeoutMs(50)).toBe(100)
    expect(clampTimeoutMs(10 ** 9)).toBe(600000)
    expect(isSafeCaseId('c-1_2')).toBe(true)
    expect(isSafeCaseId('c 1')).toBe(false)
  })

  it('SQL 模板：UNION ALL + source_case 字面量列 + case_<id> 别名', () => {
    const sql = buildUnionTemplate(['c1', 'c2'], 'obj_person')
    expect(sql).toContain("'c1' AS source_case")
    expect(sql).toContain('case_c1.obj_person')
    expect(sql).toContain('UNION ALL')
    expect(sql).toContain('case_c2.obj_person')
    expect(() => buildUnionTemplate(['c1'], 'obj_person; DROP TABLE x')).toThrow(/非法表名/)
  })

  it('结果表 source_case 列探测（无则不伪造）', () => {
    expect(hasSourceCaseColumn([{ source_case: 'c1', v: 1 }])).toBe(true)
    expect(hasSourceCaseColumn([{ v: 1 }])).toBe(false)
    expect(hasSourceCaseColumn([])).toBe(false)
  })
})

describe('MVP-5 案件包 packageFlow', () => {
  const step = (key: VerifyStep['key'], status: VerifyStep['status']): VerifyStep =>
    ({ key, label: key, status })
  it('七步顺序补齐 + fail 计数 + 色调', () => {
    const steps = orderedSteps([
      step('hash', 'fail'),
      step('format', 'pass'),
    ])
    expect(steps.map((s) => s.key)).toEqual(['format', 'manifest', 'hash', 'declarations', 'schema', 'chain', 'duckdb'])
    expect(failCount(steps)).toBeGreaterThanOrEqual(6) // hash fail + 缺失步补 fail
    expect(stepTone('pass')).toBe('success')
    expect(stepTone('warn')).toBe('warning')
    expect(stepTone('fail')).toBe('error')
  })

  it('chain 橙警不阻断导入；ok=false 才禁入', () => {
    expect(chainWarning(false)).toContain('橙色告警')
    expect(chainWarning(true)).toBe('')
    expect(canImport({ ok: true, chain_ok: false } as never)).toBe(true)
    expect(canImport({ ok: false, chain_ok: true } as never)).toBe(false)
    expect(canImport(null)).toBe(false)
  })

  it('敏感红框去重 + 任务相位 + 下载文件名', () => {
    expect(sensitiveRedList(['a/case_knowledge.json', 'a/case_knowledge.json'])).toHaveLength(1)
    expect(taskPhase('SUCCEEDED')).toBe('done')
    expect(taskPhase('RUNNING')).toBe('running')
    expect(taskPhase('FAILED')).toBe('failed')
    expect(downloadZipName('c1')).toBe('c1_package.zip')
  })
})

describe('MVP-5 逃生舱 escapeHatch', () => {
  it('四类元数据齐全 + stats 归一补零', () => {
    expect(Object.keys(EXT_META)).toHaveLength(4)
    const bars = normalizeStats([{ ext_type: 'function', count: 5 }])
    expect(bars).toHaveLength(4)
    expect(bars.find((b) => b.ext_type === 'function')?.count).toBe(5)
    expect(bars.find((b) => b.ext_type === 'side_effect')?.count).toBe(0)
    expect(isEmptyStats(normalizeStats([]))).toBe(true)
    const max = statsMax(bars)
    expect(max).toBe(5)
    expect(barPct(5, max)).toBe(100)
    expect(barPct(0, max)).toBe(0)
  })
  it('name 标识符校验', () => {
    expect(stubNameError('')).toContain('不能为空')
    expect(stubNameError('1abc')).toContain('标识符')
    expect(stubNameError('my_func')).toBe('')
  })
})

describe('MVP-5 系统设置 settingsModel（fail-closed 红线）', () => {
  it('非 admin 闸门：锁定面板不渲染数值', () => {
    expect(canViewAdminSettings(false)).toBe(false)
    expect(canViewAdminSettings(true)).toBe(true)
  })
  it('reason 必填 + 有变更才可提交', () => {
    expect(settingsReasonError('  ')).toContain('必须填写原因')
    expect(canSubmit(['max_workers'], '扩容')).toBe(true)
    expect(canSubmit([], '扩容')).toBe(false)
    expect(canSubmit(['max_workers'], '')).toBe(false)
  })
  it('数值键区间钳制 + diff', () => {
    const meta = QUEUE_KEYS[0] // max_workers 1–16
    expect(clampKey(meta, 0)).toBe(1)
    expect(clampKey(meta, 99)).toBe(16)
    expect(diffValues({ a: 1, b: 2 }, { a: 1, b: 3 })).toEqual(['b'])
  })
  it('红线键锁定行存在且不含可写开关', () => {
    expect(LOCKED_REDLINES.some((r) => r.key === 'llm_enabled')).toBe(true)
    // 红线键永不出现在可写白名单
    const writable = [...QUEUE_KEYS.map((k) => k.key)]
    expect(writable).not.toContain('llm_enabled')
  })
})
