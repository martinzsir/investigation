import { beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { setTransport } from '../src/api/transport'
import { clearToken } from '../src/api/token'
import { FakeTransport, okEnvelope, errEnvelope } from './helpers'
import { crossLevelFromSignals, countIndependentJians, type CrossSignal } from '../src/domain/crosslevel'
import { isOverdue, stayDays, boardStats, toBoardCard } from '../src/domain/board'
import { CLUE_STATUS, type ClueStatus } from '../src/domain/clue'
import {
  completeLlmInference,
  confidenceBand,
  diffRows,
  hasMore,
  mergeQueuePage,
  parseVerdictKey,
  reasonError,
} from '../src/domain/review'
import {
  clampPage,
  jumpPageError,
  PAGE_SIZE_DEFAULT,
  parsePageQuery,
  pageQuery,
  totalPages,
} from '../src/domain/pagination'
import {
  nullRateBand, scoreBand, severityBand,
  groupColumnsByObject, columnHasIssue, type ProfileColumn,
} from '../src/domain/profile'
import { reviewApi } from '../src/api/endpoints/review'
import type { ClueListItem } from '../src/api/endpoints/clues'

const here = dirname(fileURLToPath(import.meta.url))
const comp = (p: string) => resolve(here, '..', 'src', p)

beforeEach(() => {
  setTransport(null)
  clearToken()
  sessionStorage.clear()
})

// ---------- FE-T-007：LLM 同源去重 ----------

describe('FE-T-007 LLM 同源去重：同一 LLM 多次判读只算一个间', () => {
  it('同模型两条 LLM 判读（同一间）→ 1 间，不升格', () => {
    const sigs: CrossSignal[] = [
      { room: '资金', source: 'llm', llmModel: 'sunzi-llm-shadow-v1' },
      { room: '资金', source: 'llm', llmModel: 'sunzi-llm-shadow-v1' },
    ]
    expect(countIndependentJians(sigs)).toBe(1)
    expect(crossLevelFromSignals(sigs)).toBe('观察')
  })

  it('红线用例：LLM 判备注可疑(资金)+LLM 判聊天可疑(通讯) ≠ 双源 → 仍是 1 间', () => {
    const sigs: CrossSignal[] = [
      { room: '资金', source: 'llm:备注判读', llmModel: 'sunzi-llm-shadow-v1' },
      { room: '通讯', source: 'llm:聊天判读', llmModel: 'sunzi-llm-shadow-v1' },
    ]
    expect(countIndependentJians(sigs)).toBe(1)
    expect(crossLevelFromSignals(sigs)).toBe('观察')
  })

  it('两个结构化通道命中 → 2 间 → 线索', () => {
    const sigs: CrossSignal[] = [
      { room: '资金', source: '银行流水' },
      { room: '通讯', source: '通话记录' },
    ]
    expect(countIndependentJians(sigs)).toBe(2)
    expect(crossLevelFromSignals(sigs)).toBe('线索')
  })

  it('三个结构化通道 → 3 间 → 可立案依据候选', () => {
    const sigs: CrossSignal[] = [
      { room: '资金', source: '银行流水' },
      { room: '通讯', source: '通话记录' },
      { room: '行为', source: '住宿轨迹' },
    ]
    expect(countIndependentJians(sigs)).toBe(3)
    expect(crossLevelFromSignals(sigs)).toBe('可立案依据候选')
  })

  it('结构化 1 间 + 同模型 LLM 声称另两间 → LLM 至多贡献 1 → 共 2 间', () => {
    const sigs: CrossSignal[] = [
      { room: '资金', source: '银行流水' },
      { room: '通讯', source: 'llm', llmModel: 'm1' },
      { room: '行为', source: 'llm', llmModel: 'm1' },
    ]
    expect(countIndependentJians(sigs)).toBe(2)
  })

  it('LLM 声称的间已被结构化证据覆盖 → 不占额', () => {
    const sigs: CrossSignal[] = [
      { room: '资金', source: '银行流水' },
      { room: '资金', source: 'llm', llmModel: 'm1' },
    ]
    expect(countIndependentJians(sigs)).toBe(1)
  })

  it('不同模型的 LLM 各自至多贡献 1 间', () => {
    const sigs: CrossSignal[] = [
      { room: '通讯', source: 'llm', llmModel: 'model-A' },
      { room: '行为', source: 'llm', llmModel: 'model-B' },
    ]
    expect(countIndependentJians(sigs)).toBe(2)
  })

  it('未知间类不计入', () => {
    expect(countIndependentJians([{ room: '不存在的间', source: 'x' }])).toBe(0)
  })
})

// ---------- FE-P-009 / FE-C-033：看板超期 ----------

describe('FE-C-033 看板 SLA 超期判定', () => {
  const now = new Date('2026-09-09T12:00:00')
  const daysAgo = (n: number) => {
    const d = new Date(now.getTime() - n * 86_400_000)
    const pad = (x: number) => String(x).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00:00`
  }

  it('停留天数按 updated_at 计算；非法日期回落 0', () => {
    expect(stayDays(daysAgo(4), now)).toBe(4)
    expect(stayDays('not-a-date', now)).toBe(0)
    expect(stayDays(undefined, now)).toBe(0)
  })

  it('待查 SLA=3 天：停留 4 天超期，3 天不超', () => {
    expect(isOverdue(CLUE_STATUS.PENDING, daysAgo(4), now)).toBe(true)
    expect(isOverdue(CLUE_STATUS.PENDING, daysAgo(3), now)).toBe(false)
  })

  it('查证中 SLA=5 / 已固证 SLA=7', () => {
    expect(isOverdue(CLUE_STATUS.VERIFYING, daysAgo(6), now)).toBe(true)
    expect(isOverdue(CLUE_STATUS.CONFIRMED, daysAgo(8), now)).toBe(true)
    expect(isOverdue(CLUE_STATUS.CONFIRMED, daysAgo(7), now)).toBe(false)
  })

  it('终态（已立案/已排除）永不超期', () => {
    expect(isOverdue(CLUE_STATUS.FILED, daysAgo(400), now)).toBe(false)
    expect(isOverdue(CLUE_STATUS.EXCLUDED, daysAgo(400), now)).toBe(false)
  })

  it('boardStats：超期计数 + 平均停留（仅考核态）', () => {
    const base = {
      clue_id: '', title: '', skill_id: '', jian_types: [], level: '观察',
      dimension: [], priority_rank: 1, priority_score: 0, source_row_count: 0,
      merged_from: [], note: '', operator: '', status_source: 'artifact' as const,
    }
    const mk = (clue_id: string, status: ClueStatus, updated_at: string): ClueListItem =>
      ({ ...base, clue_id, status, updated_at })
    const cards = [
      toBoardCard(mk('C1', CLUE_STATUS.PENDING, daysAgo(5)), now), // 超期
      toBoardCard(mk('C2', CLUE_STATUS.PENDING, daysAgo(1)), now), // 不超
      toBoardCard(mk('C3', CLUE_STATUS.VERIFYING, daysAgo(7)), now), // 超期
      toBoardCard(mk('C4', CLUE_STATUS.FILED, daysAgo(30)), now), // 终态不计
    ]
    const s = boardStats(cards)
    expect(s.overdue).toBe(2)
    expect(s.byStatus[CLUE_STATUS.PENDING]).toBe(2)
    expect(s.byStatus[CLUE_STATUS.FILED]).toBe(1)
    // 平均停留只统计 3 张考核态：(5+1+7)/3 = 4.3
    expect(s.avgStay).toBe(4.3)
  })
})

// ---------- FE-P-005 / FE-C-019：实体裁决键盘流 ----------

describe('FE-P-005 键盘流 A/R/D', () => {
  const key = (k: string, target?: HTMLElement | null) =>
    parseVerdictKey({ key: k, target: target ?? document.body })

  it('A=合并(merge) / R=驳回(reject) / D=跳过(defer)，大小写不敏感', () => {
    expect(key('a')).toBe('merge')
    expect(key('R')).toBe('reject')
    expect(key('d')).toBe('defer')
  })

  it('其他键不触发', () => {
    expect(key('x')).toBeNull()
    expect(key('Enter')).toBeNull()
  })

  it('在输入框/文本域内打字不触发快捷键（理由输入不被劫持）', () => {
    const ta = document.createElement('textarea')
    const inp = document.createElement('input')
    expect(parseVerdictKey({ key: 'a', target: ta })).toBeNull()
    expect(parseVerdictKey({ key: 'r', target: inp })).toBeNull()
  })

  it('带修饰键（Ctrl/Alt/Meta）不触发', () => {
    expect(parseVerdictKey({ key: 'a', ctrlKey: true })).toBeNull()
    expect(parseVerdictKey({ key: 'r', altKey: true })).toBeNull()
    expect(parseVerdictKey({ key: 'd', metaKey: true })).toBeNull()
  })

  it('驳回理由必填；合并理由可空；超长拦截', () => {
    expect(reasonError('reject', '')).toContain('必须填写')
    expect(reasonError('reject', '   ')).toContain('必须填写')
    expect(reasonError('merge', '')).toBe('')
    expect(reasonError('reject', '身份证号与住址均不同')).toBe('')
    expect(reasonError('merge', 'x'.repeat(201))).toContain('200')
  })
})

describe('FE-C-019 无限加载队列 + 差异行', () => {
  it('mergeQueuePage 按 entity_id 去重并保序', () => {
    const acc = [{ entity_id: 'A' }, { entity_id: 'B' }]
    const out = mergeQueuePage(acc, {
      items: [{ entity_id: 'B' }, { entity_id: 'C' }],
      total: 3,
    })
    expect(out.map((x) => x.entity_id)).toEqual(['A', 'B', 'C'])
  })

  it('hasMore：已载数 < total 才为 true', () => {
    expect(hasMore(20, 25)).toBe(true)
    expect(hasMore(25, 25)).toBe(false)
  })

  it('diffRows：值不同标记差异；两侧皆空不算；相似依据行也高亮', () => {
    const rows = diffRows([
      { label: '信用代码', left: 'X1', right: 'X1', basis: true },
      { label: '住址', left: '浦东 A 号', right: '闵行 B 号' },
      { label: '备注', left: '', right: '' },
      { label: '法人', left: '张某', right: '张某' },
    ])
    expect(rows[0].diff).toBe(false) // 相同
    expect(rows[1].diff).toBe(true) // 不同
    expect(rows[2].diff).toBe(false) // 双空
    expect(rows[3].diff).toBe(false)
  })
})

// ---------- FE-C-023：LLM 判读卡三件套 ----------

describe('FE-C-023 LLM 判读卡三件套缺一不渲染', () => {
  const full = {
    text: '判读结论',
    llm_model: 'm1',
    confidence: 0.8,
    room: '资金',
    source_rows: [{ row_uri: 'u1' }],
  }
  it('三件套齐备 → 可渲染', () => {
    expect(completeLlmInference(full)).toBe(true)
  })
  it.each([
    ['缺结论', { ...full, text: '' }],
    ['缺溯源', { ...full, source_rows: [] }],
    ['缺模型', { ...full, llm_model: '' }],
    ['缺置信度', { ...full, confidence: undefined }],
    ['置信度非数值', { ...full, confidence: Number.NaN }],
  ])('%s → 不渲染', (_name, inf) => {
    expect(completeLlmInference(inf)).toBe(false)
  })
  it('置信度色阶：≥.85 青 / .60–.84 琥珀 / <.60 红', () => {
    expect(confidenceBand(0.9)).toBe('ok')
    expect(confidenceBand(0.7)).toBe('warn')
    expect(confidenceBand(0.4)).toBe('error')
  })
})

// ---------- FE-C-003：分页 ----------

describe('FE-C-003 分页：page_size=50 / 跳页校验 / URL query', () => {
  it('默认每页 50', () => {
    expect(PAGE_SIZE_DEFAULT).toBe(50)
  })
  it('总页数计算', () => {
    expect(totalPages(0)).toBe(1)
    expect(totalPages(50)).toBe(1)
    expect(totalPages(51)).toBe(2)
    expect(totalPages(243, 50)).toBe(5)
  })
  it('parsePageQuery：非法/缺省回落 1，接受 string 与数组', () => {
    expect(parsePageQuery(undefined)).toBe(1)
    expect(parsePageQuery('')).toBe(1)
    expect(parsePageQuery('abc')).toBe(1)
    expect(parsePageQuery('3')).toBe(3)
    expect(parsePageQuery(['3', '4'])).toBe(3)
    expect(parsePageQuery('0')).toBe(1)
    expect(parsePageQuery('-5')).toBe(1)
  })
  it('clampPage：钳制在 [1, totalPages]', () => {
    expect(clampPage(99, 243)).toBe(5)
    expect(clampPage(0, 243)).toBe(1)
    expect(clampPage(3, 243)).toBe(3)
  })
  it('跳页输入校验：空/非数字/超范围报错', () => {
    expect(jumpPageError('', 243)).toContain('页码')
    expect(jumpPageError('abc', 243)).toContain('正整数')
    expect(jumpPageError('6', 243)).toContain('超出')
    expect(jumpPageError('5', 243)).toBe('')
    expect(jumpPageError('1', 0)).toBe('') // total=0 → 共 1 页
  })
  it('pageQuery：page=1 省略 page 键，page_size 始终携带', () => {
    expect(pageQuery(1)).toEqual({ page_size: '50' })
    expect(pageQuery(3)).toEqual({ page: '3', page_size: '50' })
  })
})

// ---------- FE-P-007：画像色阶 ----------

describe('FE-P-007 画像色阶', () => {
  it('画像分 ≥90 绿 / 70–89 琥珀 / <70 红', () => {
    expect(scoreBand(98.7)).toBe('ok')
    expect(scoreBand(72)).toBe('warn')
    expect(scoreBand(61)).toBe('error')
  })
  it('空值率 >20% 红 / >5% 琥珀 / 否则正常', () => {
    expect(nullRateBand(0.214)).toBe('error')
    expect(nullRateBand(0.183)).toBe('warn')
    expect(nullRateBand(0.002)).toBe('ok')
  })
  it('扣分项严重度映射', () => {
    expect(severityBand('high')).toBe('error')
    expect(severityBand('medium')).toBe('warn')
    expect(severityBand('low')).toBe('ok')
  })
})

// ---------- API 契约：review 三端点路径 + 裁决写幂等键 ----------

describe('reviewApi 契约（server review.py W-021）', () => {
  it('queue GET 路径含分页参数', async () => {
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path.includes('/review/queue'),
        respond: (r) => {
          expect(r.path).toContain('page=2')
          expect(r.path).toContain('page_size=20')
          return okEnvelope({ items: [], total: 0 })
        },
      },
    ])
    setTransport(t)
    const q = await reviewApi.queue('c1', 2, 20)
    expect(q.total).toBe(0)
  })

  it('decide POST：review_reject 路径 + 幂等键 + body', async () => {
    const t = new FakeTransport([
      {
        match: (r) => r.method === 'POST' && r.path.includes('/review/ENT-1/decision'),
        respond: (r) => {
          expect(r.headers?.['Idempotency-Key']).toBeTruthy()
          const body = r.body as { action: string; reason?: string }
          expect(body.action).toBe('review_reject')
          expect(body.reason).toBe('身份证号不同')
          return okEnvelope({ task_id: 't1', action: body.action, candidate_id: 'ENT-1' })
        },
      },
    ])
    setTransport(t)
    const res = await reviewApi.decide('c1', 'ENT-1', { action: 'review_reject', reason: '身份证号不同' })
    expect(res.candidate_id).toBe('ENT-1')
  })

  it('evidence 404（案件未 BUILD）→ 抛 NOT_FOUND', async () => {
    setTransport(
      new FakeTransport([
        { match: () => true, respond: () => errEnvelope(404, 'NOT_FOUND', '案件尚未 BUILD') },
      ]),
    )
    await expect(reviewApi.evidence('c2', 'ENT-X')).rejects.toMatchObject({ code: 'NOT_FOUND' })
  })
})

// ---------- 页面/组件结构红线（无 test-utils，源码断言） ----------

describe('MVP-2 页面结构红线', () => {
  it('EntityCompare 明示「禁止自动合并」红线 + A/R/D 键位', () => {
    const src = readFileSync(comp('components/review/EntityCompare.vue'), 'utf8')
    expect(src).toContain('禁止自动合并')
    expect(src).toContain('parseVerdictKey')
    expect(src).toContain('reasonError')
  })
  it('VerdictView 空态文案「未发现实体歧义」', () => {
    const src = readFileSync(comp('views/VerdictView.vue'), 'utf8')
    expect(src).toContain('未发现实体歧义')
  })
  it('ProfileView 空态文案「尚未接入数据源」', () => {
    const src = readFileSync(comp('views/ProfileView.vue'), 'utf8')
    expect(src).toContain('尚未接入数据源')
  })
  it('KanbanCard 超期整卡红框 class', () => {
    const src = readFileSync(comp('components/board/KanbanCard.vue'), 'utf8')
    expect(src).toContain('kcard--overdue')
    expect(src).toContain('sun-error-border')
  })
  it('DataTable 含跳页输入框', () => {
    const src = readFileSync(comp('components/common/DataTable.vue'), 'utf8')
    expect(src).toContain('jumpPageError')
    expect(src).toContain('跳至')
  })
  it('LlmInferenceCard 三件套守卫 + 归属间标签', () => {
     const src = readFileSync(comp('components/review/LlmInferenceCard.vue'), 'utf8')
     expect(src).toContain('completeLlmInference')
     expect(src).toContain('llm_model')
   })
 })

// ---------- FE-P-007：属性画像按对象折叠分组 ----------

describe('FE-P-007 属性画像 groupColumnsByObject 按对象折叠', () => {
  const col = (object: string, attribute: string, over: Partial<ProfileColumn> = {}): ProfileColumn => ({
    object, attribute,
    value_type: 'string', connectable: false, status: 'ok',
    row_count: 0, non_null: 0, null_rate: 0, distinct_count: 0, samples: [],
    dropped_rows: null, clean_rule: null,
    compliance_rate: null, compliance_element: null,
    mixed_type: false, landing: [], needs_confirmation: false,
    composite_suspect: 0, variants_rule: 0, variants_alias: 0,
    score: 100, issues: [],
    ...over,
  })

  it('空数组 → 空分组', () => {
    expect(groupColumnsByObject([])).toEqual([])
  })

  it('保持对象首次出现顺序与组内属性顺序；live/issue 聚合计数', () => {
    const groups = groupColumnsByObject([
      col('person', 'name'),
      col('call', 'caller_raw'),
      col('person', 'id_no', { status: 'missing_column' }),
      col('call', 'date', { issues: ['L5_NULL_HIGH'] }),
      col('call', 'times'),
    ])
    expect(groups.map((g) => g.object)).toEqual(['person', 'call'])
    expect(groups[0].columns.map((c) => c.attribute)).toEqual(['name', 'id_no'])
    expect(groups[1].columns.map((c) => c.attribute)).toEqual(['caller_raw', 'date', 'times'])
    // person：1 活 1 死（缺列同时计问题）
    expect(groups[0].live_count).toBe(1)
    expect(groups[0].issue_count).toBe(1)
    // call：3 行全物化，date 有扣分 → 1 项问题
    expect(groups[1].live_count).toBe(3)
    expect(groups[1].issue_count).toBe(1)
  })

  it('columnHasIssue 口径：混装/复合/待确认/变体/缺列/扣分算问题，干净行不算', () => {
    expect(columnHasIssue(col('x', 'a'))).toBe(false)
    expect(columnHasIssue(col('x', 'a', { status: 'unmaterialized_object' }))).toBe(true)
    expect(columnHasIssue(col('x', 'a', { mixed_type: true }))).toBe(true)
    expect(columnHasIssue(col('x', 'a', { composite_suspect: 2 }))).toBe(true)
    expect(columnHasIssue(col('x', 'a', { needs_confirmation: true }))).toBe(true)
    expect(columnHasIssue(col('x', 'a', { variants_rule: 1 }))).toBe(true)
    expect(columnHasIssue(col('x', 'a', { variants_alias: 2 }))).toBe(true)
    expect(columnHasIssue(col('x', 'a', { issues: ['L5_NULL_HIGH'] }))).toBe(true)
  })

  it('红线：ProfileView 属性画像表含分组折叠结构（组头行 + 全部展开/折叠）', () => {
    const src = readFileSync(comp('views/ProfileView.vue'), 'utf8')
    expect(src).toContain('group-row')
    expect(src).toContain('toggleGroup')
    expect(src).toContain('全部展开')
    expect(src).toContain('全部折叠')
  })
})
