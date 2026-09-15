<script setup lang="ts">
// S1 本体管理器（独立 App，非侧栏第七组——R1）。
// 左：19 个本体文件语义六组树（F1 总览 API）；右：现有 8 个编辑页零改动挂载（R2/D2）。
// 只读文件标 🔒 + 「暂无编辑界面」（R3/D3）；标准域来源警示（R4/F3.3）；
// 单文件缺失/解析失败局部降级（E1-3/E1-4/D6）；深链接回落首项（E2-2）；
// 挂载失败给「在 JSON 页打开」逃生入口（E2-1）。
import { computed, onErrorCaptured, ref, shallowRef, watch } from 'vue'
import type { Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NSpin, NTag, NTooltip, useMessage } from 'naive-ui'
import {
  ontologyOverviewApi,
  type OntologyFileEntry,
  type OntologyOverviewDto,
  type SourceLayer,
} from '../api/endpoints/ontologyOverview'
import { presentError, isApiError } from '../api/errors'
import { useCaseStore } from '../stores/case'
import EmptyState from '../components/common/EmptyState.vue'

const route = useRoute()
const router = useRouter()
const cs = useCaseStore()
const message = useMessage()

const loading = ref(false)
const errorText = ref('')
const overview = ref<OntologyOverviewDto | null>(null)

const caseId = computed(() => String(route.params.caseId ?? ''))

// ---- 案件上下文同步（现有编辑页读 case store，壳只同步不改其内部——R2）----
watch(
  caseId,
  (id) => {
    // 无 caseId 但案件选择器已有案件 → 自动携带 case_id（F2.1 从案件上下文进入）
    if (!id && cs.currentCaseId) {
      void router.replace(`/ontology-manager/${cs.currentCaseId}`)
      return
    }
    if (id && cs.currentCaseId !== id) cs.selectCase(id)
    void load()
  },
  { immediate: true },
)

async function load(): Promise<void> {
  if (!caseId.value) {
    overview.value = null
    return
  }
  loading.value = true
  errorText.value = ''
  try {
    overview.value = await ontologyOverviewApi.overview(caseId.value)
  } catch (e) {
    overview.value = null
    errorText.value = isApiError(e) ? e.message : presentError(e).title
  } finally {
    loading.value = false
  }
}

// ---- 左栏语义六组（D4：后端已按组序返回，这里只聚合）----
const groups = computed<{ name: string; files: OntologyFileEntry[] }[]>(() => {
  const out: { name: string; files: OntologyFileEntry[] }[] = []
  for (const f of overview.value?.files ?? []) {
    let g = out.find((x) => x.name === f.group)
    if (!g) {
      g = { name: f.group, files: [] }
      out.push(g)
    }
    g.files.push(f)
  }
  return out
})

const entries = computed(() => overview.value?.files ?? [])

const selectedName = computed(() => String(route.params.file ?? ''))
const selected = computed(
  () => entries.value.find((f) => f.name === selectedName.value) ?? null,
)

// E2-2：深链接文件不存在 → 回落首项 + 轻提示（不白屏）
watch(
  [entries, selectedName],
  ([list, name]) => {
    if (!list.length) return
    if (!name) {
      void router.replace(`/ontology-manager/${caseId.value}/${list[0].name}`)
      return
    }
    if (!list.some((f) => f.name === name)) {
      message.info(`未找到本体文件 ${name}.json，已定位到 ${list[0].name}.json`)
      void router.replace(`/ontology-manager/${caseId.value}/${list[0].name}`)
    }
  },
  { immediate: true },
)

function open(name: string): void {
  void router.push(`/ontology-manager/${caseId.value}/${name}`)
}

// ---- 顶部条（F3）----
const versionShort = computed(() => {
  const v = overview.value?.ontology_version ?? ''
  return v ? `${v.slice(0, 8)}…` : '—'
})

// ---- F3 来源层标注 / R4 标准域警示 ----
const LAYER_LABEL: Record<SourceLayer, string> = {
  shared: '全域',
  industry: '行业',
  case: '案件',
}

const sharedWarning = computed(() => {
  const f = selected.value
  if (!f || !f.source_layer.includes('shared')) return ''
  return `⚠ 该文件在全域层（_shared）存在同名声明，全域层改动将影响所有案件。当前编辑器修改的是案件层（仅影响本案件）。`
})

