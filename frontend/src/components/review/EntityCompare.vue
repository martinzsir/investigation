<script setup lang="ts">
// ★ FE-C-019 EntityCompare：实体裁决双栏对比 + 键盘流 A/R/D。
// 红线：永不自动合并——合并/驳回都只能由人工按键/点按触发，逐条留操作者+理由；
// 驳回理由必填（domain/review.ts reasonError，后端 400 双保险）。
// 差异行/相似依据行琥珀金 warn 高亮；属性遮蔽走 MaskedField（FE-C-010）。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NInput } from 'naive-ui'
import {
  diffRows,
  parseVerdictKey,
  reasonError,
  REASON_MAX,
  type VerdictAction,
} from '../../domain/review'
import type { ReviewEvidence, ReviewHistoryItem } from '../../api/endpoints/review'
import MaskedField from '../common/MaskedField.vue'

const props = defineProps<{
  candidate: ReviewEvidence
  history: ReviewHistoryItem[]
  operator: string
  busy?: boolean
  /** 队列剩余待处理条数（含当前条） */
  pendingCount?: number
}>()

const emit = defineEmits<{
  decide: [action: VerdictAction, reason: string]
  defer: []
}>()

const reason = ref('')
const errMsg = ref('')

const rows = computed(() => diffRows(props.candidate.attributes ?? []))
const pct = computed(() => `${Math.round((props.candidate.confidence ?? 0) * 100)}%`)
const pctClass = computed(() => {
  const v = props.candidate.confidence ?? 0
  return v >= 0.85 ? 'pct--ok' : v >= 0.6 ? 'pct--warn' : 'pct--error'
})

function reset(): void {
  reason.value = ''
  errMsg.value = ''
}

function doDecide(action: VerdictAction): void {
  if (props.busy) return
  const err = reasonError(action, reason.value)
  if (err) {
    errMsg.value = err
    return
  }
  emit('decide', action, reason.value.trim())
  reset()
}

function doDefer(): void {
  if (props.busy) return
  emit('defer')
  reset()
}

