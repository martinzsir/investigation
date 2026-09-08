import { beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { createPinia, setActivePinia } from 'pinia'
import { useCaseStore } from '../src/stores/case'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  ACTION_TARGET,
  allowedActions,
  auditBanner,
  canFileRole,
  crossLevel,
  fileGate,
  healthBanner,
  isMachineRole,
  maskField,
  maskIdCard,
  maskPhone,
  partitionEvidence,
  CLUE_STATUS,
  degradeReason,
  type EvidenceItem,
} from '../src/domain/clue'

const here = dirname(fileURLToPath(import.meta.url))
const comp = (p: string) => resolve(here, '..', 'src', 'components', p)

beforeEach(() => {
  setActivePinia(createPinia())
  sessionStorage.clear()
  localStorage.clear()
})

describe('FE-T-009 敏感/业务数据不落 localStorage', () => {
  it('案件选择只写 sessionStorage；localStorage 无 sunzi.* 键', () => {
    const cs = useCaseStore()
    cs.selectCase('c1')
    expect(sessionStorage.getItem('sunzi.case')).toBe('c1')
    const lsKeys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)).filter(Boolean)
    expect(lsKeys.filter((k) => k!.startsWith('sunzi.'))).toEqual([])
  })
})

describe('FE-T-003 立案角色门禁：机器/低权会话不渲染立案入口', () => {
  it.each([
    ['human', true],
    ['主办', true],
    ['偏将', false],
    ['正兵', false],
    ['见习', false],
  ])('角色 %s → canFileRole=%s', (role, expected) => {
    expect(canFileRole(role)).toBe(expected)
  })

  it.each(['system', 'ai', 'assistant', 'agent:disposer'])('%s 机器会话一律不可立案（按钮不渲染）', (role) => {
    expect(canFileRole(role)).toBe(false)
    expect(isMachineRole(role)).toBe(true)
  })
})

describe('FE-C-014 立案四重门禁（fileGate）', () => {
  it('门禁1 不通过：render=false（不渲染，而非置灰）', () => {
    const g = fileGate('system', CLUE_STATUS.CONFIRMED, '刑诉法107条', false)
    expect(g.render).toBe(false)
    expect(g.enabled).toBe(false)
  })
  it('门禁2：非「已固证」→ 渲染但禁用，提示需先固证', () => {
    const g = fileGate('human', CLUE_STATUS.VERIFYING, '刑诉法107条', false)
    expect(g.render).toBe(true)
    expect(g.enabled).toBe(false)
    expect(g.reason).toContain('固证')
  })
  it('门禁3：已固证但法定依据空 → 禁用并提示', () => {
    const g = fileGate('human', CLUE_STATUS.CONFIRMED, '   ', false)
    expect(g.enabled).toBe(false)
    expect(g.reason).toContain('法定依据')
  })
  it('四重全过：可提交', () => {
    const g = fileGate('human', CLUE_STATUS.CONFIRMED, '《刑事诉讼法》第一百零七条', false)
    expect(g).toMatchObject({ render: true, enabled: true })
  })
  it('FE-T-010 降级态：渲染但禁用且写明降级原因', () => {
    const g = fileGate('human', CLUE_STATUS.CONFIRMED, '依据', true)
    expect(g.render).toBe(true)
    expect(g.enabled).toBe(false)
    expect(g.reason).toContain('降级')
    expect(degradeReason(true)).toContain('降级')
    expect(degradeReason(false)).toBe('')
  })
})

describe('FE-T-006 五间交叉升格：单源恒「观察」', () => {
  it.each([
    [0, '观察'],
    [1, '观察'],
    [2, '线索'],
    [3, '可立案依据候选'],
    [5, '可立案依据候选'],
  ])('独立通道数 %s → %s', (n, level) => {
    expect(crossLevel(n)).toBe(level)
  })
})

describe('FE-T-004 无溯源推断拒绝渲染', () => {
  const items: EvidenceItem[] = [
    { id: 'f1', kind: 'fact', text: '事实1', source_rows: [{ row_uri: 'u1' }] },
    { id: 'i1', kind: 'inference', text: '推断-有溯源', source_rows: [{ row_uri: 'u2' }] },
    { id: 'i2', kind: 'inference', text: '推断-无溯源' },
    { id: 'i3', kind: 'inference', text: '推断-空溯源', source_rows: [] },
    { id: 'p1', kind: 'pending', text: '待核实1' },
  ]
  const col = partitionEvidence(items)

  it('缺 source_rows 的推断被丢弃并计数', () => {
    expect(col.droppedInferences).toBe(2)
    expect(col.inferences.map((i) => i.id)).toEqual(['i1'])
  })
  it('事实栏只含 fact，推断永不混入（FE-T-005 三栏不合并）', () => {
    expect(col.facts.every((i) => i.kind === 'fact')).toBe(true)
    expect(col.pending.every((i) => i.kind === 'pending')).toBe(true)
    expect(col.inferences.every((i) => i.kind === 'inference' && (i.source_rows?.length ?? 0) > 0)).toBe(true)
  })
})

