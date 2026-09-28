<script setup lang="ts">
// P2 关系窗口（subject → relation，PRD 功能 3）。
// 数据：语义层一跳关联（graphApi 按 person_pk 过滤）+ 画布内研判边。
// 红线 R2：重名待裁决（person_ambiguous=true）不绑定唯一主体——顶部横幅
// 显式提示，不做任何图谱查询（查了就是把候选当结论）。
import { computed, ref, watch } from 'vue'
import { graphApi } from '../../../api/endpoints/graph'
import { KIND_LABELS, type CanvasDoc, type CanvasNode } from '../../../domain/canvas'

const props = defineProps<{
  node: CanvasNode
  caseId: string
  doc: CanvasDoc
}>()

interface OneHopItem {
  key: string
  peer: string
  peerType: string
  rel: string
  fromGraph: boolean
}

const loading = ref(false)
const failed = ref(false)
/** 请求代际：节点切换后旧响应丢弃（竞态=最新优先） */
let seq = 0
const graphNeighbors = ref<OneHopItem[]>([])

const ambiguous = computed(() => props.node.props?.person_ambiguous === true)
const personPk = computed(() => String(props.node.props?.person_pk ?? ''))

/** 画布内研判边一跳：同现（另一 subject）/ 位于（place）/ 发生于（event） */
const docNeighbors = computed<OneHopItem[]>(() => {
  const byId = new Map(props.doc.nodes.map((n) => [n.id, n]))
  const out: OneHopItem[] = []
  for (const e of props.doc.edges) {
    let peerId: string | null = null
    if (e.source === props.node.id) peerId = e.target
    else if (e.target === props.node.id && e.rel === '同现') peerId = e.source
    if (!peerId) continue
    const peer = byId.get(peerId)
    if (!peer) continue
    out.push({
      key: e.id,
      peer: peer.label,
      peerType: KIND_LABELS[peer.kind] ?? peer.kind,
      rel: e.rel,
      fromGraph: false,
    })
  }
  return out
})

/** graph 节点 id 域 "<object>:<pk>"：只取 person 对象且 pk 命中 */
async function loadGraph(): Promise<void> {
  graphNeighbors.value = []
  failed.value = false
  if (ambiguous.value || !personPk.value) return
  const my = ++seq
  loading.value = true
  try {
    const g = await graphApi.get(props.caseId, { nodeLimit: 300, edgeLimit: 1000 })
    if (my !== seq) return // 旧响应丢弃
    if (!g.available) return
    const nodeId = `person:${personPk.value}`
    const labelById = new Map(g.nodes.map((n) => [n.id, n]))
    const items: OneHopItem[] = []
    for (const e of g.edges) {
      let peerId: string | null = null
      if (e.source === nodeId) peerId = e.target
      else if (e.target === nodeId) peerId = e.source
      if (!peerId) continue
      const peer = labelById.get(peerId)
      items.push({
        key: `${e.source}--${e.type}--${e.target}--${items.length}`,
        peer: peer?.label ?? peerId,
        peerType: peer?.type_title ?? peer?.type ?? peerId.split(':')[0],
        rel: e.label || e.type,
        fromGraph: true,
      })
      if (items.length >= 30) break
    }
    graphNeighbors.value = items
  } catch {
    if (my === seq) failed.value = true
  } finally {
    if (my === seq) loading.value = false
  }
}

watch(() => [props.node.id, personPk.value], () => void loadGraph(), {
  immediate: true,
})

const hasAny = computed(
  () => docNeighbors.value.length > 0 || graphNeighbors.value.length > 0,
)
</script>

<template>
  <div class="relw" data-testid="relation-window">
    <div v-if="ambiguous" class="relw-amb" data-testid="relation-ambiguous">
      该主体存在同名候选，待人工裁决，本窗口结果未绑定唯一主体
    </div>

    <template v-else>
      <p v-if="!personPk" class="relw-hint">该节点未绑定主体主键，仅展示画布内连线</p>

      <div v-if="loading" class="relw-loading" data-testid="relation-loading">
        正在取一跳关联…
      </div>
      <div v-else-if="failed" class="relw-failed" data-testid="relation-failed">
        关联数据加载失败（图谱服务不可用或未构建）
      </div>

      <template v-else>
        <div v-if="!hasAny" class="relw-empty" data-testid="relation-empty">
          暂无一跳关联
        </div>
        <template v-else>
          <h4 class="relw-sec">画布连线（{{ docNeighbors.length }}）</h4>
          <ul class="relw-list">
            <li v-for="it in docNeighbors" :key="it.key" class="relw-item">
              <span class="relw-peer">{{ it.peer }}</span>
              <span class="relw-meta">{{ it.peerType }} · {{ it.rel }}</span>
            </li>
          </ul>
          <h4 v-if="graphNeighbors.length" class="relw-sec">
            语义层一跳（{{ graphNeighbors.length }}）
          </h4>
          <ul v-if="graphNeighbors.length" class="relw-list">
            <li v-for="it in graphNeighbors" :key="it.key" class="relw-item">
              <span class="relw-peer">{{ it.peer }}</span>
              <span class="relw-meta">{{ it.peerType }} · {{ it.rel }}</span>
            </li>
          </ul>
        </template>
      </template>
    </template>
  </div>
</template>

<style scoped>
.relw { display: flex; flex-direction: column; gap: 8px; }
.relw-amb {
  padding: 6px 8px;
  font-size: 12px;
  color: #8a5a00;
  background: #fdf3e0;
  border: 1px solid #f0d9ad;
  border-radius: 6px;
}
.relw-hint, .relw-empty, .relw-loading, .relw-failed {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-tertiary, #7c8aa5);
}
.relw-failed { color: var(--sun-danger, #d03050); }
.relw-sec {
  margin: 2px 0 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--sun-text-secondary, #4a5a76);
}
.relw-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.relw-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 6px;
}
.relw-peer { font-weight: 600; color: var(--sun-text-primary, #0a1b36); }
.relw-meta { font-size: 11px; color: var(--sun-text-tertiary, #7c8aa5); }
</style>