function onKeydown(e: KeyboardEvent): void {
  const k = parseVerdictKey(e)
  if (!k) return
  e.preventDefault()
  if (k === 'defer') doDefer()
  else doDecide(k)
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="ec">
    <div class="ec-redline" role="alert">
      <b>同名不同人，禁止自动合并</b>——以下候选须人工逐条裁决（A 同一人 / R 不同人 / D 跳过）
      <span v-if="pendingCount !== undefined" class="ec-pending">待处理 {{ pendingCount }} 条</span>
    </div>

    <div class="ec-main">
      <div class="ec-panel">
        <header class="ec-head">
          <div class="ec-name">
            <h3>{{ candidate.canonical_name }}</h3>
            <div class="ec-variants">
              <span class="ec-variants-label">书写变体</span>
              <code v-for="v in candidate.variants" :key="v" class="ec-variant">{{ v }}</code>
            </div>
          </div>
          <div class="ec-sim" :class="pctClass">
            <span class="ec-sim-num">{{ pct }}</span>
            <span class="ec-sim-label">相似度匹配度</span>
          </div>
        </header>

        <p class="ec-reason-line">归集依据：{{ candidate.merge_reason }}</p>

        <table class="ec-table">
          <thead>
            <tr>
              <th style="width: 130px">属性</th>
              <th>候选实体</th>
              <th>已归集实体</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.label" :class="{ 'row--diff': r.diff || r.basis }">
              <td class="ec-attr">{{ r.label }}</td>
              <td>
                <MaskedField
                  v-if="r.policy"
                  :value="r.left"
                  :policy="r.policy"
                  :type="r.mask ?? 'text'"
                />
                <template v-else>{{ r.left || '—' }}</template>
              </td>
              <td>
                <MaskedField
                  v-if="r.policy"
                  :value="r.right"
                  :policy="r.policy"
                  :type="r.mask ?? 'text'"
                />
                <template v-else>{{ r.right || '—' }}</template>
              </td>
            </tr>
          </tbody>
        </table>

        <div class="ec-decide">
          <div class="ec-reason">
            <label>裁决理由（驳回必填，{{ reason.length }}/{{ REASON_MAX }}）</label>
            <NInput
              v-model:value="reason"
              type="textarea"
              :rows="2"
              :maxlength="REASON_MAX + 20"
              placeholder="确认为不同人时必须填写理由；确认为同一人可填补充说明"
              @update:value="errMsg = ''"
            />
            <p v-if="errMsg" class="ec-err">{{ errMsg }}</p>
          </div>
          <div class="ec-btns">
            <NButton class="btn-merge" :loading="busy" @click="doDecide('merge')">
              <span class="keyhint">A</span> 确认为同一人
            </NButton>
            <NButton class="btn-reject" type="primary" :loading="busy" @click="doDecide('reject')">
              <span class="keyhint">R</span> 确认为不同人
            </NButton>
            <NButton class="btn-defer" :disabled="busy" @click="doDefer">
              <span class="keyhint">D</span> 跳过
            </NButton>
          </div>
          <p class="ec-sign">裁决将以 <b>{{ operator }}</b> 名义写入审计链</p>
        </div>
      </div>

      <aside class="ec-history">
        <h4>历史裁决记录</h4>
        <p v-if="!history.length" class="ec-history-empty">暂无裁决记录</p>
        <ul v-else class="ec-history-list">
          <li v-for="h in history" :key="`${h.candidate_id}-${h.occurred_at}`">
            <span class="hdot" :class="h.action === 'merge' ? 'hdot--merge' : 'hdot--reject'" aria-hidden="true" />
            <div class="hbody">
              <p class="htitle">
                {{ h.canonical_name }}
                <span class="haction" :class="h.action === 'merge' ? 'haction--merge' : 'haction--reject'">
                  {{ h.action === 'merge' ? '同一人' : '不同人' }}
                </span>
              </p>
              <p class="hmeta">
                置信度 {{ Math.round(h.confidence * 100) }}% · {{ h.operator }} · {{ h.occurred_at }}
              </p>
              <p v-if="h.reason" class="hreason">{{ h.reason }}</p>
            </div>
          </li>
        </ul>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.ec {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ec-redline {
  border: 1px solid var(--sun-error-border);
  background: var(--sun-error-bg);
  color: var(--sun-error-text);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
}
.ec-pending {
  margin-left: 10px;
  font-family: var(--sun-font-mono);
}
.ec-main {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 12px;
  align-items: start;
}
.ec-panel {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ec-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.ec-name h3 {
  margin: 0 0 6px;
  font-size: 16px;
  color: var(--sun-text-primary);
}
.ec-variants {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.ec-variants-label {
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.ec-variant {
  font-family: var(--sun-font-mono);
  font-size: 12px;
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  border-radius: 4px;
  padding: 1px 8px;
}
.ec-sim {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 96px;
}
.ec-sim-num {
  font-family: var(--sun-font-mono);
  font-size: 30px;
  font-weight: 700;
  line-height: 1.1;
}
.ec-sim-label {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.pct--ok {
  color: var(--sun-ok-text);
}
.pct--warn {
  color: var(--sun-warn-text);
}
.pct--error {
  color: var(--sun-error-text);
}
.ec-reason-line {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.ec-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.ec-table th {
  text-align: left;
  font-weight: 400;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  padding: 6px 10px;
  border-bottom: 1px solid var(--sun-border);
}
.ec-table td {
  padding: 8px 10px;
  border-bottom: 1px dashed rgba(16, 49, 74, 0.6);
  color: var(--sun-text-secondary);
}
.ec-attr {
  color: var(--sun-text-tertiary);
  font-size: 12px;
}
/* 差异行/相似依据行：琥珀金 warn 高亮（FE-C-019） */
.row--diff td {
  background: var(--sun-warn-bg);
  color: var(--sun-warn-text);
}
.ec-decide {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-top: 1px solid var(--sun-border);
  padding-top: 12px;
}
.ec-reason {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ec-reason label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.ec-err {
  margin: 0;
  font-size: 12px;
  color: var(--sun-error-text);
}
.ec-btns {
  display: flex;
  gap: 10px;
}
.keyhint {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border: 1px solid currentColor;
  border-radius: 3px;
  font-family: var(--sun-font-mono);
  font-size: 11px;
  margin-right: 4px;
}
.btn-merge {
  font-weight: 600;
}
.btn-reject {
  font-weight: 600;
}
.ec-sign {
  margin: 0;
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.ec-history {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 14px;
}
.ec-history h4 {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--sun-text-primary);
}
.ec-history-empty {
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.ec-history-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ec-history-list li {
  display: flex;
  gap: 8px;
}
.hdot {
  flex: 0 0 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 6px;
}
/* 设计图 05：红=同一人 / 青=不同人 */
.hdot--merge {
  background: var(--sun-error-text);
}
.hdot--reject {
  background: var(--sun-border-active);
}
.htitle {
  margin: 0;
  font-size: 13px;
  color: var(--sun-text-primary);
}
.haction {
  margin-left: 6px;
  font-size: 11px;
  padding: 0 6px;
  border-radius: 10px;
  border: 1px solid;
}
.haction--merge {
  color: var(--sun-error-text);
  border-color: var(--sun-error-border);
}
.haction--reject {
  color: var(--sun-border-active);
  border-color: var(--sun-info-border);
}
.hmeta {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.hreason {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
</style>
