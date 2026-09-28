/**
 * 三维交汇证据图 —— 构图层（纯函数，不依赖 G6）。
 *
 * 为什么单独抽出这一层
 * --------------------
 * 1. G6 经动态 import 装载，且注释已写明「装载失败/渲染异常/无 canvas 需降级」；
 *    构图若与渲染耦合，降级时图数据无从校验。抽成纯函数可脱离 canvas 单测。
 * 2. 星型/树状结构将来若改为手写 SVG 渲染，此层可直接复用，不必重写。
 *
 * 图为什么长这样
 * --------------
 * 锚点是（人 · 时 · 地）三元组，故中心节点为锚点，三个维度挂在它周围，
 * 每个维度下再挂各自的支撑观察；同现主体单独一类，挂在锚点旁。
 * 即：anchor → dim → support，anchor → co_present。
 *
 * 两条红线（与列表面板同口径，改此处须同步 ConvergenceCard.vue）
 * ------------------------------------------------------------
 * R-1 精度档必须在图上可见：date 档画虚线细边，minute/second 档画实线粗边。
 *     否则「12 条 date 档」与「4 条 minute 档」在图上长得一样，
 *     精度加权就会被一张漂亮的图重新掩盖。
 * R-2 重名不猜：person_ambiguous 的锚点画虚线边框并标「待裁决」，
 *     与普通锚点视觉可分，不混排。
 */
import {
  DIM_LABEL,
  DIM_ORDER,
  type ConvergenceDetailResult,
  type ConvergenceDim,
  type ConvergenceItem,
  type ConvergenceSupport,
} from '../api/endpoints/convergence'

export type GraphNodeKind = 'anchor' | 'dim' | 'support' | 'person'

export interface ConvergenceGraphNode {
  id: string
  kind: GraphNodeKind
  label: string
  /** 节点填充色——由 kind/dim 推导，口径在此收敛，避免样式散落到模板里无法单测 */
  color: string
  /** 副标题（时间/地址/依据），渲染到节点第二行 */
  sub?: string
  /** 所属维度（dim/support 节点有值） */
  dim?: string
  /** 精度档——决定边样式 */
  precision?: string
  /** 命中条数——必须与 precision 同看，不单独表达强度 */
  count?: number
  weight?: number
  /** 未命中维度的原因，明写，不由其他维度命中盖过去 */
  missReason?: string | null
  /** 重名待裁决 */
  ambiguous?: boolean
  degraded?: boolean
  /** 支撑观察的 obs_id，可跳回观察档案 */
  observationId?: string
}

export interface ConvergenceGraphEdge {
  source: string
  target: string
  /** 边样式由精度档推导：粗细=权重，线型=精度 */
  width: number
  dashed: boolean
  color: string
  label?: string
}

export interface ConvergenceGraph {
  nodes: ConvergenceGraphNode[]
  edges: ConvergenceGraphEdge[]
  /** 图例口径说明，渲染到图下方 */
  note: string
}

/** 维度主色：维度/支撑节点同色系，便于一眼看出证据分属哪一维 */
export const DIM_COLOR: Record<string, string> = {
  space: '#2f7ed8',
  time: '#8a5cd6',
  relation: '#d97a1c',
}

/** 节点填充色（非维度节点）。口径在此收敛，避免样式散落到模板里无法单测 */
const NODE_COLOR = {
  anchor: '#33475b',
  anchorAmbiguous: '#6b4a12',
  person: '#8a8a8a',
  miss: '#c2c2c2',
} as const

/** 精度档 → 边颜色。date 档用警示色，防止被读成"同时" */
function precisionColor(p: string): string {
  if (p === 'minute' || p === 'second') return '#18a058'
  if (p === 'hour') return '#2f7ed8'
  if (p === 'date') return '#f0a020'
  return '#999999'
}

/** 权重 → 线宽（1..4），仅为视觉区分，不单独表达强度 */
function weightToWidth(w: number | undefined): number {
  if (!w || w <= 0) return 1
  return Math.min(4, 1 + Math.round(w * 1.5))
}

function shortAddr(addr: string): string {
  const s = String(addr || '')
  const seg = s.split('/')
  return seg[seg.length - 1] || s || '—'
}

