import { describe, expect, it } from 'vitest'
import type { CaseCanvasLensGroup } from '../src/domain/canvas'
import {
  META_REVEALED_GROUPS,
  allRevealedKeys,
  groupRowsByTarget,
  parseRevealKey,
  readRevealedGroups,
  revealKey,
  serializeRevealedGroups,
  toggleRevealedGroup,
  unrevealedCountByTarget,
  withRevealedGroups,
} from '../src/domain/case-growth'

// 研判画布渐进式揭示集（v3 §6）纯逻辑契约：
// 揭示集 = (靶心, 镜头) 组集合，持久在 doc.meta.revealed_groups，
// 缺省空集=最小画布；脏值 fail-closed；序列化稳定；派生清单供面板/角标。

function group(
  target: string,
  lens: string,
  extra: Partial<CaseCanvasLensGroup> = {},
): CaseCanvasLensGroup {
  return {
    target_node_id: target,
    lens_id: lens,
    lens_name: lens,
    target_label: '',
    on_canvas: true,
    revealed: false,
    observation_count: 1,
    ...extra,
  }
}

describe('揭示集读写（与后端 canvas_case_doc 同键同纪律）', () => {
  it('meta 键名与后端一致：revealed_groups', () => {
    expect(META_REVEALED_GROUPS).toBe('revealed_groups')
  })

  it('缺省/非数组/脏元素一律降级空集（fail-closed，不多长）', () => {
    expect(readRevealedGroups(undefined).size).toBe(0)
    expect(readRevealedGroups(null).size).toBe(0)
    expect(readRevealedGroups({}).size).toBe(0)
    expect(readRevealedGroups({ revealed_groups: null }).size).toBe(0)
    expect(readRevealedGroups({ revealed_groups: 'x' }).size).toBe(0)
    expect(readRevealedGroups({ revealed_groups: [null, 1, 's', {}] }).size).toBe(0)
    expect(
      readRevealedGroups({ revealed_groups: [{ target: 't' }] }).size,
    ).toBe(0)
    expect(
      readRevealedGroups({ revealed_groups: [{ target: '', lens: 'l' }] }).size,
    ).toBe(0)
  })

  it('合法条目解析，空白被 trim', () => {
    const keys = readRevealedGroups({
      revealed_groups: [
        { target: 'case#c1:subject:p1', lens: 'fund_overpass_two_hop' },
        { target: ' p2 ', lens: ' comm_x ' },
      ],
    })
    expect(keys.size).toBe(2)
    expect(keys.has(revealKey('case#c1:subject:p1', 'fund_overpass_two_hop'))).toBe(true)
    expect(keys.has(revealKey('p2', 'comm_x'))).toBe(true)
  })

  it('键往返 parseRevealKey；畸形键返回 null', () => {
    const k = revealKey('t', 'l')
    expect(parseRevealKey(k)).toEqual({ target: 't', lens: 'l' })
    expect(parseRevealKey('nosep')).toBeNull()
    expect(parseRevealKey('␟l')).toBeNull()
    expect(parseRevealKey('t␟')).toBeNull()
  })

  it('序列化按 target/lens 排序，文档稳定', () => {
    const keys = new Set([
      revealKey('b', 'z'),
      revealKey('a', 'm'),
      revealKey('a', 'a'),
    ])
    expect(serializeRevealedGroups(keys)).toEqual([
      { target: 'a', lens: 'a' },
      { target: 'a', lens: 'm' },
      { target: 'b', lens: 'z' },
    ])
  })
})

describe('集合变更不可变 + 文档投影', () => {
  it('toggle 增删返回新集合，不改入参', () => {
    const base = new Set([revealKey('t', 'l')])
    const added = toggleRevealedGroup(base, 't2', 'l2', true)
    expect(added.size).toBe(2)
    expect(base.size).toBe(1) // 原集合不动
    const removed = toggleRevealedGroup(added, 't', 'l', false)
    expect(removed.has(revealKey('t', 'l'))).toBe(false)
    expect(added.size).toBe(2)
  })

  it('withRevealedGroups 保留其他 meta 且不改原文档', () => {
    const doc: {
      nodes: { id: string }[]
      edges: unknown[]
      meta: Record<string, unknown>
    } = {
      nodes: [{ id: 'a' }],
      edges: [],
      meta: { lens_layout: { a: { x: 1 } }, other: 'keep' },
    }
    const next = withRevealedGroups(doc, new Set([revealKey('t', 'l')]))
    expect(next.meta?.[META_REVEALED_GROUPS]).toEqual([{ target: 't', lens: 'l' }])
    expect(next.meta?.lens_layout).toEqual({ a: { x: 1 } })
    expect(next.meta?.other).toBe('keep')
    // 原文档与原 meta 不被改写
    expect(doc.meta[META_REVEALED_GROUPS]).toBeUndefined()
  })

  it('空集也写回空数组（隐藏全部后服务端读到最小画布）', () => {
    const next = withRevealedGroups(
      { nodes: [], edges: [], meta: { revealed_groups: [{ target: 't', lens: 'l' }] } },
      new Set(),
    )
    expect(next.meta?.[META_REVEALED_GROUPS]).toEqual([])
  })
})

describe('清单派生：面板分区 / 靶心角标 / 全部揭示', () => {
  const groups: CaseCanvasLensGroup[] = [
    group('case#c1:subject:p1', 'lens_a', {
      target_label: '张卫国', revealed: true,
    }),
    group('case#c1:subject:p2', 'lens_b', { target_label: '李四' }),
    group('case#c1:subject:p2', 'lens_c', { target_label: '李四' }),
  ]

  it('未揭示组数按靶心聚合（已揭示不计）', () => {
    const m = unrevealedCountByTarget(groups)
    expect(m.get('case#c1:subject:p1')).toBeUndefined()
    expect(m.get('case#c1:subject:p2')).toBe(2)
  })

  it('全部揭示 = 清单所有组的键，不凭空造组', () => {
    const keys = allRevealedKeys(groups)
    expect(keys.size).toBe(3)
    expect(allRevealedKeys([]).size).toBe(0)
  })

  it('按靶心分区：保持首次出现顺序，同靶心连续，带显示名', () => {
    const secs = groupRowsByTarget(groups)
    expect(secs.map((s) => s.target)).toEqual([
      'case#c1:subject:p1',
      'case#c1:subject:p2',
    ])
    expect(secs[0].targetLabel).toBe('张卫国')
    expect(secs[0].rows).toHaveLength(1)
    expect(secs[1].targetLabel).toBe('李四')
    expect(secs[1].rows.map((r) => r.lens_id)).toEqual(['lens_b', 'lens_c'])
  })

  it('靶心无显示名时回退 node id（面板不留空标题）', () => {
    const secs = groupRowsByTarget([group('case#c1:subject:p9', 'lens_x')])
    expect(secs[0].targetLabel).toBe('case#c1:subject:p9')
  })
})
