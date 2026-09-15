<script setup lang="ts">
// S3-F4 函数声明只读可视化（本体管理器壳内挂载 functions.json）。
// functions.json 无写路由（D7/E4-1）：只读展示参数/返回类型/依赖，不显示编辑控件；
// sql 实现文本后端不外曝（已 pop），前端也不展示。改为锁函数需走代码 + 测试，不在此页。
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NSpin, NTag } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import {
  functionsApi,
  type FunctionDecl,
  type FunctionParam,
} from '../api/endpoints/functions'
import { presentError, isApiError } from '../api/errors'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()

const loading = ref(false)
const errorText = ref('')
const pack = ref('')
const functions = ref<FunctionDecl[]>([])
const selectedName = ref('')
const keyword = ref('')

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    functions.value = []
    pack.value = ''
    return
  }
  loading.value = true
  errorText.value = ''
  try {
    const res = await functionsApi.list(cs.currentCaseId)
    // 目录按名称稳定排序，选中原选中项否则首项
    functions.value = [...res.functions].sort((a, b) => a.name.localeCompare(b.name))
    pack.value = res.pack
    if (!functions.value.some((f) => f.name === selectedName.value)) {
      selectedName.value = functions.value[0]?.name ?? ''
    }
  } catch (e) {
    functions.value = []
    errorText.value = isApiError(e) ? e.message : presentError(e).title
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return functions.value
  return functions.value.filter(
    (f) =>
      f.name.toLowerCase().includes(k) ||
      (f.title ?? '').toLowerCase().includes(k) ||
      (f.description ?? '').toLowerCase().includes(k),
  )
})

const selected = computed(
  () => functions.value.find((f) => f.name === selectedName.value) ?? null,
)

const IMPL_LABEL: Record<string, string> = { sql: 'SQL 实现', py: 'Python 实现' }
const PARAM_TYPE_LABEL: Record<string, string> = {
  integer: '整数',
  decimal: '小数',
  date: '日期',
  boolean: '布尔',
  string: '字符串（enum 白名单）',
}

function paramEntries(params: Record<string, FunctionParam> | undefined): [string, FunctionParam][] {
  return Object.entries(params ?? {})
}

function defaultValue(v: unknown): string {
  if (v === undefined || v === null) return '（无默认）'
  return String(v)
}
</script>

