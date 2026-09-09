<script setup lang="ts">
// FE-P-010 GraphCanvas：@antv/g6 v5 力导向图。
// 纪律：G6 经动态 import 装载（首屏不拖累）；装载失败/渲染异常/无 canvas
// 一律 emit('fallback')，由页面降级为关系表格（禁空白）。组件只画图与转发
// 节点点击，不做业务判定（下钻路由在页面/domain）。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { GraphDto, GraphNode } from '../../api/endpoints/graph'

const props = defineProps<{
  graph: GraphDto
}>()

const emit = defineEmits<{
  (e: 'fallback'): void
  (e: 'node-click', node: GraphNode): void
}>()

const containerEl = ref<HTMLDivElement | null>(null)

interface G6Instance {
  render?: () => Promise<unknown>
  setData?: (d: unknown) => void
  on?: (ev: string, cb: (e: unknown) => void) => void
  destroy?: () => void
}

let inst: G6Instance | null = null
let failed = false

/** 五间着色（首标签；无间标签中性色） */
const JIAN_COLORS: Record<string, string> = {
  因间: '#5ad8a6',
  内间: '#5d7092',
  反间: '#f6bd16',
  死间: '#e86452',
  生间: '#6dc8ec',
}

function nodeColor(n: GraphNode): string {
  return JIAN_COLORS[n.jian?.[0] ?? ''] ?? '#6e9fc1'
}

function toData(): unknown {
  return {
    nodes: props.graph.nodes.map((n) => ({
      id: n.id,
      data: { label: n.label, color: nodeColor(n) },
    })),
    edges: props.graph.edges.map((e) => ({
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      data: { label: e.label },
    })),
  }
}

function fail(): void {
  if (failed) return
  failed = true
  try {
    inst?.destroy?.()
  } catch {
    // ignore
  }
  inst = null
  emit('fallback')
}

async function mount(): Promise<void> {
  if (failed || !containerEl.value) return
  let GraphCtor: new (cfg: unknown) => G6Instance
  try {
    const mod = await import('@antv/g6')
    GraphCtor = mod.Graph as unknown as new (cfg: unknown) => G6Instance
  } catch {
    fail()
    return
  }
  try {
    inst = new GraphCtor({
      container: containerEl.value,
      autoResize: true,
      autoFit: 'view',
      data: toData(),
      layout: { type: 'force', linkDistance: 90 },
      node: {
        style: {
          size: 28,
          fill: (d: { data?: { color?: string } }) => d.data?.color ?? '#6e9fc1',
          labelText: (d: { data?: { label?: string } }) => d.data?.label ?? '',
          labelFill: '#d6e4f0',
          labelFontSize: 11,
          labelPlacement: 'bottom',
        },
      },
      edge: {
        style: {
          stroke: '#3a5a72',
          endArrow: true,
          labelText: (d: { data?: { label?: string } }) => d.data?.label ?? '',
          labelFill: '#8aa5b8',
          labelFontSize: 10,
        },
      },
      behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
    })
    inst.on?.('node:click', (ev: unknown) => {
      const id =
        (ev as { target?: { id?: string } })?.target?.id ??
        (ev as { id?: string })?.id ??
        ''
      const node = props.graph.nodes.find((n) => n.id === id)
      if (node) emit('node-click', node)
    })
    await inst.render?.()
  } catch {
    fail()
  }
}

onMounted(() => {
  void mount()
})

watch(
  () => [props.graph.nodes, props.graph.edges],
  () => {
    if (failed || !inst) return
    try {
      inst.setData?.(toData())
      void inst.render?.()
    } catch {
      fail()
    }
  },
)

onBeforeUnmount(() => {
  try {
    inst?.destroy?.()
  } catch {
    // ignore
  }
  inst = null
})
</script>

<template>
  <div ref="containerEl" class="graph-canvas" />
</template>

<style scoped>
.graph-canvas {
  width: 100%;
  height: 540px;
  min-height: 360px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background:
    radial-gradient(circle at 50% 40%, rgba(109, 200, 236, 0.05), transparent 70%),
    var(--sun-bg-card);
  overflow: hidden;
}
</style>
