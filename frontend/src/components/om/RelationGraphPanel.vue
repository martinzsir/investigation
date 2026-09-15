<script setup lang="ts">
// S2 对象关系图（PRD §6 F3 / 线框 E1）：G6 v5 动态导入（与 ResearchCanvas 同范式）。
// D6 确定性布局：固定分列（entity 左 / event 右）+ 名称排序 → 同一份数据每次打开位置一致；
// 不用 force / dagre。节点点击 → 选中对象；连线点击 → 编辑关系；新建关系走按钮（E2 弹窗）。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

interface GraphNode {
  id: string
  label: string
  kind: 'entity' | 'event'
  /** new = 左栏「新增」徽标对象（绿描边）；deleted 不传入 */
  isNew: boolean
  selected: boolean
}

interface GraphEdge {
  id: string
  label: string
  source: string
  target: string
  state: 'new' | 'modified' | 'deleted' | 'unchanged'
}

const props = defineProps<{ nodes: GraphNode[]; edges: GraphEdge[] }>()
const emit = defineEmits<{
  (e: 'select', name: string): void
  (e: 'edit-edge', key: string): void
  (e: 'create'): void
}>()

const containerEl = ref<HTMLElement | null>(null)
const graphFailed = ref(false)

// G6 v5 轻量类型（完整类型走 @antv/g6 自带 d.ts，动态导入后 any 化边界收敛在此）
interface G6Like {
  destroy: () => void
  on: (ev: string, fn: (e: unknown) => void) => void
  setData: (data: Record<string, unknown>) => void
  draw?: () => Promise<void>
  render?: () => Promise<void>
}

let inst: G6Like | null = null
// 深色主题画布用色（画布不吃 CSS var，取 design/tokens 暗色近似值）
const C = {
  nodeFill: '#232833',
  nodeStroke: '#5a6472',
  nodeLabel: '#e5e7eb',
  selected: '#d4a24e',
  newGreen: '#34d399',
  deletedRed: '#f87171',
  edge: '#6b7280',
  modified: '#d4a24e',
  edgeLabel: '#9ca3af',
}

function buildData(): { nodes: unknown[]; edges: unknown[] } {
  // 确定性布局：entity 左列 / event 右列，各自按 id 排序，固定间距
  const entities = props.nodes.filter((n) => n.kind === 'entity').map((n) => n.id).sort()
  const events = props.nodes.filter((n) => n.kind === 'event').map((n) => n.id).sort()
  const byId = new Map(props.nodes.map((n) => [n.id, n]))
  const COL = { entity: 90, event: 300 }
  const GAP = 86
  const TOP = 50
  const nodes = props.nodes.map((n) => {
    const col = n.kind === 'entity' ? COL.entity : COL.event
    const row = n.kind === 'entity' ? entities.indexOf(n.id) : events.indexOf(n.id)
    return {
      id: n.id,
      style: {
        x: col,
        y: TOP + row * GAP,
        size: [96, 36],
        fill: C.nodeFill,
        stroke: byId.get(n.id)?.selected ? C.selected : byId.get(n.id)?.isNew ? C.newGreen : C.nodeStroke,
        lineWidth: byId.get(n.id)?.selected ? 2.5 : 1.2,
        lineDash: byId.get(n.id)?.isNew ? [4, 3] : [],
        radius: 8,
        labelText: n.label,
        labelFill: C.nodeLabel,
        labelFontSize: 12,
        labelPlacement: 'center',
        labelWordWrap: true,
        labelMaxWidth: 88,
        cursor: 'pointer',
      },
    }
  })
  const edges = props.edges.map((e) => {
    const dash = e.state === 'unchanged' ? [] : [5, 4]
    const stroke =
      e.state === 'deleted' ? C.deletedRed
        : e.state === 'new' ? C.newGreen
          : e.state === 'modified' ? C.modified
            : C.edge
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      style: {
        stroke,
        lineWidth: e.state === 'unchanged' ? 1.2 : 1.6,
        lineDash: dash,
        endArrow: e.state === 'deleted' ? false : true,
        endArrowSize: 8,
        labelText: e.label,
        labelFill: e.state === 'deleted' ? C.deletedRed : C.edgeLabel,
        labelFontSize: 10,
        labelBackground: true,
        labelBackgroundFill: '#1a1e26',
        labelBackgroundRadius: 4,
        labelPadding: [1, 4],
        cursor: 'pointer',
      },
    }
  })
  return { nodes, edges }
}

