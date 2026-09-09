<script setup lang="ts">
// 隔离区 + 清洗留痕（MVP-4，/c/quarantine）。
// 红线五：零隔离必须显式说「本次装载无数据被丢弃」（empty_message），不允许沉默空表；
// 隔离样本一律遮蔽展示（samples_masked）；四类原因服务端聚合 stats。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { quarantineApi, QUARANTINE_REASONS, type QuarantinePage } from '../api/endpoints/quarantine'
import { presentError, isApiError } from '../api/errors'
import { PAGE_SIZE_DEFAULT, totalPages, clampPage } from '../domain/pagination'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

const loading = ref(false)
const reasonFilter = ref('')
const page = ref(1)
const data = ref<QuarantinePage | null>(null)
const cleanTrace = ref<{ items: Array<Record<string, unknown>>; total: number }>({ items: [], total: 0 })

const pageCount = computed(() =>
  data.value ? totalPages(data.value.total, data.value.page_size || PAGE_SIZE_DEFAULT) : 1,
)

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    data.value = null
    cleanTrace.value = { items: [], total: 0 }
    return
  }
  loading.value = true
  try {
    const [q, t] = await Promise.all([
      quarantineApi.list(cs.currentCaseId, {
        reason: reasonFilter.value || undefined,
        page: page.value,
        pageSize: PAGE_SIZE_DEFAULT,
      }),
      quarantineApi.cleanTrace(cs.currentCaseId, { pageSize: PAGE_SIZE_DEFAULT }),
    ])
    data.value = q
    cleanTrace.value = { items: t.items as unknown as Array<Record<string, unknown>>, total: t.total }
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, () => { page.value = 1; load() }, { immediate: true })

function setReason(r: string): void {
  reasonFilter.value = r
  page.value = 1
  load()
}
function go(p: number): void {
  page.value = clampPage(p, pageCount.value)
  load()
}

function reasonLabel(r: string): string {
  return QUARANTINE_REASONS.find((x) => x.value === r)?.label ?? r
}
function pct(rate: unknown): string {
  return `${(Number(rate ?? 0) * 100).toFixed(2)}%`
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>隔离区</h2>
      <p class="dim hint">
        装载时被挡下的行（类型转换失败/空值拒绝/去重丢弃）；样本已遮蔽。隔离区是数据去向的交代，不是错误日志。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="隔离数据按案件归属" />

    <template v-else>
      <!-- 原因筛选 + stats -->
      <div class="filter-bar">
        <button class="chip" :class="{ active: !reasonFilter }" @click="setReason('')">
          全部 <span class="n">{{ data?.total ?? 0 }}</span>
        </button>
        <button
          v-for="r in QUARANTINE_REASONS"
          :key="r.value"
          class="chip"
          :class="{ active: reasonFilter === r.value }"
          @click="setReason(reasonFilter === r.value ? '' : r.value)"
        >
          {{ r.label }} <span class="n">{{ data?.stats?.[r.value] ?? 0 }}</span>
        </button>
      </div>

      <NSpin :show="loading">
        <!-- 零隔离显式文案（红线五） -->
        <EmptyState
          v-if="!loading && data && data.items.length === 0"
          type="empty"
          title="本次装载无数据被丢弃"
          :desc="data.empty_message ?? '所有装载行均通过校验，隔离区为空'"
        />

        <div v-else-if="data && data.items.length" class="grid-wrap">
          <table class="grid">
            <thead>
              <tr><th>对象.属性</th><th>源列</th><th>原因</th><th>主体</th><th>样本（已遮蔽）</th><th>隔离时间</th></tr>
            </thead>
            <tbody>
              <tr v-for="(q, i) in data.items" :key="i">
                <td class="mono">{{ q.object }}.{{ q.property }}</td>
                <td class="dim">{{ q.src_column }}</td>
                <td><NTag size="tiny" :bordered="false" :type="q.reason === 'cast_error' ? 'error' : 'warning'">{{ reasonLabel(q.reason) }}</NTag></td>
                <td>{{ q.name_value || '—' }}</td>
                <td class="dim samples">{{ q.samples_masked.join('；') }}</td>
                <td class="dim">{{ q.quarantined_at }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 分页 -->
        <div v-if="data && data.total > (data.page_size || PAGE_SIZE_DEFAULT)" class="pager">
          <NButton size="tiny" :disabled="page <= 1" @click="go(page - 1)">上一页</NButton>
          <span class="dim">{{ page }} / {{ pageCount }}</span>
          <NButton size="tiny" :disabled="page >= pageCount" @click="go(page + 1)">下一页</NButton>
        </div>

        <!-- 清洗留痕 -->
        <div class="card">
          <div class="card-title">清洗留痕（清洗前后对比）</div>
          <p class="dim hint" v-if="!cleanTrace.items.length">暂无清洗留痕（未运行过 BUILD/RESCAN）。</p>
          <div v-else class="grid-wrap">
            <table class="grid">
              <thead>
                <tr><th>对象.属性</th><th>清洗规则</th><th>清洗前行数</th><th>清洗后行数</th><th>丢弃</th><th>丢弃率</th><th>来源</th></tr>
              </thead>
              <tbody>
                <tr v-for="(t, i) in cleanTrace.items" :key="i">
                  <td class="mono">{{ t.object }}.{{ t.property }}</td>
                  <td class="mono dim">{{ (t.rules as string[])?.join('、') }}</td>
                  <td>{{ t.rows_before }}</td>
                  <td>{{ t.rows_after }}</td>
                  <td :class="{ 'drop-warn': Number(t.dropped_rows) > 0 }">{{ t.dropped_rows }}</td>
                  <td>{{ pct(t.rate) }}</td>
                  <td class="dim">{{ t.source }} · {{ t.created_at }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.hint { font-size: 12px; margin: 4px 0 0; }
.filter-bar { display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
  border: 1px solid var(--sun-border); background: var(--sun-bg-card); color: var(--sun-text-secondary);
  border-radius: 16px; padding: 4px 12px; font-size: 12px; cursor: pointer;
}
.chip.active { border-color: var(--sun-info-text, var(--sun-ok-text)); color: var(--sun-text-primary); font-weight: 600; }
.chip .n { color: var(--sun-text-tertiary); margin-left: 4px; }
.grid-wrap { border: 1px solid var(--sun-border); border-radius: 6px; background: var(--sun-bg-card); overflow: auto; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; white-space: nowrap; }
.samples { max-width: 320px; word-break: break-all; }
.drop-warn { color: var(--sun-warn-text); font-weight: 600; }
.pager { display: flex; align-items: center; gap: 10px; justify-content: center; }
.card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.card-title { font-size: 13px; font-weight: 600; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
