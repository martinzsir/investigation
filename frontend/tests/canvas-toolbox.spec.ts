import { describe, expect, it } from 'vitest'
import {
  autoFillParams,
  isToolboxRunnable,
  lensDisabledReason,
  lensReadyHint,
  missingRequiredParams,
  toolboxLensAvailable,
} from '../src/domain/canvas-toolbox'
import type { LensSpecItem } from '../src/api/endpoints/lenses'
import type { ToolboxNode } from '../src/domain/canvas-toolbox'

// P3 案件画布镜头工具箱纯函数契约（PRD V1.0.0 功能 4）。
// 红线：工具箱可用性与「自动批量跑」彻底分离——案件覆盖（case_override）
// 优先，未覆盖回落包级画布白名单（pack_canvas_enabled），与 enabled 无关；
// 靶心带入只带主参数（pair_with 从参数留手填）；缺失必填只禁跑不报错。

function mkLens(over: Partial<LensSpecItem> = {}): LensSpecItem {
  return {
    skill_id: 'geo_site_profile',
    name: '落脚点画像',
    stage: 'survey',
    mode: 'deterministic',
    pack_id: 'geo',
    pack_enabled: true,
    case_override: null,
    enabled: true,
    canvas_enabled: true,
    pack_canvas_enabled: true,
    requires_params: false,
    params_schema: {},
    ...over,
  }
}

const subjectNode: ToolboxNode = {
  id: 'case#cn_1',
  kind: 'subject',
  label: '张卫国',
  props: { person_pk: 'p_zwg', person_name: '张卫国' },
}

describe('toolboxLensAvailable（案件画布可用性，与 enabled 分离）', () => {
  it('案件覆盖优先：覆盖 true 时即使包未声明画布白名单也可用', () => {
    expect(toolboxLensAvailable(mkLens({
      case_override: true,
      pack_canvas_enabled: false,
    }))).toBe(true)
  })

  it('案件覆盖 false 恒不可用（覆盖压过包级白名单）', () => {
    expect(toolboxLensAvailable(mkLens({
      case_override: false,
      pack_canvas_enabled: true,
    }))).toBe(false)
  })

  it('未覆盖回落包级白名单：pack_canvas_enabled=true / 缺省 undefined 不可用', () => {
    expect(toolboxLensAvailable(mkLens({
      case_override: null,
      pack_canvas_enabled: true,
    }))).toBe(true)
    expect(toolboxLensAvailable(mkLens({
      case_override: null,
      pack_canvas_enabled: undefined,
      // enabled=true 不参与：批量跑 ≠ 画布手动跑
      enabled: true,
    }))).toBe(false)
  })
})

describe("lensDisabledReason（禁跑原因，'' = 可运行）", () => {
  it('不可用 → 案件级停用提示', () => {
    expect(lensDisabledReason(mkLens({ case_override: false })))
      .toBe('已在启停面板停用（案件级覆盖）')
    expect(lensDisabledReason(mkLens({ pack_canvas_enabled: undefined })))
      .toBe('已在启停面板停用（案件级覆盖）')
  })

  it('草案镜头禁跑（后端 400 兜底，前端先拒）', () => {
    expect(lensDisabledReason(mkLens({ mode: 'draft' })))
      .toBe('草案镜头：产出须经人验，不支持直接调度')
  })

  it('可用且确定性 → 可运行', () => {
    expect(lensDisabledReason(mkLens())).toBe('')
  })
})

describe('lensReadyHint（就绪度只提示不禁跑）', () => {
  it('无 readiness 或已就绪 → 无提示', () => {
    expect(lensReadyHint(mkLens())).toBe('')
    expect(lensReadyHint(mkLens({
      readiness: { skill_id: 'x', name: 'x', ready: true, missing: [], deps: [], note: '' },
    }))).toBe('')
  })

  it('缺数据 → 列出缺失依赖（前 3 个），超出加「 等」', () => {
    const hint = lensReadyHint(mkLens({
      readiness: {
        skill_id: 'x', name: 'x', ready: false,
        missing: ['轨迹', '通话', '资金', '工商'], deps: [], note: '',
      },
    }))
    expect(hint).toBe('缺数据：轨迹、通话、资金 等')
    expect(lensReadyHint(mkLens({
      readiness: {
        skill_id: 'x', name: 'x', ready: false,
        missing: ['轨迹'], deps: [], note: '',
      },
    }))).toBe('缺数据：轨迹')
  })

  it('缺数据但 missing 全空 → 回落 note', () => {
    expect(lensReadyHint(mkLens({
      readiness: {
        skill_id: 'x', name: 'x', ready: false,
        missing: [''], deps: [], note: '依赖未接入',
      },
    }))).toBe('依赖未接入')
  })
})

