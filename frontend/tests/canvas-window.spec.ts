// 节点窗口模型（WIN-02~11）：分化、定位、矩阵、持有链、引擎槽位。
import { beforeEach, describe, expect, it } from 'vitest'
import {
  WINDOW_BOX,
  buildEvidenceWindow,
  buildHoldingWindow,
  buildMapWindow,
  buildRelationWindow,
  buildTimeWindow,
  dimCellsOf,
  placeWindow,
  toSinglePointGeoModel,
  windowKindFor,
  type WindowNode,
} from '../src/domain/canvas-window'
import { acquireEngineSlot, releaseEngineSlot, resetEngineSlot } from '../src/domain/map-engine-slot'

const N = (id: string, kind: string, label: string, props?: Record<string, unknown>): WindowNode => ({
  id,
  kind,
  label,
  props,
})

describe('窗口分化', () => {
  it('subject→关系 / place→地图 / event→时间 / analysis_result→证据 / item→持有链', () => {
    expect(windowKindFor(N('a', 'subject', '张卫国')).kind).toBe('relation')
    expect(windowKindFor(N('b', 'place', '莫干山路')).kind).toBe('map')
    expect(windowKindFor(N('c', 'event', '3-10 见面')).kind).toBe('time')
    expect(windowKindFor(N('d', 'analysis_result', '异常轨迹')).kind).toBe('evidence')
    expect(windowKindFor(N('e', 'item', '浙A·12345')).kind).toBe('holding')
  })

  it('无专属窗口的节点给原因，不弹空窗', () => {
    const r = windowKindFor(N('f', 'fact', '某事实'))
    expect(r.kind).toBeNull()
    expect(r.reason).toContain('无专属窗口')
  })
})

describe('WIN-02 贴附定位与翻转', () => {
  it('默认落在锚点右下方', () => {
    const p = placeWindow({ x: 100, y: 100 }, { width: 1200, height: 800 })
    expect(p.left).toBe(114)
    expect(p.top).toBe(114)
    expect(p.flippedX).toBe(false)
  })

  it('右侧越界翻到左侧', () => {
    const p = placeWindow({ x: 1100, y: 100 }, { width: 1200, height: 800 })
    expect(p.flippedX).toBe(true)
    expect(p.left + WINDOW_BOX.width).toBeLessThanOrEqual(1100)
  })

  it('下方越界上移', () => {
    const p = placeWindow({ x: 100, y: 780 }, { width: 1200, height: 800 })
    expect(p.flippedY).toBe(true)
    expect(p.top + WINDOW_BOX.height).toBeLessThanOrEqual(780)
  })

  it('翻转后夹紧到视口内，不溢出左/上边界', () => {
    const p = placeWindow({ x: 4, y: 4 }, { width: 300, height: 200 })
    expect(p.left).toBeGreaterThanOrEqual(WINDOW_BOX.margin)
    expect(p.top).toBeGreaterThanOrEqual(WINDOW_BOX.margin)
  })
})

describe('WIN-03 关系窗口', () => {
  const center = N('s1', 'subject', '张卫国', { person_pk: 'person_ecb' })
  const other = N('s2', 'subject', '李志强', { person_pk: 'person_07e' })
  const far = N('s3', 'subject', '王五', { person_pk: 'person_aaa' })

  it('排序按加权分而非裸次数：3 次时刻级压过 15 次日期级', () => {
    const w = buildRelationWindow(
      center,
      [
        { source: 's1', target: 's2', rel: '同框', data: { count: 3, precision: 'minute', system: false } },
        { source: 's1', target: 's3', rel: '同框', data: { count: 15, precision: 'date', system: false } },
      ],
      new Map([['s2', other], ['s3', far]]),
    )
    expect(w.rows[0].id).toBe('s2')
    expect(w.rows[0].weight).toBe(1.4) // 1.0 × repeat(3)=1.4
    expect(w.rows[1].weight).toBe(0.6) // 0.2 × repeat(15)=3.0（封顶）
    expect(w.rows[0].count).toBeLessThan(w.rows[1].count)
    // 同加权分（都是 3.0）时的兜底：精度强者在前，绝不退回"次数多者在前"
    expect(w.rows[0].precision).toBe('minute')
    expect(w.rows[1].precision).toBe('date')
  })

  it('无关联时写明原因', () => {
    const w = buildRelationWindow(center, [], new Map())
    expect(w.empty).toContain('暂无')
  })

  it('未锚定主体明示无法检索', () => {
    const w = buildRelationWindow(N('x', 'subject', '张卫国'), [], new Map())
    expect(w.empty).toContain('未锚定')
  })
})

