<script setup lang="ts">
// ETL 管道（MVP-4，/c/etl）。
// bindings.json 派生视图：每对象源表/清洗规则/CAST 失败策略/空值策略/去重；
// 红线四：复合列（composite_props）只诊断 + 给出路（A 拆 source_sql / B 整列降级），
// 无「忽略继续」、无自动改写——「拆分」按钮恒 disabled（改 source_sql 在接入侧/案件包）。
// 清洗策略变更改变装载行为 → 🔴 危险确认 + 理由必填。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NTag, NSelect, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  etlApi, CAST_ERROR_POLICIES, NULL_POLICIES,
  type EtlSource,
} from '../api/endpoints/etl'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import {
  CAST_ERROR_LABEL, CAST_ERROR_DEFAULT_LABEL, NULL_POLICY_LABEL,
  isValidCastErrorPolicy, isValidNullPolicy, compositeList, PATH_GUIDE,
} from '../domain/etlConfig'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const sources = ref<EtlSource[]>([])

const canWrite = computed(() => canWriteConfig(auth.clearance))
const dirty = ref(false)

const castOptions = [
  { label: `缺省：${CAST_ERROR_DEFAULT_LABEL}`, value: '' },
  ...CAST_ERROR_POLICIES.map((v) => ({ label: CAST_ERROR_LABEL[v], value: v })),
]
const nullOptions = [
  { label: '缺省（未声明）', value: '' },
  ...NULL_POLICIES.map((v) => ({ label: NULL_POLICY_LABEL[v], value: v })),
]

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    sources.value = []
    return
  }
  loading.value = true
  try {
    const doc = await etlApi.getPipeline(cs.currentCaseId)
    sources.value = doc.sources
    dirty.value = false
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

/** 某对象某属性的 cast 策略（'' = 缺省降级 NULL） */
function castOf(s: EtlSource, prop: string): string {
  return s.on_cast_error?.[prop] ?? ''
}
function nullOf(s: EtlSource, prop: string): string {
  return s.null_policy?.[prop] ?? ''
}
function setCast(s: EtlSource, prop: string, v: string): void {
  if (!v) delete s.on_cast_error[prop]
  else {
    if (!isValidCastErrorPolicy(v)) return
    s.on_cast_error[prop] = v
  }
  dirty.value = true
}
function setNull(s: EtlSource, prop: string, v: string): void {
  if (!v) delete s.null_policy[prop]
  else {
    if (!isValidNullPolicy(v)) return
    s.null_policy[prop] = v
  }
  dirty.value = true
}

function policyProps(s: EtlSource): string[] {
  // 属性策略行：对象绑定里出现过的属性（on_cast_error/null_policy 键集）
  return Array.from(new Set([...Object.keys(s.on_cast_error ?? {}), ...Object.keys(s.null_policy ?? {})]))
}

const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
function askSave(): void {
  confirmReason.value = ''
  confirmOpen.value = true
}
async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  confirmSaving.value = true
  try {
    await etlApi.savePipeline(cs.currentCaseId, {
      sources: sources.value.map((s) => ({
        object: s.object,
        clean: s.clean,
        on_cast_error: s.on_cast_error,
        null_policy: s.null_policy,
        dedup_key: s.dedup_key,
      })),
      reason: confirmReason.value,
    })
    message.success('ETL 策略已保存并留痕；下次装载/RESCAN 生效')
    confirmOpen.value = false
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>ETL 管道</h2>
      <p class="dim hint">
        源表 → 语义对象的清洗策略：CAST 失败/空值如何处置、去重键。复合列只诊断不出路外操作。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="管道配置按案件快照归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 清洗策略决定哪些行进模型、哪些行进隔离区；变更影响全部装载结果，保存即记入审计链。
      </div>

      <NSpin :show="loading">
        <div v-for="s in sources" :key="s.object" class="source-card">
          <div class="source-head">
            <span class="mono obj-name">{{ s.object }}</span>
            <span class="dim">← 源表 {{ s.source_table ?? '（source_sql 自由查询）' }}</span>
            <NTag size="small" :bordered="false">去重键：{{ s.dedup_key.join('、') || '—' }}</NTag>
            <NTag size="small" :bordered="false" type="info">{{ s.dedup_on_conflict }}</NTag>
          </div>

          <div class="clean-row dim">
            清洗规则：
            <NTag v-for="c in s.clean" :key="c" size="small" :bordered="false" class="mono">{{ c }}</NTag>
            <span v-if="!s.clean.length">无</span>
          </div>

          <!-- 属性策略 -->
          <div class="grid-wrap">
            <table class="grid">
              <thead>
                <tr><th>属性</th><th>CAST 失败策略</th><th>空值策略</th></tr>
              </thead>
              <tbody>
                <tr v-for="prop in policyProps(s)" :key="prop">
                  <td class="mono">{{ prop }}</td>
                  <td>
                    <NSelect
                      :value="castOf(s, prop)" size="tiny" :options="castOptions" :disabled="!canWrite"
                      @update:value="(v: string) => setCast(s, prop, v)"
                    />
                  </td>
                  <td>
                    <NSelect
                      :value="nullOf(s, prop)" size="tiny" :options="nullOptions" :disabled="!canWrite"
                      @update:value="(v: string) => setNull(s, prop, v)"
                    />
                  </td>
                </tr>
                <tr v-if="!policyProps(s).length">
                  <td colspan="3" class="dim empty-row">该对象暂无逐属性策略（全部缺省：CAST 失败降级 NULL + 诊断）</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 复合列诊断（红线四：只诊断 + 出路，无忽略继续） -->
          <div v-if="compositeList(s).length" class="composite">
            <div class="composite-title">⛔ 复合列诊断（{{ compositeList(s).length }}）——无法逐列 CAST/映射：</div>
            <div v-for="(c, i) in compositeList(s)" :key="i" class="composite-item">
              <div><span class="mono">{{ String(c.prop) }}</span>：<code class="mono">{{ String(c.source_sql ?? '') }}</code></div>
              <div v-if="c.reason" class="dim">{{ String(c.reason) }}</div>
            </div>
            <div class="paths">
              <div class="path-item"><b>出路 A（推荐）</b>：{{ PATH_GUIDE.A_split_source_sql }}</div>
              <div class="path-item"><b>出路 B</b>：{{ PATH_GUIDE.B_degrade_column }}</div>
            </div>
            <div class="path-actions">
              <NButton size="tiny" disabled>拆分为独立源列（在接入向导/案件包中改 source_sql）</NButton>
              <span class="dim">本页不提供「忽略继续」——复合列必须走 A 或 B，不得静默入库</span>
            </div>
          </div>
        </div>

        <div v-if="canWrite" class="save-bar">
          <NButton type="primary" danger :disabled="!dirty" @click="askSave">保存管道策略（危险变更）</NButton>
          <span v-if="!dirty" class="dim">无未保存修改</span>
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="true"
      detail="bindings.json 清洗策略（CAST 失败/空值/去重）变更，下次装载生效"
      :loading="confirmSaving"
      title="ETL 策略变更确认"
      @confirm="doSave"
    />
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.hint { font-size: 12px; margin: 4px 0 0; }
.notice-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.source-card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.source-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.obj-name { font-weight: 700; }
.clean-row { display: flex; align-items: center; gap: 6px; font-size: 12px; flex-wrap: wrap; }
.grid-wrap { border: 1px solid var(--sun-border); border-radius: 4px; overflow: auto; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; }
.grid .n-select { width: 260px; }
.empty-row { text-align: center; padding: 12px; }
.composite {
  border: 1px solid var(--sun-error-border); background: var(--sun-error-bg);
  border-radius: 4px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px;
}
.composite-title { font-size: 13px; font-weight: 600; color: var(--sun-error-text); }
.composite-item { font-size: 12px; }
.composite-item code { word-break: break-all; }
.paths { display: flex; flex-direction: column; gap: 4px; font-size: 12px; margin-top: 4px; }
.path-item { color: var(--sun-text-secondary); }
.path-actions { display: flex; align-items: center; gap: 10px; margin-top: 4px; flex-wrap: wrap; }
.save-bar { display: flex; align-items: center; gap: 12px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
