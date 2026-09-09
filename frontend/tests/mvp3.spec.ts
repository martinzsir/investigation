import { beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { setTransport } from '../src/api/transport'
import { clearToken } from '../src/api/token'
import { FakeTransport, okEnvelope } from './helpers'
import {
  isActive, isTerminal, canCancel, canRetry, mergeProgress, normPct, isIndeterminate,
  taskStats, reconnectHint, failureSummary, taskTypeLabel, type TaskRow,
} from '../src/domain/task'
import {
  MATCH_META, matchTone, isLowConfidence, isMatched, buildWizardMapping,
  toImportColumnMap, missingRequiredColumns, mappedCount, duplicateSourceMessage,
  classifyIngestError, type MappingSuggestion, type ColumnMatch,
} from '../src/domain/mapping'
import {
  deRecoStatus, DE_DRAFT_NOTICE, isDecidable, deConfidenceTone,
} from '../src/domain/recommend'
import { parseTaskEvent } from '../src/api/sse'
import { tasksApi } from '../src/api/endpoints/tasks'
import { sourcesApi } from '../src/api/endpoints/sources'
import { recommendationsApi } from '../src/api/endpoints/recommendations'

const here = dirname(fileURLToPath(import.meta.url))
const src = (p: string) => resolve(here, '..', 'src', p)
const view = (p: string) => readFileSync(src(p), 'utf-8')

beforeEach(() => {
  setTransport(null)
  clearToken()
  sessionStorage.clear()
})

function task(over: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 't1', case_id: 'c1', task_type: 'IMPORT', params: {},
    status: 'RUNNING', progress_pct: 0, progress_stage: '', progress_label: '', progress_detail: '',
    retry_count: 0, max_retries: 3, idem_key: 'k', created_at: '', updated_at: '',
    started_at: '', finished_at: '', error_code: '', error_message: '', created_by: '王',
    ...over,
  }
}

const suggestion: MappingSuggestion = {
  target_table: '银行流水',
  confidence: 0.77,
  matches: [
    { source_col: '交易时间', target_prop: '交易时间', match_type: 'exact', confidence: 1.0 },
    { source_col: '付款方名称', target_prop: '付款方', match_type: 'normalized', confidence: 0.85 },
    { source_col: '交易金额', target_prop: '金额', match_type: 'fuzzy', confidence: 0.6 },
    { source_col: null, target_prop: '摘要', match_type: 'none', confidence: 0.0 },
  ],
  missing_required: ['摘要'],
  low_confidence: ['金额'],
}

const matchOf = (t: ColumnMatch['match_type']): ColumnMatch =>
  suggestion.matches.find((m) => m.match_type === t)!

// ---------- FE-I-006：SSE 进度只增不减（重连不跳变） ----------

describe('FE-I-006 SSE 进度单调合并', () => {
  it('重连后收到更低 pct 的旧帧 → 不回退', () => {
    const prev = task({ progress_pct: 80, progress_stage: 'cold', status: 'RUNNING' })
    const older = task({ progress_pct: 20, progress_stage: 'parse', status: 'RUNNING' })
    const merged = mergeProgress(prev, older)
    expect(merged.progress_pct).toBe(80)
    expect(merged.progress_stage).toBe('cold')
  })

  it('收到更高 pct 的新帧 → 前进', () => {
    const prev = task({ progress_pct: 20, progress_stage: 'parse' })
    const next = task({ progress_pct: 60, progress_stage: 'cold' })
    expect(mergeProgress(prev, next).progress_pct).toBe(60)
  })

  it('终态帧权威：失败必须如实显示，即便 pct 较低', () => {
    const prev = task({ progress_pct: 90, status: 'RUNNING' })
    const failed = task({ progress_pct: 40, status: 'FAILED', error_code: 'IMPORT_PARSE' })
    const merged = mergeProgress(prev, failed)
    expect(merged.status).toBe('FAILED')
    expect(merged.error_code).toBe('IMPORT_PARSE')
  })

  it('已终态后不再被进度帧覆盖', () => {
    const done = task({ status: 'SUCCEEDED', progress_pct: 100 })
    const replay = task({ status: 'RUNNING', progress_pct: 50 })
    expect(mergeProgress(done, replay).status).toBe('SUCCEEDED')
  })

  it('等 pct 时保留有阶段文案的帧', () => {
    const bare = task({ progress_pct: 50, progress_stage: '', progress_label: '' })
    const rich = task({ progress_pct: 50, progress_stage: 'cold', progress_label: '写冷层' })
    expect(mergeProgress(bare, rich).progress_stage).toBe('cold')
  })

  it('pct 缺失/越界 → indeterminate（计算中）', () => {
    expect(isIndeterminate(null)).toBe(true)
    expect(isIndeterminate(undefined)).toBe(true)
    expect(isIndeterminate(120)).toBe(true)
    expect(isIndeterminate(-5)).toBe(true)
    expect(isIndeterminate(40)).toBe(false)
    expect(normPct(NaN)).toBe(-1)
  })

  it('断线 ≤3s 不提示，>3s 才提示（防抖动闪烁）', () => {
    expect(reconnectHint(0)).toBe('')
    expect(reconnectHint(2999)).toBe('')
    expect(reconnectHint(3000)).toBe('')
    expect(reconnectHint(3001)).toContain('重连')
    expect(reconnectHint(8000)).toContain('重连')
  })
})

