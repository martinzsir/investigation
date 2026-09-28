<script setup lang="ts">
// 交汇锚点卡片：三维**命中矩阵**并列渲染（count + precision + weight）。
//
// 为什么不用一个分数条
// --------------------
// score 是 Σ(维度权 × 精度权 × 重复因子)，单看它无法分辨：
//   「时间维 12 条 date 档，权重 0.6」 vs 「空间维 4 条 minute 档，权重 1.6」
// 合并成一个总分，精度加权就被重新掩盖——正兵会以为 12 条比 4 条更值得看。
// 故每格强制给「条数 + 精度档 + 权重」，分数只在右侧弱化显示并注明仅排序用。
import { NTag, NSpin } from 'naive-ui'
import ConvergenceGraph from './ConvergenceGraph.vue'
import {
  DIM_LABEL,
  DIM_ORDER,
  PRECISION_LABEL,
  type ConvergenceDetailResult,
  type ConvergenceDim,
  type ConvergenceItem,
} from '../../api/endpoints/convergence'

const props = defineProps<{
  row: ConvergenceItem
  expanded?: boolean
  detail?: ConvergenceDetailResult | null
  detailLoading?: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle'): void
  (e: 'open-observation', obsId: string): void
}>()

function dimOf(dim: string): ConvergenceDim | null {
  const d = props.row.dimensions?.[dim as 'space' | 'time' | 'relation']
  return d && d.hit ? d : null
}

/** 街道短名：std_address 形如 省/市/区/全址，取最后一段 */
function shortAddr(addr: string): string {
  const s = String(addr || '')
  const seg = s.split('/')
  return seg[seg.length - 1] || s || '—'
}

/** 精度档配色：date 档显式标 warning——防止被误读成"同时" */
function precisionType(p: string): 'success' | 'info' | 'warning' | 'default' {
  if (p === 'minute' || p === 'second') return 'success'
  if (p === 'hour') return 'info'
  if (p === 'date') return 'warning'
  return 'default'
}

function precisionHint(p: string): string {
  return PRECISION_LABEL[p]?.hint ?? '精度不明，权重最低'
}

/** 未命中原因必须写明，不许由其他维度的命中盖过去 */
function missReason(dim: string): string {
  const d = props.row.dimensions?.[dim as 'space' | 'time' | 'relation']
  return d?.reason || '该维度无命中（未被其他维度掩盖）'
}
</script>