describe('WIN-04 地图窗口', () => {
  it('门牌级出点，无坐标时给原因且不消失', () => {
    const ok = buildMapWindow(N('p1', 'place', '莫干山路', { lng: 120.1, lat: 30.3, coord_precise: true }))
    expect(ok.point?.precise).toBe(true)
    expect(ok.unmappable).toBe('')

    const bad = buildMapWindow(N('p2', 'place', '某地点'))
    expect(bad.point).toBeNull()
    expect(bad.unmappable).toContain('无可用经纬度')
  })

  it('区划质心带自陈提示', () => {
    const m = buildMapWindow(N('p3', 'place', '西湖区', { lng: 120.1, lat: 30.3, coord_precise: false }))
    expect(m.point?.precise).toBe(false)
    expect(m.coordsNote).toContain('区级推算')
  })

  it('单点模型不把地点伪装成交汇锚点', () => {
    const m = buildMapWindow(N('p4', 'place', '莫干山路', { lng: 120.1, lat: 30.3, coord_precise: true }))
    const g = toSinglePointGeoModel(m.point!)
    expect(g.points).toHaveLength(1)
    expect(g.note).toContain('不代表任何聚集形态')
    expect(g.bounds.maxLat).toBeGreaterThan(g.bounds.minLat)
  })
})

describe('WIN-05 时间窗口', () => {
  it('区分可判重叠与仅同地异时', () => {
    const w = buildTimeWindow(N('e1', 'event', '3-10'), [
      { id: 'a', at: '2020-03-10 14:35', precision: 'minute', dim: 'space' },
      { id: 'b', at: '2020-03-17 15:20', precision: 'minute', dim: 'space' },
      { id: 'c', at: '2020-03-24', precision: 'date', dim: 'time' },
    ])
    expect(w.determinableCount).toBe(2)
    expect(w.degradedCount).toBe(1)
    expect(w.rows.map((r) => r.id)).toEqual(['a', 'b', 'c'])
  })

  it('无时刻时写明原因', () => {
    expect(buildTimeWindow(N('e2', 'event', 'x'), []).empty).toContain('无可用时刻')
  })
})

describe('WIN-06 证据窗口', () => {
  const props = {
    score: 3.6,
    dims: {
      space: { hit: 1, count: 4, precision: 'minute', weight: 1.6 },
      time: { hit: 1, count: 12, precision: 'date', weight: 0.6 },
      relation: { hit: 1, count: 3, precision: 'minute', weight: 1.4 },
    },
  }

  it('三维矩阵必带 count/precision/weight', () => {
    const w = buildEvidenceWindow(N('r1', 'analysis_result', '交汇', props))
    expect(w.scoreAlone).toBe(true)
    for (const c of w.dims) {
      expect(typeof c.count).toBe('number')
      expect(c.precision).not.toBe('')
      expect(typeof c.weight).toBe('number')
    }
    const time = w.dims.find((d) => d.dim === 'time')!
    expect(time.count).toBe(12)
    expect(time.precision).toBe('date')
    expect(time.weight).toBe(0.6)
  })

  it('未命中维度保留并写明原因', () => {
    const w = buildEvidenceWindow(N('r2', 'analysis_result', '仅有空间', {
      dims: { space: { hit: 1, count: 2, precision: 'minute', weight: 2 } },
    }))
    const miss = w.dims.filter((d) => !d.hit)
    expect(miss).toHaveLength(2)
    expect(miss[0].reason).toContain('无支撑观察')
  })

  it('支撑观察带 obs_id 可回溯', () => {
    const w = buildEvidenceWindow(N('r3', 'analysis_result', 'x', props), [
      { obs_id: 'obs_001', label: '轨迹异常', precision: 'minute', dim: 'space' },
    ])
    expect(w.supports[0].obs_id).toBe('obs_001')
  })

  it('dimCellsOf 对空 props 不抛异常', () => {
    const cells = dimCellsOf(undefined)
    expect(cells).toHaveLength(3)
    expect(cells.every((c) => !c.hit)).toBe(true)
  })

  it('dim_cells 对象口径（growth 重建层）直接成矩阵', () => {
    const cells = dimCellsOf({
      dim_cells: { time: { hit: true, count: 1, precision: 'date', weight: 0.2 } },
      dims: ['time'],
    })
    const time = cells.find((c) => c.dim === 'time')!
    expect(time.hit).toBe(true)
    expect(time.count).toBe(1)
    expect(time.precision).toBe('date')
    expect(time.weight).toBe(0.2)
    // 其余两维未命中而非丢失
    expect(cells.filter((c) => !c.hit)).toHaveLength(2)
  })

  it('dims 字符串数组兜底：列出维度判命中，权重回退节点档', () => {
    const cells = dimCellsOf({
      dims: ['relation'],
      precision: 'unknown',
      precision_weight: 0,
    })
    const relation = cells.find((c) => c.dim === 'relation')!
    expect(relation.hit).toBe(true)
    expect(relation.weight).toBe(0)
    expect(cells.filter((c) => !c.hit)).toHaveLength(2)
  })

  it('dims 字符串数组不得让加权分被抬成非零（数组无精度信息）', () => {
    const w = buildEvidenceWindow(
      N('r4', 'analysis_result', '关系结论', { dims: ['relation'] }))
    expect(w.score).toBe(0)
    expect(w.dims.find((d) => d.dim === 'relation')!.hit).toBe(true)
  })
})