// ---------- 任务状态机 / 统计 ----------

describe('任务状态机与统计', () => {
  it('终态/进行中/可取消判定', () => {
    expect(isTerminal('SUCCEEDED')).toBe(true)
    expect(isTerminal('FAILED')).toBe(true)
    expect(isTerminal('CANCELLED')).toBe(true)
    expect(isTerminal('RUNNING')).toBe(false)
    expect(isActive('PENDING')).toBe(true)
    expect(isActive('RUNNING')).toBe(true)
    expect(isActive('SUCCEEDED')).toBe(false)
    // 仅排队中可取消（RUNNING/终态 409）
    expect(canCancel('PENDING')).toBe(true)
    expect(canCancel('RUNNING')).toBe(false)
    expect(canCancel('SUCCEEDED')).toBe(false)
  })

  it('可重试判定：仅 FAILED / CANCELLED', () => {
    expect(canRetry('FAILED')).toBe(true)
    expect(canRetry('CANCELLED')).toBe(true)
    expect(canRetry('PENDING')).toBe(false)
    expect(canRetry('RUNNING')).toBe(false)
    expect(canRetry('SUCCEEDED')).toBe(false)
  })

  it('taskStats 五统计 + 失败摘要含 error_code 与重试次数', () => {
    const tasks = [
      task({ id: 'a', status: 'PENDING' }),
      task({ id: 'b', status: 'RUNNING' }),
      task({ id: 'c', status: 'SUCCEEDED' }),
      task({ id: 'd', status: 'FAILED', error_code: 'IMPORT_PARSE', error_message: '解析失败', retry_count: 2, max_retries: 3 }),
      task({ id: 'e', status: 'CANCELLED' }),
    ]
    const s = taskStats(tasks)
    expect(s.total).toBe(5)
    expect(s.active).toBe(2)
    expect(s.pending).toBe(1)
    expect(s.succeeded).toBe(1)
    expect(s.failed).toBe(1)
    expect(s.cancelled).toBe(1)
    const summary = failureSummary(tasks[3])
    expect(summary).toContain('IMPORT_PARSE')
    expect(summary).toContain('解析失败')
    expect(summary).toContain('2/3')
  })

  it('任务类型中文映射', () => {
    expect(taskTypeLabel('IMPORT')).toBe('数据导入')
    expect(taskTypeLabel('DE_RECOMMEND')).toBe('数据元推荐')
    expect(taskTypeLabel('UNKNOWN_X')).toBe('UNKNOWN_X')
  })
})

// ---------- FE-P-002：列映射（低置信高亮 / 缺列非阻断 / 方向转换） ----------

