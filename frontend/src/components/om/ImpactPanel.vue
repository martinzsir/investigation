<script setup lang="ts">
// S5-F3 影响面面板：五类可展开（🔴线索/🟡规则/🟡视图/🟢物化表/⚪函数），
// 已固证线索⚠单独标记（R2），uncertain 独立标记（§8.4），
// 动态 SQL 引用独立分组，partial/failed 显式警示（D10，绝不显示无影响）。
import { computed } from 'vue'
import { NAlert, NCollapse, NCollapseItem, NEmpty, NTag } from 'naive-ui'
import type { ImpactResult } from '../../api/endpoints/ontologyGeneric'
import {
  CATEGORY_META,
  categoryUnavailable,
  impactWarnings,
  isZeroImpact,
  itemsOf,
  solidifiedClues,
} from '../../domain/impact'

const props = defineProps<{ impact: ImpactResult }>()

const warnings = computed(() => impactWarnings(props.impact))
const zero = computed(() => isZeroImpact(props.impact))
const lockedIds = computed(() => new Set(solidifiedClues(props.impact).map((i) => i.id)))

const expandedNames = computed(() =>
  CATEGORY_META.filter((m) => itemsOf(props.impact, m.key).length > 0).map((m) => m.key),
)
</script>

<template>
  <div class="impact-panel">
    <!-- 总状态条 -->
    <NAlert
      :type="impact.status === 'failed' ? 'error' : impact.status === 'partial' ? 'warning' : 'success'"
      :show-icon="false"
      class="imp-status"
    >
      <div v-if="impact.status === 'failed'">
        ⛔ 影响面分析失败/不可用，下列结果不完整，<b>不得据此判断「无影响」</b>。请先处理失败原因后重新评估。
      </div>
      <div v-else-if="impact.status === 'partial'">
        ⚠ 影响面部分完成：部分类别不可用（已在下方标出），不可用 ≠ 无影响，请人工兜底核对。
      </div>
      <div v-else>
        ✅ 影响面分析完成（{{ impact.totals.total ?? 0 }} 项命中{{ impact.totals.uncertain ? `，其中不确定 ${impact.totals.uncertain} 项` : '' }}）
      </div>
    </NAlert>

    <!-- 警示清单 -->
    <NAlert
      v-for="(w, i) in warnings"
      :key="i"
      :type="w.level === 'danger' ? 'error' : 'warning'"
      class="imp-warn"
    >
      {{ w.text }}
    </NAlert>

    <!-- 变更内容 -->
    <div v-if="impact.changes.length" class="imp-changes">
      <div class="imp-sec-title">本次变更（仅删除/改名产生影响面，新增不影响下游）</div>
      <ul class="imp-change-list">
        <li v-for="c in impact.changes" :key="`${c.kind}:${c.ref}`" class="imp-change-item">
          <NTag size="tiny" type="error" :bordered="false">删除</NTag>
          <span>{{ c.label }}</span>
        </li>
      </ul>
    </div>

    <!-- 五类 -->
    <NCollapse :default-expanded-names="expandedNames">
      <NCollapseItem v-for="m in CATEGORY_META" :key="m.key" :name="m.key">
        <template #header>
          <span class="imp-cat-head">
            <NTag size="small" :type="m.color" :bordered="false">{{ m.label }}</NTag>
            <span class="imp-cat-count">{{ itemsOf(impact, m.key).length }}</span>
            <NTag v-if="categoryUnavailable(impact, m.key)" size="tiny" type="error" :bordered="false">
              不可用
            </NTag>
            <NTag
              v-else-if="m.key === 'clues' && lockedIds.size"
              size="tiny"
              type="error"
              :bordered="false"
            >⚠ {{ lockedIds.size }} 已固证/立案</NTag>
          </span>
        </template>
        <div class="imp-cat-hint dim">{{ m.hint }}</div>
        <div v-if="categoryUnavailable(impact, m.key)" class="imp-unavail">
          该类数据不可用，无法核对（不代表无影响）：
          <div v-for="f in impact.failures.filter((x) => x.category === m.key)" :key="f.reason" class="imp-fail-reason">
            — {{ f.reason }}
          </div>
        </div>
        <ul v-else-if="itemsOf(impact, m.key).length" class="imp-items">
          <li v-for="it in itemsOf(impact, m.key)" :key="`${it.id}:${it.certainty}`" class="imp-item">
            <div class="imp-item-head">
              <span class="mono imp-item-id">{{ it.title }}</span>
              <NTag v-if="it.certainty === 'uncertain'" size="tiny" type="warning" :bordered="false">
                不确定
              </NTag>
              <NTag v-if="it.solidified" size="tiny" type="error" :bordered="false">
                ⚠ {{ it.status || '已固证' }}
              </NTag>
              <NTag v-else-if="it.status" size="tiny" :bordered="false">{{ it.status }}</NTag>
            </div>
            <div class="imp-item-meta dim">
              {{ it.via }}<span v-if="it.refs.length"> · 命中：{{ it.refs.join('、') }}</span>
            </div>
          </li>
        </ul>
        <NEmpty v-else description="无命中" size="small" />
      </NCollapseItem>
    </NCollapse>

    <!-- 动态 SQL 不确定引用（§8.4） -->
    <div v-if="impact.dynamic_refs.length" class="imp-dyn">
      <div class="imp-sec-title">bindings 动态 SQL 词法命中（无法静态确定，人工核对）</div>
      <ul class="imp-items">
        <li v-for="(d, i) in impact.dynamic_refs" :key="`${d.token}:${i}`" class="imp-item">
          <div class="imp-item-head">
            <span class="mono">{{ d.token }}</span>
            <NTag size="tiny" type="warning" :bordered="false">不确定</NTag>
          </div>
          <div class="imp-item-meta dim">{{ d.file }} · {{ d.reason }}</div>
        </li>
      </ul>
    </div>

    <!-- 真空（complete 且零命中）：§8.7 覆盖说明，而非「无影响」 -->
    <div v-if="zero" class="imp-zero">
      当前五类引用核对均无命中。{{ impact.coverage_note }}；新增字段不影响下游，删除/改名才会产生影响面。
    </div>
  </div>
</template>

<style scoped>
.impact-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.imp-status,
.imp-warn {
  font-size: 12px;
}
.imp-sec-title {
  font-size: 12px;
  font-weight: 600;
  margin: 4px 0;
}
.imp-change-list,
.imp-items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.imp-change-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.imp-cat-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.imp-cat-count {
  font-weight: 600;
  font-size: 13px;
}
.imp-cat-hint {
  font-size: 11px;
  margin-bottom: 6px;
}
.imp-item-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
}
.imp-item-id {
  font-weight: 500;
}
.imp-item-meta {
  font-size: 11px;
  margin-top: 2px;
  word-break: break-all;
}
.imp-unavail {
  font-size: 12px;
  color: var(--sun-error-text);
}
.imp-fail-reason {
  padding-left: 8px;
  word-break: break-all;
}
.imp-dyn {
  border-top: 1px dashed var(--sun-border);
  padding-top: 8px;
}
.imp-zero {
  font-size: 12px;
  color: var(--sun-text-secondary);
  background: var(--sun-bg-card-hover);
  border-radius: 6px;
  padding: 8px 12px;
}
.dim {
  color: var(--sun-text-tertiary);
}
.mono {
  font-family: var(--sun-font-mono);
}
</style>
