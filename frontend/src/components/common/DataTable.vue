<script setup lang="ts" generic="T extends Record<string, unknown>">
// ★ FE-C-003 DataTable：分页表（非虚拟滚动）——?page= 可分享（URL 同步在页面层）、
// 跳页输入框、page_size=50、默认时间倒序（服务端排序）。列遮蔽由调用方经
// cell 插槽挂 MaskedField，组件不感知字段策略。
import { computed, ref } from 'vue'
import { NButton, NInput } from 'naive-ui'
import { clampPage, jumpPageError, totalPages } from '../../domain/pagination'

export interface DataTableColumn {
  key: string
  title: string
  width?: string
  mono?: boolean
}

const props = withDefaults(
  defineProps<{
    columns: DataTableColumn[]
    items: T[]
    total: number
    page: number
    pageSize?: number
    loading?: boolean
    rowKey?: (item: T) => string
  }>(),
  { pageSize: 50, loading: false },
)

const emit = defineEmits<{
  'update:page': [page: number]
  'row-click': [item: T]
}>()

const pages = computed(() => totalPages(props.total, props.pageSize))
const curPage = computed(() => clampPage(props.page, props.total, props.pageSize))

const jumpInput = ref('')
const jumpErr = ref('')

function go(p: number): void {
  const target = clampPage(p, props.total, props.pageSize)
  if (target !== curPage.value) emit('update:page', target)
}

function jump(): void {
  const err = jumpPageError(jumpInput.value, props.total, props.pageSize)
  jumpErr.value = err
  if (err) return
  go(Number.parseInt(jumpInput.value.trim(), 10))
  jumpInput.value = ''
}

function cellText(item: T, key: string): string {
  const v = item[key]
  return v === undefined || v === null ? '' : String(v)
}
function keyOf(item: T, idx: number): string {
  return props.rowKey ? props.rowKey(item) : String(idx)
}
</script>

<template>
  <div class="dt">
    <div class="dt-scroll">
      <table class="dt-table">
        <thead>
          <tr>
            <th v-for="col in columns" :key="col.key" :style="col.width ? { width: col.width } : undefined">
              {{ col.title }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td :colspan="columns.length" class="dt-loading">加载中…</td>
          </tr>
          <tr v-else-if="!items.length">
            <td :colspan="columns.length" class="dt-empty">无数据</td>
          </tr>
          <tr
            v-for="(item, idx) in items"
            v-else
            :key="keyOf(item, idx)"
            class="dt-row"
            @click="emit('row-click', item)"
          >
            <td v-for="col in columns" :key="col.key" :class="{ mono: col.mono }">
              <slot :name="`cell-${col.key}`" :item="item" :value="cellText(item, col.key)">
                {{ cellText(item, col.key) || '—' }}
              </slot>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="dt-pager">
      <span class="dt-total">共 {{ total }} 条</span>
      <NButton size="tiny" :disabled="curPage <= 1" @click="go(curPage - 1)">上一页</NButton>
      <span class="dt-page">第 <b>{{ curPage }}</b> / {{ pages }} 页</span>
      <NButton size="tiny" :disabled="curPage >= pages" @click="go(curPage + 1)">下一页</NButton>
      <span class="dt-jump">
        跳至
        <NInput
          v-model:value="jumpInput"
          size="tiny"
          class="dt-jump-input"
          placeholder="页"
          @keyup.enter="jump"
        />
        页
        <NButton size="tiny" type="primary" @click="jump">Go</NButton>
      </span>
      <span v-if="jumpErr" class="dt-jump-err">{{ jumpErr }}</span>
    </div>
  </div>
</template>

<style scoped>
.dt {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dt-scroll {
  overflow-x: auto;
}
.dt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
}
.dt-table th {
  text-align: left;
  font-weight: 400;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  padding: 8px 10px;
  border-bottom: 1px solid var(--sun-border);
  white-space: nowrap;
}
.dt-table td {
  padding: 9px 10px;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.6);
  vertical-align: middle;
}
.dt-table td.mono {
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.dt-row {
  cursor: pointer;
}
.dt-row:hover td {
  background: rgba(110, 222, 233, 0.05);
}
.dt-loading,
.dt-empty {
  text-align: center;
  color: var(--sun-text-tertiary);
  padding: 24px 0;
}
.dt-pager {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.dt-total {
  font-family: var(--sun-font-mono);
  color: var(--sun-text-tertiary);
}
.dt-page b {
  color: var(--sun-border-active);
  font-family: var(--sun-font-mono);
}
.dt-jump {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 8px;
}
.dt-jump-input {
  width: 64px;
}
.dt-jump-err {
  color: var(--sun-error-text);
}
</style>