const industryWarning = computed(() => {
  const f = selected.value
  const ind = overview.value?.industry
  if (!f || !ind || !f.source_layer.includes('industry')) return ''
  return `⚠ 该文件在行业层（${ind}）存在同名声明，行业层改动将影响所有采用该行业包的案件。当前编辑器修改的是案件层（仅影响本案件）。`
})

// ---- 右栏编辑器挂载（D2：现有页面零改动，动态 import 异步挂载）----
/** .vue 动态 import 返回模块命名空间，组件在 default 上（vite/client shim） */
type EditorLoader = () => Promise<{ default: Component }>

const EDITOR_LOADERS: Record<string, EditorLoader> = {
  objects: () => import('../views/ModelDesignerView.vue'),
  links: () => import('../views/ModelDesignerView.vue'),
  data_elements: () => import('../views/DataElementsView.vue'),
  bindings: () => import('../views/EtlPipelineView.vue'),
  rules: () => import('../views/RuleWorkshopView.vue'),
  functions: () => import('../views/FunctionsView.vue'),
  actions: () => import('../views/ActionsView.vue'),
  states: () => import('../views/StatesView.vue'),
  policies: () => import('../views/PolicyMaskingView.vue'),
  views: () => import('../views/PolicyMaskingView.vue'),
  case_knowledge: () => import('../views/KnowledgeView.vue'),
  // S5 通用 schema 驱动表单（不替换专用编辑器 R5；llm_policy 组件内只读无表单 R6）
  derived_properties: () => import('../views/GenericConfigView.vue'),
  dimensions: () => import('../views/GenericConfigView.vue'),
  enum_space: () => import('../views/GenericConfigView.vue'),
  jians: () => import('../views/GenericConfigView.vue'),
  scoring: () => import('../views/GenericConfigView.vue'),
  thresholds: () => import('../views/GenericConfigView.vue'),
  verify_playbooks: () => import('../views/GenericConfigView.vue'),
  llm_policy: () => import('../views/GenericConfigView.vue'),
}

// 只读文件但有只读可视化页（S3-F4 functions.json：D7/E4-1 只读展示，不假装可编辑；
// S5 llm_policy：R6 只读 JSON，永不渲染表单）
const READONLY_EDITORS = new Set(['functions', 'llm_policy'])

const editorComp = shallowRef<Component | null>(null)
const editorLoading = ref(false)
const mountFailed = ref(false)
const editorKey = ref(0)

async function loadEditor(name: string): Promise<void> {
  const loader = EDITOR_LOADERS[name]
  if (!loader) {
    editorComp.value = null
    return
  }
  editorLoading.value = true
  mountFailed.value = false
  try {
    editorComp.value = (await loader()).default
  } catch {
    // E2-1：组件加载失败 → 挂载失败态（逃生入口在模板）
    mountFailed.value = true
    editorComp.value = null
  } finally {
    editorLoading.value = false
  }
}

watch(selectedName, (name) => {
  editorKey.value += 1
  void loadEditor(name)
}, { immediate: true })

// E2-1：现有页面渲染异常 → 挂载失败态（拦截向上传播，避免整壳崩）
onErrorCaptured(() => {
  mountFailed.value = true
  editorComp.value = null
  return false
})

function retryMount(): void {
  editorKey.value += 1
  void loadEditor(selectedName.value)
}

const showEditor = computed(
  () =>
    !!selected.value &&
    EDITOR_LOADERS[selected.value.name] !== undefined &&
    (selected.value.writable || READONLY_EDITORS.has(selected.value.name)),
)
</script>