<template>
  <div class="cc">
    <div class="cc-head" data-testid="cc-head" @click="emit('toggle')">
      <div class="cc-h1">
        <NTag
          v-if="row.person_ambiguous"
          size="tiny"
          round
          type="error"
          data-testid="cc-ambiguous-tag"
          :title="`同名异人未裁决：候选主键 ${(row.ambiguous_candidates || []).join('、')}`"
        >
          重名待裁决
        </NTag>
        <span class="cc-person" data-testid="cc-person">
          {{ (row.person_names || []).join('、') || row.person_key }}
        </span>
        <span class="cc-date">{{ row.date }}</span>
        <span class="cc-addr" :title="(row.std_addresses || []).join('；')">
          {{ (row.std_addresses || []).map(shortAddr).join('、') }}
        </span>
      </div>
      <div class="cc-h2">
        <span
          v-for="cp in row.co_present || []"
          :key="cp"
          class="cc-cp"
          title="同现主体：仅陈述空间/时间接近，不构成接触结论"
        >
          同现 {{ cp }}
        </span>
        <span
          class="cc-score"
          data-testid="cc-score"
          title="score 仅用于排序，不代表风险高低；强度请看下方三维各自的条数与精度档"
        >
          {{ row.score }} / {{ row.max_score }}
        </span>
      </div>
    </div>

    <!-- 命中矩阵：三维并列，每格 count + precision + weight（红线 1 的落点） -->
    <div class="cc-matrix" data-testid="cc-matrix">
      <div
        v-for="dim in DIM_ORDER"
        :key="dim"
        class="cc-cell"
        :class="{ 'cc-cell--miss': !dimOf(dim) }"
        :data-testid="`cc-cell-${dim}`"
        :title="dimOf(dim) ? precisionHint(dimOf(dim)!.precision) : missReason(dim)"
      >
        <span class="cc-dim">{{ DIM_LABEL[dim] }}</span>
        <template v-if="dimOf(dim)">
          <span class="cc-count">{{ dimOf(dim)!.count }} 条</span>
          <NTag size="tiny" round :type="precisionType(dimOf(dim)!.precision)">
            {{ PRECISION_LABEL[dimOf(dim)!.precision]?.label || dimOf(dim)!.precision }}
          </NTag>
          <span class="cc-w">权 {{ dimOf(dim)!.weight }}</span>
        </template>
        <span v-else class="cc-miss">—</span>
      </div>
    </div>

    <ul v-if="row.claims && row.claims.length" class="cc-claims" data-testid="cc-claims">
      <li v-for="(c, i) in row.claims" :key="i">{{ c }}</li>
    </ul>

    <NSpin v-if="detailLoading" size="small" />
    <div v-else-if="expanded && detail" class="cc-detail" data-testid="cc-detail">
      <!-- 证据构成图：先看结构（哪一维、什么精度），再看下面逐条支撑观察。
           图不可用时组件内部降级为结构化列表，此处不另设空态。 -->
      <ConvergenceGraph
        :item="row"
        :detail="detail"
        @open-observation="emit('open-observation', $event)"
      />
      <p v-if="detail.note" class="cc-detail-note">{{ detail.note }}</p>
      <div v-for="dim in DIM_ORDER" :key="dim" class="cc-dgroup">
        <h4 class="cc-dtitle">
          {{ DIM_LABEL[dim] }}维 · 支撑观察 {{ (detail.support?.[dim] || []).length }}
        </h4>
        <p v-if="!(detail.support?.[dim] || []).length" class="dim">
          该维度无支撑观察
        </p>
        <ul v-else class="cc-dlist">
          <li v-for="s in detail.support[dim]" :key="s.observation_id" class="cc-ditem">
            <div class="cc-dhead">
              <span class="cc-dlens">{{ s.lens_name }}</span>
              <a
                class="cc-dlink"
                href="javascript:void(0)"
                @click.stop="emit('open-observation', s.observation_id)"
              >
                {{ s.title }}
              </a>
              <NTag
                v-if="s.degraded"
                size="tiny"
                round
                type="error"
                :title="s.degraded_reason"
              >
                数据未齐
              </NTag>
            </div>
            <p v-if="s.basis" class="cc-dbasis">{{ s.basis }}</p>
          </li>
        </ul>
      </div>
      <p v-if="row.falsification" class="cc-fals">
        <b>证伪条件：</b>{{ row.falsification }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.cc-head {
  cursor: pointer;
}
.cc-h1 {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.cc-person {
  font-size: 13px;
  font-weight: 600;
}
.cc-date {
  font-size: 12px;
  color: var(--sun-text-secondary, #555);
}
.cc-addr {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cc-h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.cc-cp {
  font-size: 11px;
  color: var(--sun-info, #2080f0);
  border: 1px solid var(--sun-border, #e5e5e5);
  border-radius: 4px;
  padding: 1px 5px;
}
.cc-score {
  margin-left: auto;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-variant-numeric: tabular-nums;
}
.cc-matrix {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.cc-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--sun-border, #e5e5e5);
  border-radius: 5px;
  padding: 4px 8px;
  font-size: 11px;
  min-width: 190px;
}
.cc-cell--miss {
  opacity: 0.5;
  border-style: dashed;
}
.cc-dim {
  font-weight: 600;
  min-width: 28px;
}
.cc-count {
  font-variant-numeric: tabular-nums;
}
.cc-w {
  color: var(--sun-text-tertiary);
  margin-left: auto;
}
.cc-miss {
  color: var(--sun-text-tertiary);
}
.cc-claims {
  margin: 0 0 6px;
  padding-left: 16px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--sun-text-secondary, #555);
}
.cc-detail {
  border-top: 1px dashed var(--sun-border, #e5e5e5);
  margin-top: 8px;
  padding-top: 8px;
}
.cc-detail-note {
  margin: 0 0 8px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.cc-dgroup {
  margin-bottom: 10px;
}
.cc-dtitle {
  margin: 0 0 4px;
  font-size: 12px;
}
.cc-dlist {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cc-ditem {
  border-left: 2px solid var(--sun-border, #e5e5e5);
  padding-left: 8px;
}
.cc-dhead {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.cc-dlens {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.cc-dlink {
  font-size: 12px;
  color: var(--sun-primary, #2080f0);
  text-decoration: none;
}
.cc-dlink:hover {
  text-decoration: underline;
}
.cc-dbasis {
  margin: 2px 0 0;
  font-size: 11px;
  line-height: 1.55;
  color: var(--sun-text-secondary, #555);
}
.cc-fals {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--sun-text-tertiary);
}
.dim {
  color: var(--sun-text-tertiary);
  font-size: 11px;
}
</style>