describe('WIN-09 持有链', () => {
  it('时序错序可辨', () => {
    const w = buildHoldingWindow(N('i1', 'item', '车'), [
      { holder: '甲', start: '2020-03-01', end: '2020-05-01' },
      { holder: '乙', start: '2019-01-01', end: '2020-01-01' },
    ])
    expect(w.outOfOrder).toBe(true)
    expect(w.note).toContain('错序')
  })

  it('区间重叠时提示需人工判定', () => {
    const w = buildHoldingWindow(N('i2', 'item', '车'), [
      { holder: '甲', start: '2020-03-01', end: '2020-08-01' },
      { holder: '乙', start: '2020-05-01', end: '2020-09-01' },
    ])
    expect(w.overlapping).toBe(true)
    expect(w.note).toContain('人工判定')
  })

  it('缺区间时是"不知道"而不是"没有"', () => {
    const w = buildHoldingWindow(N('i3', 'item', '车'), [{ holder: '甲' }])
    expect(w.links[0].unknownRange).toBe(true)
    expect(w.note).toContain('不可判定')
  })

  it('WIN-11 无可排序环节时退回列表（flat，不是降级）', () => {
    const w = buildHoldingWindow(N('i4', 'item', '车'), [])
    expect(w.layoutKind).toBe('flat')
    expect(w.layout).toBeNull()
  })

  it('WIN-11 有可排序环节时走蛇形布局', () => {
    const w = buildHoldingWindow(N('i5', 'item', '车'), [
      { holder: '甲', start: '2020-03-01', end: '2020-05-01' },
      { holder: '乙', start: '2020-06-01', end: '2020-08-01' },
    ])
    expect(w.layoutKind).toBe('snake')
    expect(w.layout?.nodes).toHaveLength(2)
    expect(w.layout?.detached).toHaveLength(0)
  })

  it('WIN-11 时间不可判定的环节不进链序（不排≠不在）', () => {
    const w = buildHoldingWindow(N('i6', 'item', '车'), [
      { holder: '甲', start: '2020-03-01', end: '2020-05-01' },
      { holder: '乙' },
    ])
    expect(w.layoutKind).toBe('snake')
    expect(w.layout?.nodes).toHaveLength(1)
    // 不能消失——从窗口里没了会被读成"这条链只有一手"
    expect(w.layout?.detached).toHaveLength(1)
    expect(w.layout?.detached[0].holder).toBe('乙')
    // 不可判定环节不连任何边，连了就等于给了它一个链序位置
    const touching = (w.layout?.edges ?? []).some(
      (e) => e.from === w.layout?.detached[0].id || e.to === w.layout?.detached[0].id,
    )
    expect(touching).toBe(false)
  })

  it('WIN-11 错序按登记顺序画链，不按时间重排', () => {
    const w = buildHoldingWindow(N('i7', 'item', '车'), [
      { holder: '甲', start: '2020-03-01', end: '2020-05-01' },
      { holder: '乙', start: '2019-01-01', end: '2020-01-01' },
    ])
    // 链序仍是甲→乙（登记顺序）；重排会把"登记顺序与时间矛盾"这个信号抹掉
    expect(w.layout?.nodes[0].holder).toBe('甲')
    expect(w.layout?.nodes[1].holder).toBe('乙')
    expect(w.layout?.edges.some((e) => e.outOfOrder)).toBe(true)
  })
})

describe('WIN-07 在线引擎单实例', () => {
  beforeEach(() => resetEngineSlot())

  it('第二个窗口申请槽位失败，须回退离线', () => {
    expect(acquireEngineSlot('w1')).toBe(true)
    expect(acquireEngineSlot('w2')).toBe(false)
  })

  it('释放后可再次申请', () => {
    acquireEngineSlot('w1')
    releaseEngineSlot('w1')
    expect(acquireEngineSlot('w3')).toBe(true)
  })
})
