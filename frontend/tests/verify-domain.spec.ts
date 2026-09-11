import { describe, expect, it } from 'vitest'
import {
  VERIFY_STATUS,
  VERIFY_TRANSITIONS,
  allowedVerifyActions,
  canVerifyTransition,
  isManualOrigin,
  isVerifyConcluded,
  isVerifySideStatus,
  legalVerifyTargets,
  verifyConclusionRequired,
  verifyKindLabel,
  verifyProgressPercent,
  verifyStatusMeta,
  type VerifyProgress,
} from '../src/domain/verify'

// REQ-V-006 AC-1：domain/verify.ts 与 core/verify_machine.py 同构纯函数。

describe('核查状态机同构表', () => {
  it('七态齐全且迁移表白名单与 core 一致', () => {
    expect(Object.values(VERIFY_STATUS)).toHaveLength(7)
    expect(VERIFY_TRANSITIONS['建议']).toEqual(['待核查', '已忽略'])
    expect(VERIFY_TRANSITIONS['已忽略']).toEqual(['待核查'])
    expect(VERIFY_TRANSITIONS['待核查']).toEqual([
      '核查中', '已证实', '已查否', '无法核实',
    ])
    expect(VERIFY_TRANSITIONS['核查中']).toEqual([
      '已证实', '已查否', '无法核实', '待核查',
    ])
    expect(VERIFY_TRANSITIONS['已证实']).toEqual(['核查中'])
    expect(VERIFY_TRANSITIONS['已查否']).toEqual(['核查中'])
    expect(VERIFY_TRANSITIONS['无法核实']).toEqual(['核查中'])
  })

  it.each([
    ['待核查', '待核查', false], // 自迁移非法
    ['已证实', '已查否', false], // 终态互跳非法
    ['建议', '已证实', false],
    ['未知态', '待核查', false],
    ['待核查', '核查中', true],
    ['建议', '待核查', true],
    ['已忽略', '待核查', true],
    ['已查否', '核查中', true],
  ])('canVerifyTransition(%s → %s) = %s', (cur, nxt, ok) => {
    expect(canVerifyTransition(cur, nxt)).toBe(ok)
    if (!ok) expect(legalVerifyTargets(cur)).not.toContain(nxt)
  })

  it('未知状态 legalVerifyTargets 返回空数组（不抛异常）', () => {
    expect(legalVerifyTargets('不存在')).toEqual([])
  })
})

describe('allowedVerifyActions 行动作现算', () => {
  const labelsOf = (s: string) => allowedVerifyActions(s).map((a) => a.label)
  const kindsOf = (s: string) => allowedVerifyActions(s).map((a) => a.kind)

  it('待核查：开始核查/证实/查否/无法核实，不含重开', () => {
    const acts = allowedVerifyActions('待核查')
    expect(labelsOf('待核查')).toEqual([
      '开始核查', '证实', '查否', '无法核实',
    ])
    expect(acts.every((a) => a.kind !== 'reopen')).toBe(true)
  })

  it('已证实：仅重开（翻案合法）', () => {
    expect(kindsOf('已证实')).toEqual(['reopen'])
    expect(labelsOf('已证实')).toEqual(['重开'])
  })

  it('已查否/无法核实：仅重开', () => {
    expect(kindsOf('已查否')).toEqual(['reopen'])
    expect(kindsOf('无法核实')).toEqual(['reopen'])
  })

  it('核查中：证实/查否/无法核实/退回（无开始核查）', () => {
    expect(kindsOf('核查中')).toEqual([
      'confirm', 'disprove', 'unverifiable', 'sendback',
    ])
  })

  it('建议：采纳（可改写）+ 忽略；已忽略：重新采纳', () => {
    const adopt = allowedVerifyActions('建议')
    expect(adopt.map((a) => a.kind)).toEqual(['adopt', 'ignore'])
    expect(adopt[0].rewrite).toBe(true)
    expect(kindsOf('已忽略')).toEqual(['readopt'])
  })

  it('证实/查否动作结论必填；采纳/开始核查/重开不要求', () => {
    const byKind = Object.fromEntries(
      allowedVerifyActions('待核查').map((a) => [a.kind, a]),
    )
    expect(byKind.confirm.conclusionRequired).toBe(true)
    expect(byKind.disprove.conclusionRequired).toBe(true)
    expect(byKind.start.conclusionRequired).toBe(false)
    expect(byKind.unverifiable.conclusionRequired).toBe(false)
  })

  it('动作目标态都在状态机白名单内（非法动作永不出现）', () => {
    for (const s of Object.values(VERIFY_STATUS)) {
      for (const a of allowedVerifyActions(s)) {
        expect(canVerifyTransition(s, a.target)).toBe(true)
      }
    }
  })

  it('未知状态无动作（不渲染）', () => {
    expect(allowedVerifyActions('不存在')).toEqual([])
  })
})

describe('结论门禁与展示元数据', () => {
  it('verifyConclusionRequired：仅已证实/已查否', () => {
    expect(verifyConclusionRequired('已证实')).toBe(true)
    expect(verifyConclusionRequired('已查否')).toBe(true)
    expect(verifyConclusionRequired('无法核实')).toBe(false)
    expect(verifyConclusionRequired('核查中')).toBe(false)
  })

  it('七态都有色板映射（复用 tokens STATUS_TONE_META）', () => {
    for (const s of Object.values(VERIFY_STATUS)) {
      const m = verifyStatusMeta(s)
      expect(m.text).toBeTruthy()
      expect(m.border).toBeTruthy()
      expect(m.bg).toBeTruthy()
    }
  })

  it('建议/已忽略为旁路态；终态三态计入已结', () => {
    expect(isVerifySideStatus('建议')).toBe(true)
    expect(isVerifySideStatus('已忽略')).toBe(true)
    expect(isVerifySideStatus('待核查')).toBe(false)
    expect(isVerifyConcluded('已证实')).toBe(true)
    expect(isVerifyConcluded('已查否')).toBe(true)
    expect(isVerifyConcluded('无法核实')).toBe(true)
    expect(isVerifyConcluded('建议')).toBe(false)
  })

  it('kind 徽标文案；origin=manual 人工标记', () => {
    expect(verifyKindLabel('manual')).toBe('人工')
    expect(verifyKindLabel('suggested')).toBe('手册建议')
    expect(verifyKindLabel('inference')).toBe('推断')
    expect(verifyKindLabel('future_kind')).toBe('future_kind') // 未知原样透出
    expect(isManualOrigin('manual')).toBe(true)
    expect(isManualOrigin('suggested')).toBe(false)
  })

  it('verifyProgressPercent：已结/总数（零安全）', () => {
    const p: VerifyProgress = {
      total: 4, concluded: 3, pending: 1, suggested: 0, ignored: 0, by_status: {},
    }
    expect(verifyProgressPercent(p)).toBe(75)
    expect(verifyProgressPercent(null)).toBe(0)
    expect(verifyProgressPercent({ ...p, total: 0 })).toBe(0)
  })
})
