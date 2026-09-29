// 持有链蛇形布局（WIN-11）单测。
//
// 这里守的是三条红线，不是几何美观：
//   1. 时间不可判定的环节不进链序（不排≠不在）；
//   2. 错序按登记顺序画，不按时间重排（重排会抹掉矛盾信号）；
//   3. 非相邻边（重叠）必须绕行，直线穿过中间节点会被读成"中间那手也参与了"。
import { describe, expect, it } from 'vitest'
import {
  CHAIN_GAP_Y,
  CHAIN_NODE_H,
  CHAIN_NODE_W,
  buildChainLayout,
  chainOverlapPairs,
  channelYOf,
  placeSnake,
  rangeTextOf,
  snakePath,
  splitChainSteps,
  type ChainStep,
} from '../src/domain/canvas-layout-chain'

function S(
  id: string,
  holder: string,
  start = '',
  end = '',
  unknown = false,
): ChainStep {
  return { id, holder, start, end, order: Number(id.slice(1)), unknownRange: unknown || !start }
}

/** 从 path 提取所有顶点（含 Q 的控制点）——用于端点断言（如折返是否垂直） */
function pointsOfPath(d: string): Array<[number, number]> {
  const out: Array<[number, number]> = []
  const re = /([MLQ])\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+)\s+([-\d.]+))?/g
  let m: RegExpExecArray | null
  while ((m = re.exec(d)) !== null) {
    out.push([Number(m[2]), Number(m[3])])
    if (m[1] === 'Q' && m[4] !== undefined) out.push([Number(m[4]), Number(m[5])])
  }
  return out
}

/**
 * 沿路径**采样**（含贝塞尔段内部的点）。
 *
 * 为什么不能只看顶点：一条从 a 底边斜拉到 b 顶边的直线，两个端点都不在任何
 * 节点盒内部，只看顶点的断言会**恒真通过**——而这条线实际上横穿了中间环节。
 * 反向验证抓到过这个漏洞（把重叠边改成直线后断言仍通过），所以这里必须采样。
 */
function samplePath(d: string, per = 16): Array<[number, number]> {
  const out: Array<[number, number]> = []
  const re = /([MLQ])((?:\s+[-\d.]+)+)/g
  let m: RegExpExecArray | null
  let cx = 0
  let cy = 0
  while ((m = re.exec(d)) !== null) {
    const nums = m[2].trim().split(/\s+/).map(Number)
    if (m[1] === 'M') {
      cx = nums[0]
      cy = nums[1]
      out.push([cx, cy])
    } else if (m[1] === 'L') {
      const [x, y] = nums
      for (let i = 1; i <= per; i += 1) {
        const t = i / per
        out.push([cx + (x - cx) * t, cy + (y - cy) * t])
      }
      cx = x
      cy = y
    } else {
      // Q：二次贝塞尔，P0=当前点、C=控制点、P1=终点
      const [qx, qy, x, y] = nums
      for (let i = 1; i <= per; i += 1) {
        const t = i / per
        const mt = 1 - t
        out.push([
          mt * mt * cx + 2 * mt * t * qx + t * t * x,
          mt * mt * cy + 2 * mt * t * qy + t * t * y,
        ])
      }
      cx = x
      cy = y
    }
  }
  return out
}

describe('蛇形排布', () => {
  it('偶数行左→右，奇数行右→左（折返）', () => {
    const a = placeSnake(S('h0', '甲', '2020-01-01'), 0, 3)
    const b = placeSnake(S('h1', '乙', '2020-02-01'), 1, 3)
    const c = placeSnake(S('h2', '丙', '2020-03-01'), 2, 3)
    // 第一行：col 递增 → x 递增
    expect(a.x).toBeLessThan(b.x)
    expect(b.x).toBeLessThan(c.x)
    // 第二行首个（idx=3）应折返到最右，与 c 同列同 x
    const d = placeSnake(S('h3', '丁', '2020-04-01'), 3, 3)
    expect(d.row).toBe(1)
    expect(d.x).toBe(c.x)
    expect(d.dir).toBe(-1)
  })

  it('折返行的第二个向左走', () => {
    const d = placeSnake(S('h3', '丁', '2020-04-01'), 3, 3)
    const e = placeSnake(S('h4', '戊', '2020-05-01'), 4, 3)
    expect(e.x).toBeLessThan(d.x)
  })
})