describe('FE-T-005 推断栏配色固定琥珀、禁绿（结构断言）', () => {
  const css = readFileSync(comp('research/ThreeColumnEvidence.vue'), 'utf8')
  it('推断栏使用金色 token', () => {
    const inferenceBlock = css.slice(css.indexOf('.ev-col--inference {'))
    expect(inferenceBlock).toContain('--sun-gold')
  })
  it('推断栏样式块不出现绿色/ok 语义色', () => {
    const inferenceBlock = css.slice(css.indexOf('.ev-col--inference {'))
    expect(inferenceBlock).not.toContain('--sun-ok')
    expect(inferenceBlock).not.toMatch(/2ED47A|46d39a/i)
  })
  it('待核实栏为灰虚线样式', () => {
    expect(css).toMatch(/ev-col--pending[\s\S]*?dashed/)
  })
})

describe('FE-C-010 字段遮蔽（红线⑥ / FE-T-021 明文不入展示缓存）', () => {
  it('手机前 3 后 4，中段不可见', () => {
    const out = maskPhone('13812345678')
    expect(out).toBe('138****5678')
    expect(out).not.toContain('1234')
  })
  it('身份证前 6 后 4', () => {
    const out = maskIdCard('310115199001011234')
    expect(out.startsWith('310115')).toBe(true)
    expect(out.endsWith('1234')).toBe(true)
    expect(out).not.toContain('19900101')
  })
  it('denied 一律渲染 **** 且不含真值片段', () => {
    expect(maskField('13812345678', 'denied', 'phone')).toBe('****')
    expect(maskField('机密内容XYZ', 'denied', 'text')).toBe('****')
  })
  it('masked 输出不含完整明文（防入缓存泄漏）', () => {
    const out = maskField('13812345678', 'masked', 'phone')
    expect(out).not.toBe('13812345678')
    expect(out).not.toContain('1234')
  })
  it('visible 才出真值', () => {
    expect(maskField('蓝海贸易', 'visible', 'text')).toBe('蓝海贸易')
  })
})

describe('FE-T-015 审计链空链 warn 语义', () => {
  it('空链（actual_count=0）：warn 且不得出现「通过」', () => {
    const b = auditBanner({ chain_ok: false, expected_count: 0, actual_count: 0, broken_links: [], empty_chain: true })
    expect(b.tone).toBe('warn')
    expect(b.title).toContain('空')
    expect(b.title).not.toContain('通过')
  })
  it('空链即使 chain_ok 被误标 true 也按 0 条判 warn（前端不盲信）', () => {
    const b = auditBanner({ chain_ok: true, expected_count: 0, actual_count: 0, broken_links: [] })
    expect(b.tone).toBe('warn')
  })
  it('断链：warn 且含断链数', () => {
    const b = auditBanner({ chain_ok: false, expected_count: 5, actual_count: 4, broken_links: [{}] })
    expect(b.tone).toBe('warn')
    expect(b.detail).toContain('1')
  })
  it('完整链：ok 语义', () => {
    const b = auditBanner({ chain_ok: true, expected_count: 5, actual_count: 5, broken_links: [] })
    expect(b.tone).toBe('ok')
    expect(b.title).toContain('通过')
  })
  it('未取得校验结果：warn', () => {
    expect(auditBanner(null).tone).toBe('warn')
  })
})

describe('FE-P-001 健康度：零记录 warn，禁止「一切正常」', () => {
  it('零诊断：warn 语义', () => {
    const b = healthBanner({ available: true, 诊断总数: 0, 计数: { critical: 0, warning: 0, info: 0 } })
    expect(b.tone).toBe('warn')
    expect(b.title).not.toContain('正常')
  })
  it('有 warning：降级 warn', () => {
    const b = healthBanner({ available: true, 诊断总数: 3, 计数: { critical: 0, warning: 1, info: 2 } })
    expect(b.tone).toBe('warn')
    expect(b.title).toContain('降级')
  })
  it('critical：严重 warn', () => {
    const b = healthBanner({ available: true, 诊断总数: 3, 计数: { critical: 1, warning: 0, info: 2 } })
    expect(b.tone).toBe('warn')
    expect(b.title).toContain('严重')
  })
  it('仅 info：ok', () => {
    expect(healthBanner({ available: true, 诊断总数: 2, 计数: { critical: 0, warning: 0, info: 2 } }).tone).toBe('ok')
  })
})

describe('状态机（core/registry.py 同构）', () => {
  it('已立案为终态：无任何可执行动作', () => {
    expect(allowedActions(CLUE_STATUS.FILED)).toEqual([])
  })
  it('已固证可立案/排除；查证中可固证/排除/退回', () => {
    expect(allowedActions(CLUE_STATUS.CONFIRMED)).toContain('file')
    expect(allowedActions(CLUE_STATUS.VERIFYING).sort()).toEqual(['confirm', 'exclude', 'reset'].sort())
  })
  it('动作目标态映射与服务端 dispose worker 一致', () => {
    expect(ACTION_TARGET.file).toBe(CLUE_STATUS.FILED)
    expect(ACTION_TARGET.confirm).toBe(CLUE_STATUS.CONFIRMED)
    expect(ACTION_TARGET.verify).toBe(CLUE_STATUS.VERIFYING)
    expect(ACTION_TARGET.exclude).toBe(CLUE_STATUS.EXCLUDED)
    expect(ACTION_TARGET.reset).toBe(CLUE_STATUS.PENDING)
  })
})
