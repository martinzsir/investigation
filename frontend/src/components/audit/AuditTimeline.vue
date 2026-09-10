<script setup lang="ts">
// FE-P-006 审计链（MVP-1 简化版）：竖向链式节点卡片（06 审计链.jpeg）。
// 节点：序号圆标 + 操作人真实姓名 + 状态迁移 + 时间戳 + 本体版本标签；
// 已立案节点金橙光晕 + 法定依据。校验条与筛选在 AuditChainView。
import { computed } from 'vue'
import { isControlledTerminal, statusMetaOf } from '../../domain/clue'
import type { AuditItem } from '../../api/endpoints/audit'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'

const props = defineProps<{
  items: AuditItem[]
  /** 当前会话操作者（其本人记录加「我」标记） */
  selfOperator?: string
}>()

const { config: cfg } = useCaseOntologyConfig()

const ACTION_TEXT: Record<string, string> = {
  disposal: '线索处置',
  proposal: '建议处置',
  parameter_set: '参数调整',
  generic: '操作',
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const rows = computed(() =>
  // 时间线新→旧展示（seq 降序）
  [...props.items].sort((a, b) => b.seq - a.seq),
)

function isFiled(it: AuditItem): boolean {
  // 受控终态（terminal && requires_role=human，默认=已立案）由声明派生
  return isControlledTerminal(it.status_to ?? '', cfg.value)
}
function statusColor(s?: string | null): string {
  return s
    ? statusMetaOf(s, cfg.value).text
    : 'var(--sun-text-tertiary)'
}
</script>

<template>
  <ol class="timeline" v-if="rows.length">
    <li
      v-for="it in rows"
      :key="it.event_id"
      class="tl-node"
      :class="{ 'tl-node--filed': isFiled(it) }"
    >
      <div class="tl-rail">
        <span class="tl-seq" :class="{ 'tl-seq--filed': isFiled(it) }">
          <span v-if="isFiled(it)" class="star">★</span>
          <template v-else>{{ it.seq }}</template>
        </span>
        <span class="tl-line" aria-hidden="true"></span>
      </div>
      <div class="tl-card">
        <div class="tl-top">
          <span class="tl-operator">
            {{ it.operator }}
            <em v-if="selfOperator && it.operator === selfOperator" class="tl-self">（我）</em>
          </span>
          <span class="tl-action-tag">{{ ACTION_TEXT[it.action] ?? it.action }}</span>
          <span v-if="it.chain_source === 'version'" class="tl-source-tag">历史链</span>
          <time class="tl-time">{{ fmtTime(it.occurred_at) }}</time>
        </div>
        <div v-if="it.status_from || it.status_to" class="tl-transition">
          <span :style="{ color: statusColor(it.status_from) }">{{ it.status_from ?? '—' }}</span>
          <span class="tl-arrow">→</span>
          <span :style="{ color: statusColor(it.status_to) }" class="tl-to">{{ it.status_to ?? '—' }}</span>
        </div>
        <p v-if="it.note" class="tl-note">{{ it.note }}</p>
        <p v-if="isFiled(it) && it.legal_basis" class="tl-legal">
          <span class="legal-label">法定依据</span>{{ it.legal_basis }}
        </p>
        <div class="tl-meta">
          <code class="tl-ver">ontology v{{ it.ontology_version }}</code>
          <code v-if="it.rule_version" class="tl-ver">rules v{{ it.rule_version }}</code>
          <code v-if="it.source_row_ids?.length" class="tl-ver">溯源 {{ it.source_row_ids.length }} 行</code>
        </div>
      </div>
    </li>
  </ol>
  <p v-else class="tl-empty">当前筛选条件下无审计记录</p>
</template>

<style scoped>
.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
}
.tl-node {
  display: flex;
  gap: 12px;
}
.tl-rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 30px;
  flex: 0 0 30px;
}
.tl-seq {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: 1px solid var(--sun-info-border);
  color: var(--sun-info-text);
  background: var(--sun-bg-card);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-family: var(--sun-font-mono);
  flex: 0 0 26px;
  z-index: 1;
}
.tl-seq--filed,
.tl-node--filed .tl-seq {
  border-color: var(--sun-filed-border);
  color: var(--sun-filed-text);
  background: var(--sun-filed-bg);
  box-shadow: 0 0 12px rgba(212, 175, 55, 0.55);
}
.star {
  font-size: 13px;
}
.tl-line {
  width: 2px;
  flex: 1;
  background: linear-gradient(var(--sun-border), transparent);
  min-height: 18px;
}
.tl-node:last-child .tl-line {
  display: none;
}
.tl-card {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 10px 14px;
  margin-bottom: 12px;
}
.tl-node--filed .tl-card {
  border-color: var(--sun-filed-border);
  box-shadow: 0 0 18px rgba(212, 175, 55, 0.18);
  background: linear-gradient(180deg, rgba(196, 30, 58, 0.12), var(--sun-bg-card));
}
.tl-top {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.tl-operator {
  font-weight: 600;
  font-size: 13px;
  color: var(--sun-text-primary);
}
.tl-self {
  font-style: normal;
  color: var(--sun-border-active);
  font-size: 12px;
}
.tl-action-tag {
  font-size: 11px;
  color: var(--sun-info-text);
  border: 1px solid var(--sun-info-border);
  background: var(--sun-info-bg);
  border-radius: 10px;
  padding: 0 8px;
}
.tl-source-tag {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  border: 1px dashed var(--sun-text-tertiary);
  border-radius: 10px;
  padding: 0 8px;
}
.tl-time {
  margin-left: auto;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}
.tl-transition {
  margin-top: 8px;
  font-size: 13px;
  font-family: var(--sun-font-mono);
  display: flex;
  align-items: center;
  gap: 8px;
}
.tl-arrow {
  color: var(--sun-text-tertiary);
}
.tl-to {
  font-weight: 700;
}
.tl-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--sun-text-secondary);
  line-height: 1.6;
}
.tl-legal {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--sun-filed-text);
  background: var(--sun-filed-bg);
  border: 1px solid var(--sun-filed-border);
  border-radius: 4px;
  padding: 6px 8px;
}
.legal-label {
  display: inline-block;
  margin-right: 8px;
  font-weight: 700;
}
.tl-meta {
  margin-top: 8px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.tl-ver {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
  border: 1px solid var(--sun-border);
  border-radius: 3px;
  padding: 0 6px;
}
.tl-empty {
  color: var(--sun-text-tertiary);
  font-size: 13px;
  padding: 24px 0;
  text-align: center;
}
</style>
