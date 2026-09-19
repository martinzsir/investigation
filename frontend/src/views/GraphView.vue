<script setup lang="ts">
// 知识图谱（FE-P-010，/c/graph）：G6 v5 力导图，动态 import 失败/不可用降级
// 关系表格（禁空白）；节点按度数 top-N 采样，截断显式横幅；节点点击下钻线索列表。
// P7：同一取数参数化——关系图谱/资金链路图模式切换、边类型过滤、
// 线索 evidence_refs 命中高亮、命中边时间轴；不新增取数 API。
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NButton, NButtonGroup, NInputNumber, NTag, NCheckbox, useMessage } from 'naive-ui'
import { graphApi, type GraphDto, type GraphNode } from '../api/endpoints/graph'
import { useCaseStore } from '../stores/case'
import { presentError, isApiError } from '../api/errors'
import {
  EDGE_LIMIT, NODE_LIMIT, GRAPH_MODES, HIT_COLOR, clampEdgeLimit, clampNodeLimit,
  edgesToRows, hasGraph, hitTimeline, nodeDrillHref, nodesByDegree,
  presentEdgeKinds, truncatedBanner, type GraphMode,
} from '../domain/graphModel'
import GraphCanvas from '../components/research/GraphCanvas.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const router = useRouter()
const route = useRoute()
const message = useMessage()

const loading = ref(false)
const data = ref<GraphDto | null>(null)
const fallback = ref(false)
const nodeLimit = ref(NODE_LIMIT.def)
const edgeLimit = ref(EDGE_LIMIT.def)
const mode = ref<GraphMode>('relation')
const selectedKinds = ref<string[]>([])
// 路由带 clue_id 时仅高亮该线索（如从线索详情跳入）
const clueId = computed(() => (typeof route.query.clue_id === 'string' ? route.query.clue_id : ''))

const graphReady = computed(() => hasGraph(data.value))
const banner = computed(() => (data.value ? truncatedBanner(data.value) : ''))
const kindOptions = computed(() => (data.value ? presentEdgeKinds(data.value) : []))
const timeline = computed(() => (data.value ? hitTimeline(data.value) : []))
const relationRows = computed(() =>
  data.value ? edgesToRows(data.value.edges, data.value.nodes) : [],
)
const topNodes = computed(() => (data.value ? nodesByDegree(data.value).slice(0, 15) : []))

// 下发给同一 API 的边白名单：资金模式固定 transfers；关系模式按勾选子集，空=全部
const edgeKindsParam = computed<string[] | undefined>(() => {
  if (mode.value === 'fund') return ['transfers']
  return selectedKinds.value.length ? selectedKinds.value : undefined
})

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    data.value = null
    return
  }
  loading.value = true
  fallback.value = false
  try {
    data.value = await graphApi.get(cs.currentCaseId, {
      nodeLimit: clampNodeLimit(nodeLimit.value),
      edgeLimit: clampEdgeLimit(edgeLimit.value),
      edgeKinds: edgeKindsParam.value,
      clueId: clueId.value || undefined,
    })
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, () => void load(), { immediate: true })
// 投影参数变化即重新取数（仍是同一端点）
watch([mode, selectedKinds, edgeKindsParam], () => void load())

function switchMode(m: GraphMode): void {
  mode.value = m
}

// naive-ui NCheckbox 独立使用时只 emit update:checked（value 仅 Group 内生效）
function toggleKind(name: string, on: boolean): void {
  if (on) {
    if (!selectedKinds.value.includes(name)) selectedKinds.value = [...selectedKinds.value, name]
  } else {
    selectedKinds.value = selectedKinds.value.filter((x) => x !== name)
  }
}

function clearKinds(): void {
  selectedKinds.value = []
}

function onFallback(): void {
  fallback.value = true
}