<template>
  <div class="om-page">
    <!-- 顶部条（S1 PRD §5.4）：案件 · 本体版本（前 8 位…，悬停看全）· 版本沿革入口（置灰，D7） -->
    <div class="om-topbar">
      <h2 class="om-title">本体管理器</h2>
      <span v-if="overview" class="om-case dim">案件：{{ overview.case_name }}</span>
      <NTooltip v-if="overview?.ontology_version" trigger="hover">
        <template #trigger>
          <NTag size="small" :bordered="true"><span class="mono">本体版本 {{ versionShort }}</span></NTag>
        </template>
        <span class="mono">{{ overview.ontology_version }}</span>
      </NTooltip>
      <NTooltip trigger="hover">
        <template #trigger>
          <!-- 禁用按钮不冒泡 hover，外包 span 保证 tooltip 可达 -->
          <span><NButton size="tiny" quaternary disabled>版本沿革</NButton></span>
        </template>
        版本沿革视图将在后续版本开放
      </NTooltip>
    </div>

    <!-- 加载中 / 错误 / 无案件 -->
    <NSpin v-if="loading" class="om-spin" size="medium" />
    <EmptyState
      v-else-if="!caseId"
      type="empty"
      title="请先选择案件"
      desc="本体管理器按案件快照归属，从顶部案件选择器选择案件，或前往案件门户"
    >
      <template #action>
        <NButton size="small" type="primary" @click="router.push('/cases')">前往案件门户</NButton>
      </template>
    </EmptyState>
    <EmptyState
      v-else-if="errorText"
      type="error"
      title="总览加载失败"
      :desc="errorText"
    >
      <template #action>
        <NButton size="small" type="primary" @click="load">重试</NButton>
      </template>
    </EmptyState>

    <!-- E1-2 空态：案件无本体文件（§8.6） -->
    <EmptyState
      v-else-if="!entries.length"
      type="empty"
      title="当前案件没有本体文件"
      desc="请通过案件包导入，或从模板创建。"
    >
      <template #action>
        <NButton size="small" type="primary" @click="router.push('/c/package')">前往案件包</NButton>
      </template>
    </EmptyState>

    <!-- 就绪：左树 + 右编辑器 -->
    <div v-else class="om-body">
      <aside class="om-tree">
        <div v-for="g in groups" :key="g.name" class="om-group">
          <div class="om-group-title dim">{{ g.name }}</div>
          <button
            v-for="f in g.files"
            :key="f.name"
            type="button"
            class="om-item"
            :class="{ active: f.name === selectedName }"
            @click="open(f.name)"
          >
            <span class="om-item-head">
              <span class="mono om-item-name">{{ f.name }}.json</span>
              <NTag v-if="f.writable" size="tiny" type="success" :bordered="false">✅ 可写</NTag>
              <NTag v-else size="tiny" :bordered="false">🔒 只读</NTag>
              <NTag v-if="!f.has_schema" size="tiny" type="warning" :bordered="false">⚠ 无 schema</NTag>
            </span>
            <span class="om-item-meta dim">
              <span
                v-for="layer in f.source_layer"
                :key="layer"
                class="om-layer"
              >{{ LAYER_LABEL[layer] }}</span>
              <span v-if="f.status === 'ok'">{{ f.item_count ?? 0 }} 项 · {{ f.updated_at?.slice(0, 16).replace('T', ' ') ?? '—' }}</span>
              <span v-else-if="f.status === 'missing'" class="om-bad">缺失</span>
              <span v-else class="om-bad">解析失败</span>
            </span>
          </button>
        </div>
      </aside>

      <section class="om-editor">
        <!-- E1-3/E1-4：单文件局部降级，不阻断整体（D6） -->
        <EmptyState
          v-if="selected?.status === 'missing'"
          type="error"
          title="文件缺失"
          :desc="`${selected.name}.json 不在案件快照中，可能被手工删除。其余文件不受影响；可通过案件包重新导入恢复。`"
        >
          <template #action>
            <NButton size="small" @click="router.push('/c/package')">前往案件包</NButton>
          </template>
        </EmptyState>
        <EmptyState
          v-else-if="selected?.status === 'parse_error'"
          type="error"
          title="文件解析失败"
          :desc="`${selected.name}.json 不是合法 JSON，可能已被手工改坏。其余文件不受影响；可导出案件包后修复。`"
        >
          <template #action>
            <NButton size="small" @click="router.push('/c/escape')">在 JSON 页打开</NButton>
          </template>
        </EmptyState>

        <template v-else-if="selected">
          <!-- 文件头：名称 + 状态 + 编辑范围（F3.2）+ 来源层（F3.1） -->
          <div class="om-editor-head">
            <span class="mono om-editor-name">{{ selected.name }}.json</span>
            <NTag v-if="selected.writable" size="tiny" type="success" :bordered="false">✅ 可写</NTag>
            <NTag v-else size="tiny" :bordered="false">🔒 只读</NTag>
            <NTag v-if="!selected.has_schema" size="tiny" type="warning" :bordered="false">⚠ 无 schema</NTag>
            <NTag
              v-for="layer in selected.source_layer"
              :key="layer"
              size="tiny"
              :bordered="false"
            >来源：{{ LAYER_LABEL[layer] }}</NTag>
            <NTag v-if="selected.writable" size="tiny" type="info" :bordered="false">编辑范围：案件层</NTag>
            <!-- 渐进替换（S2）：objects/links 已有可视化建模器，JSON 页保持并存 -->
            <NButton
              v-if="selected.name === 'objects' || selected.name === 'links'"
              size="tiny"
              type="primary"
              secondary
              @click="router.push('/c/omodel')"
            >可视化建模器 ↗</NButton>
          </div>

          <!-- R4/F3.3 标准域警示 -->
          <div v-if="sharedWarning" class="warn-banner">{{ sharedWarning }}</div>
          <div v-if="industryWarning" class="warn-banner">{{ industryWarning }}</div>
          <!-- §8.4 无 schema 提示 -->
          <div v-if="!selected.has_schema" class="hint-banner">
            {{ selected.name }}.json 暂无结构声明（schema），后续开放编辑时字段校验可能不完整。
          </div>

          <!-- E2-1 挂载失败：重试 + 逃生入口 -->
          <EmptyState
            v-if="mountFailed"
            type="error"
            title="编辑器挂载失败"
            :desc="`${selected.name}.json 的编辑页面加载异常，可重试，或到代码逃生舱直接编辑 JSON。`"
          >
            <template #action>
              <span class="btn-row">
                <NButton size="small" type="primary" @click="retryMount">重试</NButton>
                <NButton size="small" @click="router.push('/c/escape')">在 JSON 页打开</NButton>
              </span>
            </template>
          </EmptyState>

          <!-- 挂载现有编辑页（零改动；key 驱动重挂载） -->
          <NSpin v-else-if="editorLoading" class="om-spin" size="medium" />
          <div v-else-if="editorComp" class="om-editor-body" :key="editorKey">
            <component :is="editorComp" />
          </div>

          <!-- R3/D3：只读文件 → 🔒 + §8.2 文案 -->
          <EmptyState
            v-if="!showEditor && !mountFailed"
            type="forbidden"
            title="暂无编辑界面"
            :desc="`${selected.name}.json 暂无可视化编辑界面，可通过案件包导出后编辑。可视化界面将在后续版本开放。`"
          >
            <template #action>
              <NButton size="small" @click="router.push('/c/package')">前往案件包</NButton>
            </template>
          </EmptyState>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.om-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: calc(100vh - 92px);
}
.om-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
}
.om-title {
  margin: 0;
  font-size: 18px;
}
.om-case {
  font-size: 13px;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
.om-spin {
  display: block;
  padding: 48px 0;
}
.om-body {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  flex: 1;
  min-height: 0;
}
.om-tree {
  flex: 0 0 300px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 10px;
  max-height: calc(100vh - 160px);
  overflow: auto;
}
.om-group-title {
  font-size: 12px;
  font-weight: 600;
  padding: 4px 6px 2px;
}
.om-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  text-align: left;
  border: none;
  background: transparent;
  border-radius: 6px;
  padding: 7px 8px;
  cursor: pointer;
  font: inherit;
  color: inherit;
}
.om-item:hover {
  background: var(--sun-bg-card-hover);
}
.om-item.active {
  background: var(--sun-input-bg);
  outline: 1px solid var(--sun-border-active);
}
.om-item-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.om-item-name {
  font-size: 13px;
  font-weight: 500;
}
.om-item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  flex-wrap: wrap;
}
.om-layer {
  border: 1px solid var(--sun-border);
  border-radius: 8px;
  padding: 0 6px;
  font-size: 10px;
  line-height: 16px;
}
.om-bad {
  color: var(--sun-error-text);
}
.om-editor {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 12px;
}
.om-editor-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.om-editor-name {
  font-size: 15px;
  font-weight: 600;
}
.warn-banner {
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
}
.hint-banner {
  background: var(--sun-bg-card-hover);
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.om-editor-body {
  min-width: 0;
}
.btn-row {
  display: inline-flex;
  gap: 8px;
}
</style>