describe('红线 1：不可判定不进链序', () => {
  it('可判定进链、不可判定单独成行', () => {
    const { ordered, detached } = splitChainSteps([
      S('h0', '甲', '2020-01-01', '2020-02-01'),
      S('h1', '乙'),
      S('h2', '丙', '2020-03-01', '2020-04-01'),
    ])
    expect(ordered.map((s) => s.holder)).toEqual(['甲', '丙'])
    expect(detached.map((s) => s.holder)).toEqual(['乙'])
  })

  it('不可判定环节不连任何边', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2020-01-01', '2020-02-01'),
      S('h1', '乙'),
      S('h2', '丙', '2020-03-01', '2020-04-01'),
    ])
    const detachedIds = new Set(l.detached.map((n) => n.id))
    expect(detachedIds.has('h1')).toBe(true)
    for (const e of l.edges) {
      expect(detachedIds.has(e.from)).toBe(false)
      expect(detachedIds.has(e.to)).toBe(false)
    }
  })

  it('全不可判定时没有链可画（flat 由 buildHoldingWindow 判定）', () => {
    const l = buildChainLayout([S('h0', '甲'), S('h1', '乙')])
    expect(l.nodes).toHaveLength(0)
    expect(l.detached).toHaveLength(2)
  })

  it('区间文案自陈：不可判定与"在持"都要说清', () => {
    expect(rangeTextOf('', '', true)).toBe('时间不可判定')
    expect(rangeTextOf('2020-01-01', '', false)).toBe('2020-01-01 → 在持')
    expect(rangeTextOf('2020-01-01', '2020-02-01', false)).toBe('2020-01-01 → 2020-02-01')
  })
})

describe('红线 2：错序不重排', () => {
  it('链按登记顺序，错序标在边上', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2020-03-01', '2020-05-01'),
      S('h1', '乙', '2019-01-01', '2020-01-01'),
    ])
    expect(l.nodes[0].holder).toBe('甲')
    expect(l.nodes[1].holder).toBe('乙')
    expect(l.edges[0].outOfOrder).toBe(true)
  })

  it('时序自洽时不标错序', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2019-01-01', '2019-06-01'),
      S('h1', '乙', '2020-01-01', '2020-06-01'),
    ])
    expect(l.edges.every((e) => !e.outOfOrder)).toBe(true)
  })
})

