<script setup lang="ts">
// 代码逃生舱（FE-P-017，/c/escape）：为四类本体扩展生成文本代码桩。
// 纪律：只生成文本（不写盘、不注册、不执行）；注册点清单引导人工落地与
// loader 校验；无角色门槛（登录即可）。统计条零依赖 CSS（D2）。
import { computed, onMounted, ref } from 'vue'
import { NSpin, NButton, NInput, NSelect, NTag, useMessage } from 'naive-ui'
import {
  escapeHatchApi, type ExtType, type GenerateResult,
} from '../api/endpoints/escapeHatch'
import { presentError, isApiError } from '../api/errors'
import {
  EXT_META, EXT_ORDER, barPct, isEmptyStats, normalizeStats, statsMax as statsMaxOf,
  stubDescError, stubNameError,
} from '../domain/escapeHatch'
import EmptyState from '../components/common/EmptyState.vue'

const message = useMessage()

const extType = ref<ExtType>('function')
const name = ref('')
const desc = ref('')
const busy = ref(false)
const result = ref<GenerateResult | null>(null)
const activeFile = ref(0)

const statsLoading = ref(false)
const bars = ref(normalizeStats([]))

const typeOptions = EXT_ORDER.map((t) => ({
  value: t,
  label: `${EXT_META[t].label}（${t}）`,
}))

const nameErr = computed(() => stubNameError(name.value))
const descErr = computed(() => stubDescError(desc.value))
const canGen = computed(() => !nameErr.value && !descErr.value)
const statsEmpty = computed(() => isEmptyStats(bars.value))
const statsMax = computed(() => statsMaxOf(bars.value))

async function loadStats(): Promise<void> {
  statsLoading.value = true
  try {
    const s = await escapeHatchApi.stats()
    bars.value = normalizeStats(s.items)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    statsLoading.value = false
  }
}

onMounted(loadStats)

async function generate(): Promise<void> {
  if (!canGen.value) return
  busy.value = true
  result.value = null
  try {
    result.value = await escapeHatchApi.generate({
      ext_type: extType.value,
      name: name.value.trim(),
      description: desc.value.trim() || undefined,
    })
    activeFile.value = 0
    message.success('代码桩已生成（文本，未落盘）')
    void loadStats()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    busy.value = false
  }
}

