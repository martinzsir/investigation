<script setup lang="ts">
// ★ FE-C-012 TraceabilityPanel（承重墙）：行 URI 等宽+可复制；原始字段全表；命中琥珀高亮。
// 溯源 URI 与命中字段来自服务端产物（只读），本组件不生成、不推断。
import { ref } from 'vue'
import { NIcon } from 'naive-ui'
import { CopyOutline, CheckmarkOutline } from '@vicons/ionicons5'
import type { SourceRowDto } from '../../api/endpoints/clues'
import MaskedField from '../common/MaskedField.vue'

defineProps<{ rows: SourceRowDto[] }>()

const copiedUri = ref('')

async function copyUri(uri: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(uri)
  } catch {
    // 内网剪贴板权限受限时兜底：选中文本由用户手动复制
    const ta = document.createElement('textarea')
    ta.value = uri
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
  copiedUri.value = uri
  window.setTimeout(() => {
    if (copiedUri.value === uri) copiedUri.value = ''
  }, 1500)
}
</script>

<template>
  <div class="trace">
    <p v-if="!rows.length" class="trace-empty">该条线索无溯源行记录</p>
    <div v-for="(row, i) in rows" :key="row.row_uri || i" class="trace-row">
      <div class="trace-head">
        <span class="trace-source">{{ row.source ?? `来源 ${i + 1}` }}</span>
        <code class="trace-uri" :title="row.row_uri">{{ row.row_uri }}</code>
        <button type="button" class="copy-btn" @click="copyUri(row.row_uri)">
          <NIcon :component="copiedUri === row.row_uri ? CheckmarkOutline : CopyOutline" />
          {{ copiedUri === row.row_uri ? '已复制' : '复制 URI' }}
        </button>
      </div>
      <table class="trace-fields">
        <tbody>
          <tr v-for="f in row.fields" :key="f.name" :class="{ 'field--hit': f.hit }">
            <th>{{ f.name }}</th>
            <td>
              <span v-if="f.hit" class="hit-mark" aria-hidden="true"></span>
              <MaskedField
                :value="f.value"
                :policy="f.policy ?? 'visible'"
                :type="f.mask ?? 'text'"
                :denied-hint="f.denied_hint"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.trace {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.trace-empty {
  color: var(--sun-text-tertiary);
  font-size: 13px;
  padding: 12px 0;
}
.trace-row {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
  background: rgba(4, 24, 40, 0.5);
}
.trace-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--sun-border);
  background: rgba(110, 222, 233, 0.05);
  flex-wrap: wrap;
}
.trace-source {
  font-size: 12px;
  color: var(--sun-info-text);
  border: 1px solid var(--sun-info-border);
  background: var(--sun-info-bg);
  border-radius: 12px;
  padding: 1px 8px;
  white-space: nowrap;
}
.trace-uri {
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-border-active);
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.copy-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--sun-border);
  background: transparent;
  color: var(--sun-text-secondary);
  border-radius: 4px;
  font-size: 12px;
  padding: 2px 8px;
  cursor: pointer;
  white-space: nowrap;
}
.copy-btn:hover {
  border-color: var(--sun-border-active);
  color: var(--sun-border-active);
}
.trace-fields {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.trace-fields th {
  text-align: left;
  color: var(--sun-text-tertiary);
  font-weight: 400;
  padding: 5px 12px;
  width: 130px;
  vertical-align: top;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.6);
}
.trace-fields td {
  padding: 5px 12px;
  color: var(--sun-text-primary);
  font-family: var(--sun-font-mono);
  border-bottom: 1px dashed rgba(16, 49, 74, 0.6);
}
.trace-fields tr:last-child th,
.trace-fields tr:last-child td {
  border-bottom: none;
}
/* 命中字段：琥珀高亮（FE-C-012） */
.trace-fields tr.field--hit td {
  color: var(--sun-gold);
  background: rgba(242, 181, 77, 0.08);
}
.field--hit th {
  color: var(--sun-gold);
}
.hit-mark {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sun-gold);
  margin-right: 6px;
  box-shadow: 0 0 6px rgba(242, 181, 77, 0.8);
}
</style>