/**
 * 构造交汇证据图。
 * 未命中的维度同样出节点（灰色 + 写明原因）——缺维信息不能从图上消失。
 */
export function buildConvergenceGraph(
  item: ConvergenceItem,
  detail?: ConvergenceDetailResult | null,
): ConvergenceGraph {
  const nodes: ConvergenceGraphNode[] = []
  const edges: ConvergenceGraphEdge[] = []

  const anchorId = 'anchor'
  const who = (item.person_names || []).join('/') || item.person_key || '—'
  const where = (item.std_addresses || []).map(shortAddr).join('、') || '—'

  nodes.push({
    id: anchorId,
    kind: 'anchor',
    label: `${item.person_ambiguous ? '? ' : ''}${who}`,
    sub: `${item.date} · ${where}`,
    ambiguous: !!item.person_ambiguous,
    // R-2：重名锚点用警示色填充 + 虚线边框，与确定锚点视觉可分
    color: item.person_ambiguous ? NODE_COLOR.anchorAmbiguous : NODE_COLOR.anchor,
  })

  for (const dim of DIM_ORDER) {
    const d: ConvergenceDim | undefined = item.dimensions?.[dim]
    const dimId = `dim:${dim}`
    const hit = !!d?.hit
    const precision = d?.precision || 'unknown'

    nodes.push({
      id: dimId,
      kind: 'dim',
      dim,
      label: hit
        ? `${DIM_LABEL[dim] ?? dim} ${d!.count} 条`
        : `${DIM_LABEL[dim] ?? dim} 未命中`,
      sub: hit ? `${precision} 档 · 权 ${d!.weight}` : (d?.reason || '该维度无命中'),
      precision: hit ? precision : undefined,
      count: hit ? d!.count : 0,
      weight: hit ? d!.weight : 0,
      missReason: hit ? null : d?.reason || '该维度无命中（未被其他维度掩盖）',
      color: hit ? DIM_COLOR[dim] ?? '#999999' : NODE_COLOR.miss,
    })

    edges.push({
      source: anchorId,
      target: dimId,
      width: hit ? weightToWidth(d!.weight) : 1,
      // R-1：date 档虚线——明示"同地异时"，不可读作同时
      dashed: !hit || precision === 'date',
      color: hit ? precisionColor(precision) : '#c2c2c2',
      label: hit ? `${precision}` : undefined,
    })

    const supports: ConvergenceSupport[] = detail?.support?.[dim] || []
    for (const s of supports) {
      const sid = `sup:${s.observation_id}`
      nodes.push({
        id: sid,
        kind: 'support',
        dim,
        label: s.title || s.skill_id,
        sub: s.lens_name || s.skill_id,
        observationId: s.observation_id,
        degraded: !!s.degraded,
      })
      edges.push({
        source: dimId,
        target: sid,
        width: 1,
        dashed: true,
        color: DIM_COLOR[dim] ?? '#999',
      })
    }
  }

  for (const p of item.co_present || []) {
    const pid = `p:${p}`
    nodes.push({
      id: pid,
      kind: 'person',
      label: p,
      sub: '同现主体',
      color: NODE_COLOR.person,
    })
    edges.push({
      source: anchorId,
      target: pid,
      width: 1,
      // 同现不构成"接触"结论，统一虚线
      dashed: true,
      color: '#8a8a8a',
      label: '同现',
    })
  }

  return {
    nodes,
    edges,
    note:
      '实线粗边 = 时刻级（可判时间窗重叠）；虚线细边 = 日期级（仅能判同地异时，非同时）。' +
      '虚线边框中心节点 = 重名待裁决，不可当作单一自然人。',
  }
}

/** 图例条目：供组件渲染，避免样式口径散落在模板里 */
export const GRAPH_LEGEND = [
  { color: '#18a058', dashed: false, text: '时刻级（minute/second）：可判时间窗重叠' },
  { color: '#2f7ed8', dashed: false, text: '小时级（hour）' },
  { color: '#f0a020', dashed: true, text: '日期级（date）：仅同地异时，非同时' },
  { color: '#c2c2c2', dashed: true, text: '该维度未命中' },
  { color: '#8a8a8a', dashed: true, text: '同现主体（不构成接触结论）' },
] as const