<template>
  <div class="fn-page">
    <!-- 🔒 只读声明（D7/E4-1：不假装可编辑） -->
    <div class="fn-ro-banner">
      🔒 函数声明只读目录 —— Function 是只读计算（SELECT/WITH 白名单 + 模板参数），
      修改函数须改 <span class="mono">functions.json</span> + <span class="mono">core/functions.py</span> 并随代码测试，本页不提供编辑。
    </div>

    <NSpin v-if="loading" class="fn-spin" size="medium" />
    <!-- 失败/空态收敛到 EmptyState：与全站同形，且保留 forbidden 语义位 -->
    <EmptyState
      v-else-if="errorText"
      type="error"
      title="函数目录加载失败"
      :desc="errorText"
    >
      <template #action>
        <NButton size="small" type="primary" @click="load">重试</NButton>
      </template>
    </EmptyState>
    <EmptyState
      v-else-if="!functions.length"
      type="empty"
      title="当前案件包没有函数声明"
      desc="Function 是只读计算（SELECT/WITH 白名单 + 模板参数），须在 functions.json 声明并注册实现"
    />

    <div v-else class="fn-body">
      <!-- 左：函数目录（搜索过滤） -->
      <aside class="fn-list">
        <div class="fn-list-head">
          <NInput
            v-model:value="keyword"
            size="small"
            clearable
            placeholder="搜索函数名 / 标题 / 描述"
          />
          <span class="dim fn-count">{{ filtered.length }}/{{ functions.length }}</span>
        </div>
        <button
          v-for="f in filtered"
          :key="f.name"
          type="button"
          class="fn-item"
          :class="{ active: f.name === selectedName }"
          @click="selectedName = f.name"
        >
          <span class="mono fn-item-name">{{ f.name }}</span>
          <span class="dim fn-item-title">{{ f.title }}</span>
          <NTag size="tiny" :bordered="false" class="fn-impl-tag">{{ f.impl }}</NTag>
        </button>
      </aside>

      <!-- 右：函数详情（只读） -->
      <section v-if="selected" class="fn-detail">
        <div class="fn-detail-head">
          <h3 class="mono fn-title">{{ selected.name }}</h3>
          <span class="fn-subtitle">{{ selected.title }}</span>
          <NTag size="small" :bordered="false">🔒 只读</NTag>
        </div>

        <p v-if="selected.description" class="fn-desc">{{ selected.description }}</p>

        <div class="fn-meta-row">
          <span class="fn-meta-label dim">实现</span>
          <NTag size="small" :bordered="false" type="info">{{ IMPL_LABEL[selected.impl] ?? selected.impl }}</NTag>
          <template v-if="selected.impl === 'py' && selected.impl_ref">
            <span class="dim mono fn-impl-ref">{{ selected.impl_ref }}</span>
          </template>
          <span class="fn-meta-label dim fn-meta-gap">返回</span>
          <NTag size="small" :bordered="false">{{ selected.output_type }}</NTag>
          <span class="dim fn-pack">案件包：{{ pack }}</span>
        </div>

        <!-- 消费的语义表（声明式依赖） -->
        <div class="fn-section">
          <div class="fn-section-title dim">消费的语义表（inputs）</div>
          <div v-if="selected.inputs?.length" class="fn-tags">
            <NTag v-for="t in selected.inputs" :key="t" size="small" :bordered="false" class="mono">
              {{ t }}
            </NTag>
          </div>
          <span v-else class="dim">（未声明）</span>
        </div>

        <!-- R3 py 真实依赖声明 -->
        <div v-if="selected.requires && Object.keys(selected.requires).length" class="fn-section">
          <div class="fn-section-title dim">真实依赖（requires，R3）</div>
          <div class="fn-requires">
            <div v-if="selected.requires.objects?.length" class="fn-req-row">
              <span class="dim fn-req-label">objects</span>
              <NTag v-for="o in selected.requires.objects" :key="o" size="small" :bordered="false" class="mono">{{ o }}</NTag>
            </div>
            <div v-if="selected.requires.links?.length" class="fn-req-row">
              <span class="dim fn-req-label">links</span>
              <NTag v-for="l in selected.requires.links" :key="l" size="small" :bordered="false" class="mono">{{ l }}</NTag>
            </div>
            <div v-if="selected.requires.props && Object.keys(selected.requires.props).length" class="fn-req-row">
              <span class="dim fn-req-label">props</span>
              <span class="mono fn-props">
                <span v-for="(props, obj) in selected.requires.props" :key="obj" class="fn-prop-item">
                  {{ obj }}: {{ (props as string[]).join('、') }}
                </span>
              </span>
            </div>
          </div>
        </div>

        <!-- 参数声明（string 必带 enum 白名单，防注入） -->
        <div class="fn-section">
          <div class="fn-section-title dim">参数声明（{{ paramEntries(selected.parameters).length }}）</div>
          <table v-if="paramEntries(selected.parameters).length" class="fn-params">
            <thead>
              <tr>
                <th>参数</th>
                <th>类型</th>
                <th>默认值</th>
                <th>约束 / 说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="[key, p] in paramEntries(selected.parameters)" :key="key">
                <td class="mono">{{ key }}</td>
                <td>
                  <NTag size="tiny" :bordered="false">{{ PARAM_TYPE_LABEL[p.type] ?? p.type }}</NTag>
                </td>
                <td class="mono">{{ defaultValue(p.default) }}</td>
                <td class="fn-param-constraint">
                  <span v-if="p.enum?.length" class="mono fn-enum">
                    enum：{{ p.enum.join(' | ') }}
                  </span>
                  <span v-if="p.description" class="dim">{{ p.description }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <span v-else class="dim">（无参数）</span>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.fn-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.fn-ro-banner {
  background: var(--sun-bg-card-hover);
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.fn-spin {
  display: block;
  padding: 32px 0;
}
.fn-error,
.fn-empty {
  color: var(--sun-text-tertiary);
  font-size: 13px;
  padding: 24px 0;
  text-align: center;
}
.fn-error {
  color: var(--sun-error-text);
}
.fn-body {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
.fn-list {
  flex: 0 0 260px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: calc(100vh - 260px);
  overflow: auto;
}
.fn-list-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-bottom: 4px;
}
.fn-count {
  font-size: 11px;
  white-space: nowrap;
}
.fn-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  text-align: left;
  border: none;
  background: transparent;
  border-radius: 6px;
  padding: 6px 8px;
  cursor: pointer;
  font: inherit;
  color: inherit;
}
.fn-item:hover {
  background: var(--sun-bg-card-hover);
}
.fn-item.active {
  background: var(--sun-input-bg);
  outline: 1px solid var(--sun-border-active);
}
.fn-item-name {
  font-size: 13px;
  font-weight: 500;
}
.fn-item-title {
  font-size: 11px;
}
.fn-impl-tag {
  font-size: 10px;
}
.fn-detail {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.fn-detail-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.fn-title {
  margin: 0;
  font-size: 15px;
}
.fn-subtitle {
  font-size: 13px;
  color: var(--sun-text-secondary);
}
.fn-desc {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
  line-height: 1.6;
}
.fn-meta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.fn-meta-label {
  font-size: 12px;
}
.fn-meta-gap {
  margin-left: 8px;
}
.fn-impl-ref {
  font-size: 12px;
}
.fn-pack {
  margin-left: auto;
  font-size: 11px;
}
.fn-section {
  border-top: 1px solid var(--sun-border);
  padding-top: 8px;
}
.fn-section-title {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 6px;
}
.fn-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.fn-requires {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.fn-req-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.fn-req-label {
  font-size: 11px;
  flex: 0 0 52px;
}
.fn-props {
  font-size: 12px;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.fn-prop-item {
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
}
.fn-params {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.fn-params th,
.fn-params td {
  border: 1px solid var(--sun-border);
  padding: 5px 8px;
  text-align: left;
  vertical-align: top;
}
.fn-params th {
  background: var(--sun-bg-card-hover);
  font-weight: 600;
}
.fn-param-constraint {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.fn-enum {
  font-size: 11px;
}
</style>