describe('列映射适配', () => {
  it('匹配色阶：exact 绿 / normalized 青 / fuzzy 琥珀低置信 / none 灰', () => {
    expect(matchTone(matchOf('exact'))).toBe('ok')
    expect(matchTone(matchOf('normalized'))).toBe('info')
    expect(matchTone(matchOf('fuzzy'))).toBe('warn')
    expect(matchTone(matchOf('none'))).toBe('muted')
    expect(MATCH_META.fuzzy.label).toBe('模糊匹配')
  })

  it('低置信归类（fuzzy 或 0<conf<0.7）', () => {
    expect(isLowConfidence(matchOf('fuzzy'))).toBe(true)
    expect(isLowConfidence(matchOf('exact'))).toBe(false)
    expect(isLowConfidence({ source_col: 'x', target_prop: 'y', match_type: 'fuzzy', confidence: 0.65 })).toBe(true)
  })

  it('未匹配列（source_col=null）不进入映射', () => {
    expect(isMatched(matchOf('none'))).toBe(false)
    expect(isMatched(matchOf('exact'))).toBe(true)
  })

  it('buildWizardMapping 仅含已匹配项 {声明列:源列}', () => {
    const m = buildWizardMapping(suggestion)
    expect(m['交易时间']).toBe('交易时间')
    expect(m['付款方']).toBe('付款方名称')
    expect(m['金额']).toBe('交易金额')
    expect(m['摘要']).toBeUndefined() // none 不进入
    expect(mappedCount(m)).toBe(3)
  })

  it('toImportColumnMap 方向转换 {源列:声明列}', () => {
    const wizard = buildWizardMapping(suggestion)
    const colMap = toImportColumnMap(wizard)
    expect(colMap['付款方名称']).toBe('付款方')
    expect(colMap['交易金额']).toBe('金额')
    expect(colMap['交易时间']).toBe('交易时间')
    // 不含未映射
    expect(Object.keys(colMap)).not.toContain('摘要')
  })

  it('缺必选列为降级警告（返回清单），非阻断——不抛错、可继续', () => {
    const missing = missingRequiredColumns(suggestion)
    expect(missing).toEqual(['摘要'])
    // 纯函数不抛错即表示非阻断；导入按钮可用性在页面层不依赖 missing
    expect(() => toImportColumnMap(buildWizardMapping(suggestion))).not.toThrow()
  })

  it('指纹重复提示文案', () => {
    expect(duplicateSourceMessage()).toContain('文件已存在')
    expect(duplicateSourceMessage('fp_x')).toContain('fp_x')
  })

  it('异常态区分：格式 / 映射 / 权限 / 冲突', () => {
    expect(classifyIngestError('CONFLICT', 'x')).toBe('conflict')
    expect(classifyIngestError('FORBIDDEN', 'x')).toBe('permission')
    expect(classifyIngestError('VALIDATION', '不支持的文件类型')).toBe('format')
    expect(classifyIngestError('VALIDATION', '声明列未在目标表')).toBe('mapping')
    expect(classifyIngestError('INTERNAL', 'boom')).toBe('other')
  })
})

// ---------- FE-P-008：接入建议 ----------

describe('接入建议确认', () => {
  it('后端中文状态 → 枚举', () => {
    expect(deRecoStatus('待核实')).toBe('pending')
    expect(deRecoStatus('采纳')).toBe('adopted')
    expect(deRecoStatus('驳回')).toBe('rejected')
  })

  it('待核实才可裁决；草案红线文案存在', () => {
    expect(DE_DRAFT_NOTICE).toContain('非生效声明')
    expect(isDecidable({ status: '待核实' } as never)).toBe(true)
    expect(isDecidable({ status: '采纳' } as never)).toBe(false)
  })

  it('置信度色阶', () => {
    expect(deConfidenceTone(0.9)).toBe('ok')
    expect(deConfidenceTone(0.6)).toBe('warn')
    expect(deConfidenceTone(0.4)).toBe('error')
  })
})

// ---------- SSE 事件字段对齐（决策 11：snake_case） ----------