async function mountGraph(): Promise<void> {
  if (graphFailed.value || !containerEl.value) return
  let GraphCtor: new (cfg: unknown) => G6Like
  try {
    const mod = await import('@antv/g6')
    GraphCtor = mod.Graph as unknown as new (cfg: unknown) => G6Like
  } catch {
    graphFailed.value = true
    return
  }
  try {
    inst = new GraphCtor({
      container: containerEl.value,
      autoResize: true,
      data: buildData(),
      // D6：数据自带 x/y（preset 语义），不挂任何自动布局
      node: { type: 'rect' },
      edge: { type: 'line' },
      behaviors: ['drag-canvas', 'zoom-canvas'],
    })
    inst.on('node:click', (e: unknown) => {
      const id = (e as { target?: { id?: string } })?.target?.id
      if (id) emit('select', id)
    })
    inst.on('edge:click', (e: unknown) => {
      const id = (e as { target?: { id?: string } })?.target?.id
      if (id) emit('edit-edge', id)
    })
    await inst.render?.()
  } catch {
    inst = null
    graphFailed.value = true
  }
}

onMounted(mountGraph)

watch(
  () => [props.nodes, props.edges] as const,
  async () => {
    if (!inst) {
      if (!graphFailed.value && containerEl.value) await mountGraph()
      return
    }
    try {
      inst.setData(buildData())
      await inst.render?.()
    } catch {
      // 渲染失败不阻断编辑（右栏只是视图）
    }
  },
  { deep: true },
)

onBeforeUnmount(() => {
  try {
    inst?.destroy()
  } catch {
    // ignore
  }
  inst = null
})
</script>

<template>
  <div class="rgp">
    <div class="rgp-head">
      <span>对象关系图</span>
      <span class="mini">确定性布局（同数据同位置）</span>
    </div>
    <div ref="containerEl" class="rgp-canvas">
      <div v-if="graphFailed" class="rgp-fallback">
        <div class="rgp-fb-title">关系图组件加载失败</div>
        <div v-for="e in edges" :key="e.id" class="rgp-fb-line" :class="`st-${e.state}`">
          {{ e.source }} ── {{ e.label }} ──▶ {{ e.target }}
        </div>
        <div v-if="!edges.length" class="mini">暂无关系</div>
      </div>
    </div>
    <div class="rgp-legend mini">
      <span class="lg"><i class="sw solid" />已有</span>
      <span class="lg"><i class="sw green" />新增</span>
      <span class="lg"><i class="sw red" />删除</span>
      <span class="lg"><i class="node new" />新增对象</span>
    </div>
    <div class="rgp-actions">
      <button class="rgp-btn" @click="emit('create')">+ 新建关系</button>
      <span class="mini">点击连线可编辑 / 删除</span>
    </div>
  </div>
</template>

<style scoped>
.rgp {
  display: flex;
  flex-direction: column;
  gap: 6px;
  height: 100%;
  min-height: 320px;
}
.rgp-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-secondary);
}
.rgp-canvas {
  flex: 1;
  min-height: 240px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: #161a22;
  overflow: hidden;
  position: relative;
}
.rgp-fallback {
  position: absolute;
  inset: 0;
  padding: 10px;
  font-size: 11px;
  overflow: auto;
}
.rgp-fb-title { font-weight: 600; margin-bottom: 6px; color: var(--sun-warn-text); }
.rgp-fb-line { color: var(--sun-text-secondary); }
.rgp-fb-line.st-new { color: var(--sun-ok-text); }
.rgp-fb-line.st-deleted { color: var(--sun-error-text); text-decoration: line-through; }
.rgp-legend { display: flex; gap: 10px; flex-wrap: wrap; }
.lg { display: inline-flex; align-items: center; gap: 3px; }
.sw {
  display: inline-block; width: 16px; height: 0;
  border-top: 2px solid var(--sun-text-tertiary); vertical-align: middle;
}
.sw.green { border-top-color: var(--sun-ok-text); border-top-style: dashed; }
.sw.red { border-top-color: var(--sun-error-text); border-top-style: dashed; }
.node.new {
  display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  border: 1.5px dashed var(--sun-ok-text);
}
.rgp-actions { display: flex; align-items: center; gap: 8px; }
.rgp-btn {
  border: 1px solid var(--sun-border); background: var(--sun-bg-card);
  color: var(--sun-text-primary); border-radius: 6px;
  padding: 3px 10px; font-size: 12px; cursor: pointer;
}
.rgp-btn:hover { border-color: var(--sun-border-active); }
.mini { font-size: 11px; color: var(--sun-text-tertiary); }
</style>
