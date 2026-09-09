<script setup lang="ts">
// 知识图谱（FE-P-010，/c/graph）：G6 v5 力导图，动态 import 失败/不可用降级
// 关系表格（禁空白）；节点按度数 top-N 采样，截断显式横幅；节点点击下钻线索列表。
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NSpin, NButton, NInputNumber, NTag, useMessage } from 'naive-ui'
import { graphApi, type GraphDto, type GraphNode } from '../api/endpoints/graph'
import { useCaseStore } from '../stores/case'
import { presentError, isApiError } from '../api/errors'
import {
  EDGE_LIMIT, NODE_LIMIT, clampEdgeLimit, clampNodeLimit, edgesToRows,
  hasGraph, nodeDrillHref, nodesByDegree, truncatedBanner,
} from '../domain/graphModel'
import GraphCanvas from '../components/research/GraphCanvas.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const router = useRouter()
const message = useMessage()

const loading = ref(false)
const data = ref<GraphDto | null>(null)
const fallback = ref(false)
const nodeLimit = ref(NODE_LIMIT.def)
const edgeLimit = ref(EDGE_LIMIT.def)

const graphReady = computed(() => hasGraph(data.value))
const banner = computed(() => (data.value ? truncatedBanner(data.value) : ''))
const relationRows = computed(() =>
  data.value ? edgesToRows(data.value.edges, data.value.nodes) : [],
)
const topNodes = computed(() => (data.value ? nodesByDegree(data.value).slice(0, 15) : []))

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
    })
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, () => void load(), { immediate: true })

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
        实体关系力导图（按关联度数采样）。点击节点下钻线索列表；图形组件不可用时自动降级为关系表格。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="图谱按案件构建" />

    <template v-else>
      <div class="controls">
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

      <NSpin :show="loading">
        <EmptyState
          v-if="!loading && data && !graphReady"
          type="empty"
          title="暂无可绘制的图谱"
          desc="案件尚未 BUILD 语义层或没有实体/关系数据；请先完成数据接入与本体构建"
        />

        <template v-else-if="graphReady && data">
          <div v-if="banner" class="banner">⚠ {{ banner }}（超限部分不参与绘制）</div>

          <div class="graph-layout">
            <div class="graph-main">
              <GraphCanvas v-if="!fallback" :graph="data" @fallback="onFallback" @node-click="drill" />

              <div v-else class="grid-wrap">
                <table class="grid">
                  <thead>
                    <tr><th>起点</th><th>关系</th><th>终点</th><th>类型</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(r, i) in relationRows" :key="i">
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
              <div class="card">
                <div class="card-title">核心节点（按关联度 top15）</div>
                <ul class="degree-list">
                  <li v-for="({ node, degree }) in topNodes" :key="node.id">
                    <button class="degree-btn" @click="drill(node)">
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
.banner {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
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