async function copyCode(): Promise<void> {
  const file = result.value?.files[activeFile.value]
  if (!file) return
  try {
    await navigator.clipboard.writeText(file.content)
    message.success('已复制到剪贴板')
  } catch {
    message.warning('剪贴板不可用，请手动选择代码复制')
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>代码逃生舱</h2>
      <p class="dim hint">
        当声明式本体不够用时，生成四类扩展的代码桩与注册点清单。逃生舱<strong>只产出文本</strong>：
        不写盘、不注册、不执行——落地须经代码评审与 ontology loader 校验。
      </p>
    </div>

    <!-- 扩展使用统计（CSS 条形） -->
    <div class="card">
      <div class="card-title">近期扩展生成统计</div>
      <NSpin :show="statsLoading">
        <p v-if="statsEmpty" class="dim">暂无扩展生成记录。</p>
        <ul v-else class="bars">
          <li v-for="b in bars" :key="b.ext_type" class="bar-row">
            <span class="bar-label">{{ b.label }}</span>
            <span class="bar-track">
              <span class="bar-fill" :style="{ width: `${barPct(b.count, statsMax)}%` }" />
            </span>
            <span class="bar-n mono">{{ b.count }}</span>
          </li>
        </ul>
      </NSpin>
    </div>

    <!-- 生成表单 -->
    <div class="card">
      <div class="card-title">生成代码桩</div>
      <label class="form-row">
        <span class="form-label">扩展类型</span>
        <NSelect v-model:value="extType" :options="typeOptions" class="type-select" />
        <span class="dim form-hint">{{ EXT_META[extType].desc }}</span>
      </label>
      <label class="form-row">
        <span class="form-label">名称 <b>*</b></span>
        <NInput v-model:value="name" placeholder="标识符，字母/下划线开头，如 my_function" />
        <span v-if="nameErr" class="form-err">{{ nameErr }}</span>
      </label>
      <label class="form-row">
        <span class="form-label">说明（可选，≤500 字）</span>
        <NInput v-model:value="desc" type="textarea" :rows="3" placeholder="扩展意图/判据，用于生成桩内注释" />
        <span v-if="descErr" class="form-err">{{ descErr }}</span>
      </label>
      <div>
        <NButton type="primary" :loading="busy" :disabled="!canGen" @click="generate">生成代码桩</NButton>
      </div>
    </div>

    <!-- 生成结果 -->
    <div v-if="result" class="card result-card">
      <div class="card-title">
        生成结果（{{ result.files.length }} 个文件）
        <NTag size="tiny" :bordered="false" type="warning">文本未落盘</NTag>
      </div>
      <div class="file-tabs">
        <button
          v-for="(f, i) in result.files"
          :key="f.path"
          class="file-tab"
          :class="{ active: i === activeFile }"
          @click="activeFile = i"
        >
          {{ f.path.split('/').pop() }}
        </button>
      </div>
      <p class="mono dim file-path">{{ result.files[activeFile]?.path }}</p>
      <pre class="code-block">{{ result.files[activeFile]?.content }}</pre>
      <div class="row">
        <NButton size="small" @click="copyCode">复制代码</NButton>
      </div>
      <div class="reg-points">
        <div class="reg-title">注册点（人工落地，loader 将校验引用一致性）：</div>
        <ul>
          <li v-for="(p, i) in result.registration_points" :key="i" class="mono">{{ p }}</li>
        </ul>
      </div>
    </div>

    <EmptyState
      v-else-if="!busy"
      type="empty"
      title="尚未生成代码桩"
      desc="填写类型与名称后生成；生成结果仅为文本，需评审后手动落地"
    />
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
.card {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-title {
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.bars {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar-row {
  display: grid;
  grid-template-columns: 140px 1fr 40px;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.bar-label {
  color: var(--sun-text-secondary);
}
.bar-track {
  height: 12px;
  background: var(--sun-input-bg, rgba(255, 255, 255, 0.04));
  border-radius: 6px;
  overflow: hidden;
}
.bar-fill {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, rgba(109, 200, 236, 0.4), rgba(90, 216, 166, 0.85));
  border-radius: 6px;
  min-width: 2px;
}
.bar-n {
  text-align: right;
  color: var(--sun-text-tertiary);
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 560px;
}
.form-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.form-label b {
  color: var(--sun-error-text);
}
.form-hint {
  font-size: 11px;
}
.form-err {
  font-size: 12px;
  color: var(--sun-error-text);
}
.type-select {
  max-width: 320px;
}
.row {
  display: flex;
  gap: 10px;
}
.file-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.file-tab {
  border: 1px solid var(--sun-border);
  background: var(--sun-bg-card);
  color: var(--sun-text-secondary);
  border-radius: 6px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
  font-family: var(--sun-font-mono);
}
.file-tab.active {
  border-color: var(--sun-info-border, var(--sun-ok-border));
  color: var(--sun-text-primary);
  font-weight: 600;
}
.file-path {
  font-size: 11px;
  margin: 0;
}
.code-block {
  margin: 0;
  background: var(--sun-input-bg, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 12px;
  font-family: var(--sun-font-mono);
  color: var(--sun-text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 420px;
  overflow: auto;
}
.reg-points {
  border-top: 1px dashed var(--sun-border);
  padding-top: 8px;
  font-size: 12px;
}
.reg-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.reg-points ul {
  margin: 0;
  padding-left: 18px;
  color: var(--sun-text-secondary);
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