describe('红线 3：重叠边绕行，不穿过中间环节', () => {
  it('重叠对检测：区间相交即冲突', () => {
    const pairs = chainOverlapPairs([
      S('h0', '甲', '2020-03-01', '2020-08-01'),
      S('h1', '乙', '2020-05-01', '2020-09-01'),
    ])
    expect(pairs).toHaveLength(1)
  })

  it('标记为不可判定的环节不参与重叠判定（即便它写了区间）', () => {
    // 用例必须取"区间存在但 unknownRange=true"。若用完全空的区间，
    // 字符串比较下空串恒最小，过滤与否**结果完全相同**，断言会恒真——
    // 反向验证抓到过这个漏洞（把过滤去掉后断言照样通过）。
    const pairs = chainOverlapPairs([
      S('h0', '甲', '2020-03-01', '2020-08-01'),
      S('h1', '乙', '2020-04-01', '2020-12-01', true),
    ])
    expect(pairs).toHaveLength(0)
  })

  it('上一条不能靠"永远返回空"蒙混：可判定且真重叠时必须报冲突', () => {
    const pairs = chainOverlapPairs([
      S('h0', '甲', '2020-03-01', '2020-08-01'),
      S('h1', '乙', '2020-04-01', '2020-12-01'),
    ])
    expect(pairs).toHaveLength(1)
  })

  it('缺区间时是"不知道"，不得静默判为重叠', () => {
    const pairs = chainOverlapPairs([
      S('h0', '甲', '2020-03-01', '2020-08-01'),
      S('h1', '乙'),
    ])
    expect(pairs).toHaveLength(0)
  })

  it('同行非相邻的边走行间通道，不穿过中间环节盒', () => {
    // 三手同在一行：h0 与 h2 区间重叠，中间夹着 h1
    const l = buildChainLayout([
      S('h0', '甲', '2020-01-01', '2020-08-01'),
      S('h1', '乙', '2020-02-01', '2020-03-01'),
      S('h2', '丙', '2020-07-01', '2020-09-01'),
    ])
    const mid = l.nodes.find((n) => n.id === 'h1')!
    const overlap = l.edges.find((e) => e.kind === 'overlap')!
    expect(overlap).toBeTruthy()
    expect(overlap.detoured).toBe(true)
    for (const [px, py] of samplePath(overlap.d)) {
      const inside =
        px > mid.x && px < mid.x + mid.w && py > mid.y && py < mid.y + mid.h
      expect(inside).toBe(false)
    }
  })

  it('相邻流转边不绕行（紧挨着的两手不该绕）', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2019-01-01', '2019-06-01'),
      S('h1', '乙', '2020-01-01', '2020-06-01'),
    ])
    expect(l.edges).toHaveLength(1)
    expect(l.edges[0].detoured).toBe(false)
  })

  it('折返处是垂直直线（行尾→下行行首同列）', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2019-01-01', '2019-02-01'),
      S('h1', '乙', '2019-03-01', '2019-04-01'),
      S('h2', '丙', '2019-05-01', '2019-06-01'),
      S('h3', '丁', '2019-07-01', '2019-08-01'),
    ])
    const crossing = l.edges.find((e) => e.from === 'h2' && e.to === 'h3')!
    expect(crossing.d.startsWith('M')).toBe(true)
    // 垂直：两端 x 相同
    const pts = pointsOfPath(crossing.d)
    expect(pts[0][0]).toBe(pts[pts.length - 1][0])
  })

  it('通道 y 不与任何节点盒相交（这是绕行成立的前提）', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2020-01-01', '2020-02-01'),
      S('h1', '乙', '2020-03-01', '2020-04-01'),
    ])
    for (const n of l.nodes) {
      const cy = channelYOf(n.row)
      expect(cy > n.y + n.h).toBe(true)
      expect(cy < n.y + n.h + CHAIN_GAP_Y).toBe(true)
    }
  })
})

describe('path 生成', () => {
  it('纯垂直时走直线，不画无意义折角', () => {
    const d = snakePath(100, 50, 100, 120, 85)
    expect(d).toBe('M 100 50 L 100 120')
  })

  it('已在通道上时直接横过去', () => {
    const d = snakePath(10, 80, 200, 80, 80)
    expect(d).toBe('M 10 80 L 200 80')
  })

  it('横向段极短时圆角收敛为 0，不过冲', () => {
    const d = snakePath(100, 50, 101, 120, 85)
    // 段长 1px，圆角按最短段收敛到 0 → 退化为折线，不应出现 Q
    expect(d).not.toContain('Q')
  })

  it('正常段保留圆角（S 型观感）', () => {
    const d = snakePath(20, 50, 300, 200, 90)
    expect(d).toContain('Q')
  })

  it('路径点坐标保留两位小数，不产生浮点长尾', () => {
    const d = snakePath(20.123456, 50.987654, 300.111, 200.222, 90.5)
    for (const n of d.match(/[-\d.]+/g) ?? []) {
      const dec = n.split('.')[1]
      expect((dec ?? '').length).toBeLessThanOrEqual(2)
    }
  })
})

describe('版面尺寸', () => {
  it('宽高覆盖所有盒（含不可判定行）', () => {
    const l = buildChainLayout([
      S('h0', '甲', '2020-01-01', '2020-02-01'),
      S('h1', '乙', '2020-03-01', '2020-04-01'),
      S('h2', '丙'),
    ])
    const all = [...l.nodes, ...l.detached]
    for (const n of all) {
      expect(n.x + n.w).toBeLessThanOrEqual(l.width)
      expect(n.y + n.h).toBeLessThanOrEqual(l.height)
    }
  })

  it('盒尺寸常量与卡面一致（改了要同步样式）', () => {
    expect(CHAIN_NODE_W).toBe(132)
    expect(CHAIN_NODE_H).toBe(46)
  })
})