describe('autoFillParams（靶心自动带入）', () => {
  it('null 节点 → 空建议', () => {
    const r = autoFillParams(mkLens(), null)
    expect(r.values).toEqual({})
    expect(r.autoKeys).toEqual([])
  })

  it('subject 节点：person_pk 优先于 person_name', () => {
    const l = mkLens({
      params_schema: {
        target_subject: {
          type: 'string', required: true,
          auto_from: { object_types: ['person'] },
        },
      },
    })
    const r = autoFillParams(l, subjectNode)
    expect(r.values).toEqual({ target_subject: 'p_zwg' })
    expect(r.autoKeys).toEqual(['target_subject'])
    // 无 pk → 名称兜底（auto 消歧）
    const r2 = autoFillParams(l, {
      ...subjectNode, props: { person_name: '张卫国' },
    })
    expect(r2.values.target_subject).toBe('张卫国')
    // 名称也没有 → label 兜底
    const r3 = autoFillParams(l, { ...subjectNode, props: {} })
    expect(r3.values.target_subject).toBe('张卫国')
  })

  it('pair_with 从参数（subject_b）不带：靶心只有一个主体', () => {
    const l = mkLens({
      params_schema: {
        subject_a: {
          type: 'string', required: true,
          auto_from: { object_types: ['person'] },
        },
        subject_b: {
          type: 'string', required: true,
          auto_from: { object_types: ['person'], pair_with: 'subject_a' },
        },
      },
    })
    const r = autoFillParams(l, subjectNode)
    expect(r.values).toEqual({ subject_a: 'p_zwg' })
    expect(r.autoKeys).toEqual(['subject_a'])
  })

  it('place 节点：location 参数带 location_id，lng/lat 尾缀带坐标', () => {
    const l = mkLens({
      params_schema: {
        location_id: { type: 'string', required: true },
        center_lng: { type: 'decimal' },
        center_lat: { type: 'decimal' },
        window_days: { type: 'integer', required: true },
      },
    })
    const node: ToolboxNode = {
      id: 'case#cn_2', kind: 'place', label: '莫干山路 111 号',
      props: { location_id: 'loc-1', lng: 120.1, lat: 30.2 },
    }
    const r = autoFillParams(l, node)
    expect(r.values).toEqual({
      location_id: 'loc-1',
      center_lng: '120.1',
      center_lat: '30.2',
    })
    expect(r.autoKeys).toEqual(['location_id', 'center_lng', 'center_lat'])
  })

  it('place 脏值不带：location_id 非串 / 坐标非数字 / 数值参数永不自动猜', () => {
    const node: ToolboxNode = {
      id: 'case#cn_2', kind: 'place', label: 'x',
      props: { location_id: 42, lng: '120.1', lat: null },
    }
    const r = autoFillParams(mkLens({
      params_schema: {
        location_id: { type: 'string' },
        center_lng: { type: 'decimal' },
        center_lat: { type: 'decimal' },
      },
    }), node)
    expect(r.values).toEqual({})
    expect(r.autoKeys).toEqual([])
  })

  it('event 等其它 kind 不自动带入（不猜）', () => {
    const r = autoFillParams(mkLens({
      params_schema: {
        target_subject: {
          type: 'string', required: true,
          auto_from: { object_types: ['person'] },
        },
      },
    }), { id: 'case#cn_3', kind: 'event', label: '到访', props: {} })
    expect(r.values).toEqual({})
  })
})

describe('missingRequiredParams / isToolboxRunnable（禁跑唯一依据）', () => {
  const l = mkLens({
    params_schema: {
      target_subject: { type: 'string', required: true },
      window_days: { type: 'integer', required: true },
      note: { type: 'string' },
    },
  })

  it('缺失必填列出；空串算缺；非必填缺不列', () => {
    expect(missingRequiredParams(l, {}))
      .toEqual(['target_subject', 'window_days'])
    expect(missingRequiredParams(l, { target_subject: '  ', window_days: 7 }))
      .toEqual(['target_subject'])
    expect(missingRequiredParams(l, { target_subject: '张三', window_days: 7 }))
      .toEqual([])
  })

  it('isToolboxRunnable = 可用 ∧ 确定性 ∧ 必填齐', () => {
    expect(isToolboxRunnable(l, { target_subject: '张三', window_days: 7 }))
      .toBe(true)
    expect(isToolboxRunnable(l, {})).toBe(false)
    expect(isToolboxRunnable(mkLens({ mode: 'draft' }), {})).toBe(false)
    expect(isToolboxRunnable(mkLens({ case_override: false }), {})).toBe(false)
  })
})
