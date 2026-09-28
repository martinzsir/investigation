<script setup lang="ts">
// P2 证据窗口（analysis_result → evidence，PRD 功能 3）。
// 数据：画布内本结论挂出的「支撑/反驳」边与目标假设（P3 镜头回写后
// 产物节点自动挂靶心与假设，本窗口即其读面）；镜头产线信息读 props。
import { computed } from 'vue'
import { KIND_LABELS, type CanvasDoc, type CanvasNode } from '../../../domain/canvas'

const props = defineProps<{
  node: CanvasNode
  doc: CanvasDoc
}>()

const byId = computed(() => new Map(props.doc.nodes.map((n) => [n.id, n])))

interface LinkItem {
  edgeId: string
  rel: string
  hypothesisId: string
  hypothesisLabel: string
}

const links = computed<LinkItem[]>(() =>
  props.doc.edges
    .filter((e) => e.source === props.node.id && (e.rel === '支撑' || e.rel === '反驳'))
    .flatMap((e) => {
      const t = byId.value.get(e.target)
      if (!t || t.kind !== 'hypothesis') return []
      return [{
        edgeId: e.id,
        rel: e.rel,
        hypothesisId: t.id,
        hypothesisLabel: t.label,
      }]
    }),
)

const lensTitle = computed(() => {
  const p = props.node.props ?? {}
  return String(p.lens_title ?? p.lens_name ?? '')
})
const lensId = computed(() => String(props.node.props?.lens_id ?? props.node.ref ?? ''))
const generatedAt = computed(() => String(props.node.props?.generated_at ?? ''))

const hasContent = computed(() => links.value.length > 0 || lensTitle.value || lensId.value)
</script>

<template>
  <div class="evw" data-testid="evidence-window">
    <div v-if="lensTitle || lensId" class="evw-lens" data-testid="evidence-lens">
      <span class="evw-lens-k">来源镜头</span>
      <span class="evw-lens-v">{{ lensTitle || lensId }}</span>
      <span v-if="generatedAt" class="evw-lens-t">{{ generatedAt }}</span>
    </div>

    <template v-if="links.length">
      <h4 class="evw-sec">假设挂接（{{ links.length }}）</h4>
      <ul class="evw-list">
        <li
          v-for="l in links"
          :key="l.edgeId"
          class="evw-item"
          :class="l.rel === '支撑' ? 'evw-sup' : 'evw-ref'"
        >
          <span class="evw-rel">{{ l.rel }}</span>
          <span class="evw-hyp">{{ l.hypothesisLabel }}</span>
        </li>
      </ul>
    </template>

    <div v-if="!hasContent" class="evw-empty" data-testid="evidence-empty">
      暂无三维支撑，先运行镜头
    </div>
    <p v-else-if="!links.length" class="evw-hint">
      该结论尚未挂接假设；在画布上用「支撑/反驳」边连到假设节点即可挂接
    </p>
  </div>
</template>

<style scoped>
.evw { display: flex; flex-direction: column; gap: 8px; }
.evw-lens {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 6px 8px;
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 6px;
  font-size: 12px;
}
.evw-lens-k { flex: none; font-size: 11px; color: var(--sun-text-tertiary, #7c8aa5); }
.evw-lens-v { font-weight: 600; color: var(--sun-text-primary, #0a1b36); }
.evw-lens-t { margin-left: auto; font-size: 10px; color: var(--sun-text-tertiary, #7c8aa5); }
.evw-sec {
  margin: 2px 0 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--sun-text-secondary, #4a5a76);
}
.evw-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.evw-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--sun-border, #e5e9f2);
}
.evw-sup { border-left: 3px solid #35a374; }
.evw-ref { border-left: 3px solid #d03050; }
.evw-rel { flex: none; font-weight: 700; }
.evw-sup .evw-rel { color: #1d7d58; }
.evw-ref .evw-rel { color: #c22550; }
.evw-hyp { color: var(--sun-text-primary, #0a1b36); }
.evw-hint { margin: 0; font-size: 12px; color: var(--sun-text-tertiary, #7c8aa5); }
.evw-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 80px;
  font-size: 12px;
  color: var(--sun-text-tertiary, #7c8aa5);
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 8px;
}
</style>