function drill(node: GraphNode): void {
  void router.push(nodeDrillHref(node))
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>知识图谱</h2>
      <p class="dim hint">
        同一语义图的两种投影：关系图谱（全边或按类型过滤）与资金链路图（转账边）。
        橙色为线索命中节点/关系，可点击溯源；图形组件不可用时自动降级为关系表格。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="图谱按案件构建" />

    <template v-else>
      <div class="controls">
        <NButtonGroup size="small">
          <NButton
            v-for="m in GRAPH_MODES" :key="m.key"
            :type="mode === m.key ? 'primary' : 'default'"
            :title="m.hint"
            @click="switchMode(m.key)"
          >{{ m.label }}</NButton>
        </NButtonGroup>

        <label class="ctl">
          节点上限
          <NInputNumber v-model:value="nodeLimit" :min="NODE_LIMIT.min" :max="NODE_LIMIT.max" size="small" />
        </label>
        <label class="ctl">
          关系上限
          <NInputNumber v-model:value="edgeLimit" :min="EDGE_LIMIT.min" :max="EDGE_LIMIT.max" size="small" />
        </label>
        <NButton size="small" type="primary" :loading="loading" @click="load">刷新图谱</NButton>
        <NButton v-if="graphReady && !fallback" size="small" quaternary @click="fallback = true">
          切换关系表格
        </NButton>
        <NButton v-else-if="graphReady && fallback" size="small" quaternary @click="fallback = false">
          切换图形视图
        </NButton>
      </div>

      <div v-if="mode === 'relation' && kindOptions.length" class="kind-filter">
        <span class="dim filter-label">边类型过滤</span>
        <NCheckbox
          v-for="k in kindOptions" :key="k.name"
          :checked="selectedKinds.includes(k.name)"
          @update:checked="(v: boolean) => toggleKind(k.name, v)"
        >{{ k.title }}</NCheckbox>
        <NButton v-if="selectedKinds.length" size="tiny" quaternary @click="clearKinds">
          全部边
        </NButton>
      </div>

      <NSpin :show="loading">
        <EmptyState
          v-if="!loading && data && !graphReady"
          type="empty"
          title="暂无可绘制的图谱"
          desc="案件尚未 BUILD 语义层或没有实体/关系数据；请先完成数据接入与本体构建"
        />

        <template v-else-if="graphReady && data">
          <div v-if="banner" class="banner">⚠ {{ banner }}（超限部分不参与绘制）</div>

          <div v-if="data.highlights.clue_count" class="legend">
            <span class="legend-dot" :style="{ background: HIT_COLOR }" />
            线索命中高亮：{{ data.highlights.clue_count }} 条线索
            （节点 {{ data.highlights.nodes }} / 关系 {{ data.highlights.edges }}）
            <template v-if="clueId"><span class="dim">· 仅线索 {{ clueId }}</span></template>
          </div>

          <div class="graph-layout">
            <div class="graph-main">
              <GraphCanvas v-if="!fallback" :graph="data" @fallback="onFallback" @node-click="drill" />

              <div v-else class="grid-wrap">
                <table class="grid">
                  <thead>
                    <tr><th></th><th>起点</th><th>关系</th><th>终点</th><th>类型</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(r, i) in relationRows" :key="i">
                      <td>
                        <span v-if="r.hit" class="hit-mark" :title="r.clue_ids.join(',')">●</span>
                      </td>
                      <td>{{ r.sourceLabel }}</td>
                      <td>{{ r.relation }}</td>
                      <td>{{ r.targetLabel }}</td>
                      <td class="mono dim">{{ r.type }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div class="graph-side">
              <div v-if="timeline.length" class="card">
                <div class="card-title">命中时间轴（{{ timeline.length }}）</div>
                <ul class="timeline-list">
                  <li v-for="(t, i) in timeline" :key="i">
                    <span class="tl-date mono">{{ t.date.slice(0, 10) }}</span>
                    <span class="tl-chain">
                      {{ t.sourceLabel }} → {{ t.targetLabel }}
                      <span v-if="t.amount" class="dim mono">（{{ t.amount }}）</span>
                    </span>
                  </li>
                </ul>
              </div>

              <div class="card">
                <div class="card-title">核心节点（按关联度 top15）</div>
                <ul class="degree-list">
                  <li v-for="({ node, degree }) in topNodes" :key="node.id">
                    <button class="degree-btn" @click="drill(node)">
                      <span v-if="node.hit" class="hit-mark">●</span>
                      <span class="degree-label">{{ node.label }}</span>
                      <NTag size="tiny" :bordered="false">{{ node.type_title || node.type }}</NTag>
                      <span v-if="node.jian?.length" class="jian-tags">
                        <NTag v-for="j in node.jian" :key="j" size="tiny" :bordered="false" type="info">{{ j }}</NTag>
                      </span>
                      <span class="degree-n mono">{{ degree }}</span>
                    </button>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </template>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-head h2 {
  margin: 0;
  font-size: 18px;
}
.hint {
  font-size: 12px;
  margin: 4px 0 0;
}
.controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.ctl {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.kind-filter {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
}
.filter-label {
  font-size: 12px;
}
.banner {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
}
.legend {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.graph-layout {
  display: grid;
  grid-template-columns: 1fr 280px;
  gap: 12px;
  align-items: start;
}
.graph-side {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 10px 12px;
}
.card-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}
.timeline-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tl-date {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  margin-right: 6px;
}
.tl-chain {
  font-size: 12px;
}
.degree-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.degree-btn {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
  color: var(--sun-text-primary);
  font-size: 12px;
}
.degree-btn:hover {
  background: rgba(110, 222, 233, 0.08);
}
.degree-label {
  font-weight: 600;
}
.hit-mark {
  color: var(--sun-warning, #ffb454);
  font-size: 11px;
}
.jian-tags {
  display: inline-flex;
  gap: 4px;
}
.degree-n {
  margin-left: auto;
  color: var(--sun-text-tertiary);
  font-size: 11px;
}
.grid-wrap {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  overflow: auto;
  max-height: 540px;
}
.grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.grid th,
.grid td {
  text-align: left;
  padding: 7px 12px;
  border-bottom: 1px solid var(--sun-border);
}
.grid th {
  color: var(--sun-text-tertiary);
  font-weight: 500;
  font-size: 12px;
  white-space: nowrap;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