describe('parseTaskEvent 字段映射', () => {
  it('读取后端 snake_case 字段（progress_stage/label/detail 文本）', () => {
    const raw = JSON.stringify({
      id: 't9', case_id: 'c1', task_type: 'IMPORT', status: 'RUNNING',
      progress_pct: 60, progress_stage: 'cold', progress_label: '写冷层 parquet',
      progress_detail: '银行流水 1203 行', error_code: '', error_message: '',
      retry_count: 1, max_retries: 3,
    })
    const ev = parseTaskEvent(raw)
    expect(ev.progress_stage).toBe('cold')
    expect(ev.progress_label).toBe('写冷层 parquet')
    expect(ev.progress_detail).toBe('银行流水 1203 行') // 纯文本
    expect(ev.progress_pct).toBe(60)
    expect(ev.task_type).toBe('IMPORT')
  })

  it('缺省字段回落默认值', () => {
    const ev = parseTaskEvent('{}')
    expect(ev.status).toBe('PENDING')
    expect(ev.progress_pct).toBe(0)
    expect(ev.max_retries).toBe(3)
    expect(ev.params).toEqual({})
  })
})

// ---------- API 契约（FakeTransport） ----------

describe('MVP-3 API 契约', () => {
  it('tasksApi.list → GET /tasks?case_id= 返回分页结构', async () => {
    const fake = new FakeTransport([
      {
        match: (r) => r.method === 'GET' && r.path.startsWith('/tasks'),
        respond: () => okEnvelope({ items: [], total: 0, page: 1, page_size: 50, stats: { total: 0, pending: 0, running: 0, active: 0, succeeded: 0, failed: 0, cancelled: 0 } }),
      },
    ])
    setTransport(fake)
    const res = await tasksApi.list('c1')
    expect(fake.calls[0].path).toContain('/tasks')
    expect(fake.calls[0].path).toContain('case_id=c1')
    expect(res).toHaveProperty('items')
    expect(res).toHaveProperty('total')
    expect(res.stats).toBeDefined()
  })

  it('tasksApi.list 分页/状态过滤 → 查询串含 status/page/page_size/task_type', async () => {
    const fake = new FakeTransport([
      { match: (r) => r.method === 'GET' && r.path.startsWith('/tasks'), respond: () => okEnvelope({ items: [], total: 0, page: 2, page_size: 50, stats: {} }) },
    ])
    setTransport(fake)
    await tasksApi.list('c1', { status: 'FAILED', taskType: 'IMPORT', page: 2, pageSize: 50 })
    const p = fake.calls[0].path
    expect(p).toContain('status=FAILED')
    expect(p).toContain('task_type=IMPORT')
    expect(p).toContain('page=2')
    expect(p).toContain('page_size=50')
  })

  it('tasksApi.cancel → POST /tasks/:id/cancel 带幂等键', async () => {
    const fake = new FakeTransport([
      { match: (r) => r.path.includes('/cancel'), respond: () => okEnvelope({ task_id: 't1', status: 'CANCELLED' }) },
    ])
    setTransport(fake)
    await tasksApi.cancel('t1')
    expect(fake.calls[0].method).toBe('POST')
    expect(fake.calls[0].path).toContain('/tasks/t1/cancel')
    expect(fake.calls[0].headers?.['Idempotency-Key']).toBeTruthy()
  })

  it('tasksApi.retry → POST /tasks/:id/retry 带幂等键，返回新任务', async () => {
    const fake = new FakeTransport([
      { match: (r) => r.path.includes('/retry'), respond: () => okEnvelope(task({ id: 't-new', status: 'PENDING', idem_key: '' })) },
    ])
    setTransport(fake)
    const nt = await tasksApi.retry('t-old')
    expect(fake.calls[0].method).toBe('POST')
    expect(fake.calls[0].path).toContain('/tasks/t-old/retry')
    expect(fake.calls[0].headers?.['Idempotency-Key']).toBeTruthy()
    expect(nt.id).toBe('t-new')
    expect(nt.status).toBe('PENDING')
  })

  it('sourcesApi.import → POST column_map 为 {源列:声明列}，带幂等键', async () => {
    const fake = new FakeTransport([
      { match: (r) => r.path.includes('/import'), respond: () => okEnvelope(task({ id: 't-import' })) },
    ])
    setTransport(fake)
    await sourcesApi.import('c1', 'up1', {
      target_table: '银行流水',
      column_map: { 付款方名称: '付款方', 交易金额: '金额' },
    })
    const body = fake.calls[0].body as { target_table: string; column_map: Record<string, string> }
    expect(body.target_table).toBe('银行流水')
    expect(body.column_map['付款方名称']).toBe('付款方')
    expect(fake.calls[0].headers?.['Idempotency-Key']).toBeTruthy()
  })

  it('recommendationsApi.decide → POST /decide body.decision', async () => {
    const fake = new FakeTransport([
      { match: (r) => r.path.includes('/decide'), respond: () => okEnvelope({ id: 't-de' }) },
    ])
    setTransport(fake)
    await recommendationsApi.decide('c1', 'der_1', 'adopt')
    expect((fake.calls[0].body as { decision: string }).decision).toBe('adopt')
    expect(fake.calls[0].path).toContain('/de-recommendations/der_1/decide')
  })
})

// ---------- 页面/组件结构源码断言（红线落点） ----------

describe('MVP-3 页面结构红线', () => {
  it('TaskCenterView：SSE 单调合并 + 3s 重连提示 + 取消', () => {
    const s = view('views/TaskCenterView.vue')
    expect(s).toContain('mergeProgress')
    expect(s).toContain('reconnectHint')
    expect(s).toContain('taskEvents')
    expect(s).toContain('PAGE_SIZE_DEFAULT')
  })

  it('TaskCenterView：失败/已取消任务可重试（创建人/admin 闸门 + 操作列）', () => {
    const s = view('views/TaskCenterView.vue')
    expect(s).toContain('tasksApi.retry')
    expect(s).toContain('canRetryTask')
    // 与后端同口径：创建人或管理员
    expect(s).toContain("t.created_by === auth.operator || auth.isAdmin")
    // 操作列与点击处理
    expect(s).toContain('@click="onRetry(t)"')
  })

  it('TaskCenterView：分页栏常驻（共 N 条）+ 跳页，单页不再整体隐藏', () => {
    const s = view('views/TaskCenterView.vue')
    // 常驻「共 N 条 / 第 X / Y 页」，跳页复用 pagination 纯函数
    expect(s).toContain('共 {{ historyTotal }} 条')
    expect(s).toContain('jumpPageError')
    expect(s).toContain('goPage')
    // 旧实现：totalPagesHist > 1 才渲染分页栏，单页时无任何分页体现
    expect(s).not.toContain('v-if="totalPagesHist > 1"')
  })

  it('MSW handlers：tasks 分页切片 + 历史演示数据（60 条 / 2 页）', () => {
    const h = readFileSync(resolve(here, '..', 'mocks', 'handlers.ts'), 'utf-8')
    expect(h).toContain('list.slice(start, start + pageSize)')
    expect(h).toContain('分页演示')
    expect(h).toContain('i < 55')
  })

  it('IngestView：Stepper + 缺列非阻断警告 + 低置信 + 指纹 + 幂等冲突', () => {
    const s = view('views/IngestView.vue')
    expect(s).toContain('Stepper')
    expect(s).toContain('missingRequiredColumns')
    expect(s).toContain('不阻断')
    expect(s).toContain('toImportColumnMap')
    expect(s).toContain('duplicateSourceMessage')
    expect(s).toContain('CONFLICT')
  })

  it('SuggestView：草案非生效声明 + 未选禁用', () => {
    const s = view('views/SuggestView.vue')
    expect(s).toContain('DE_DRAFT_NOTICE')
    expect(s).toContain(':disabled="!choice')
  })

  it('TaskProgressCard：indeterminate + 失败显 error_code', () => {
    const s = view('components/task/TaskProgressCard.vue')
    expect(s).toContain('isIndeterminate')
    expect(s).toContain('failureSummary')
  })

  it('MSW handlers：SSE events 流 + tasks/sources/de-recommendations 端点', () => {
    const h = readFileSync(resolve(here, '..', 'mocks', 'handlers.ts'), 'utf-8')
    expect(h).toContain("tasks/:tid/events")
    expect(h).toContain('text/event-stream')
    expect(h).toContain('sources/upload')
    expect(h).toContain('de-recommendations')
  })
})
