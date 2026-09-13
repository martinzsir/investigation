<script setup lang="ts">
// UX P0 事实明细预览：fact 卡片「N 行 / N 实体」计数胶囊弹层。
// 只呈现 doc 既有标签、组内确定序号与状态标记（RC-104：禁止字段明文）；
// 点单行才打开节点抽屉，字段值经后端遮蔽 DTO 获取。
import { computed } from 'vue'
import { NIcon } from 'naive-ui'
import { CloseOutline } from '@vicons/ionicons5'
import type { CanvasNode } from '../../domain/canvas'
import {
  nodeSubtitle,
  rowBadges,
  type DetailModel,
  type FactDetailGroup,
  type RowBadge,
  type RowOrdinal,
} from '../../domain/canvas-view'

const props = defineProps<{
  fact: CanvasNode
  group: FactDetailGroup | null
  nodeById: CanvasNode[]
  ordinals: ReadonlyMap<string, RowOrdinal>
  position: { x: number; y: number }
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-node', id: string): void
}>()

const nodeMap = computed(() => new Map(props.nodeById.map((n) => [n.id, n])))

interface RowVM {
  id: string
  label: string
  subtitle: string
  badges: RowBadge[]
}

function rowsOf(ids: string[]): RowVM[] {
  const out: RowVM[] = []
  for (const id of ids) {
    const n = nodeMap.value.get(id)
    if (!n) continue
    out.push({
      id,
      label: n.label,
      subtitle: nodeSubtitle(n, { model: EMPTY_MODEL_CTX, ordinals: props.ordinals }),
      badges: rowBadges(n),
    })
  }
  return out
}

const objectRows = computed(() =>
  (props.group?.objects ?? [])
    .map((id) => nodeMap.value.get(id))
    .filter((n): n is CanvasNode => !!n),
)
const dataRows = computed(() => rowsOf(props.group?.rows ?? []))
const fileRows = computed(() =>
  (props.group?.files ?? [])
    .map((id) => nodeMap.value.get(id))
    .filter((n): n is CanvasNode => !!n),
)

const BADGE_META: Record<RowBadge, { text: string; cls: string }> = {
  missing: { text: '缺失', cls: 'tag--danger' },
  unregistered: { text: '未登记', cls: 'tag--warn' },
  'table-summary': { text: '表级汇总', cls: 'tag--info' },
}

// nodeSubtitle 的 fact/rule 分支才需要 model；行/文件分支只读 ordinals/props
const EMPTY_MODEL_CTX: DetailModel = {
  groups: [],
  groupByFact: new Map(),
  producers: new Map(),
  adjacency: new Map(),
}
</script>

<template>
  <div
    class="fpop"
    data-testid="fact-detail-popover"
    :style="{ left: `${position.x}px`, top: `${position.y}px` }"
  >
    <div class="fpop-head">
      <span class="fpop-title" :title="fact.label">{{ fact.label }}</span>
      <button
        type="button"
        class="fpop-close"
        data-testid="fact-detail-close"
        aria-label="关闭"
        @click="emit('close')"
      >
        <NIcon :component="CloseOutline" />
      </button>
    </div>

    <div v-if="objectRows.length" class="block">
      <div class="block-title">实体（{{ objectRows.length }}）</div>
      <button
        v-for="n in objectRows"
        :key="n.id"
        type="button"
        class="item"
        :data-node-id="n.id"
        @click="emit('open-node', n.id)"
      >
        <span class="item-label">{{ n.label }}</span>
        <span v-if="n.props?.type_title" class="item-sub">{{ n.props.type_title }}</span>
      </button>
    </div>

    <div v-if="dataRows.length" class="block">
      <div class="block-title">来源行（{{ dataRows.length }}）</div>
      <button
        v-for="r in dataRows"
        :key="r.id"
        type="button"
        class="item"
        :data-node-id="r.id"
        @click="emit('open-node', r.id)"
      >
        <span class="item-label">{{ r.label }}</span>
        <span class="item-sub">{{ r.subtitle }}</span>
        <span
          v-for="b in r.badges"
          :key="b"
          class="tag"
          :class="BADGE_META[b].cls"
        >{{ BADGE_META[b].text }}</span>
      </button>
    </div>

    <div v-if="fileRows.length" class="block">
      <div class="block-title">数据源（{{ fileRows.length }}）</div>
      <button
        v-for="n in fileRows"
        :key="n.id"
        type="button"
        class="item"
        :data-node-id="n.id"
        @click="emit('open-node', n.id)"
      >
        <span class="item-label">{{ n.label }}</span>
        <span class="item-sub">
          {{ n.props?.registered === false ? '未登记数据源' : '已登记数据源' }}
        </span>
      </button>
    </div>

    <div v-if="!group || group.total === 0" class="empty">
      暂无可预览明细，点击节点 +/− 或抽屉展开后由后端懒加载
    </div>
  </div>
</template>

<style scoped>
.fpop {
  position: absolute;
  z-index: 25;
  width: 272px;
  max-height: 440px;
  overflow-y: auto;
  padding: 10px 12px;
  border: 1px solid var(--sun-border-active);
  border-radius: 6px;
  background: rgba(5, 21, 34, 0.97);
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
}
.fpop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.fpop-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fpop-close {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--sun-text-tertiary);
  cursor: pointer;
  padding: 2px;
  display: inline-flex;
}
.fpop-close:hover {
  color: var(--sun-text-primary);
}
.block + .block {
  margin-top: 8px;
}
.block-title {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}
.item {
  appearance: none;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  margin-bottom: 3px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: var(--sun-bg-card);
  cursor: pointer;
  text-align: left;
}
.item:hover {
  border-color: var(--sun-border-active);
}
.item-label {
  font-size: 12px;
  color: var(--sun-text-primary);
  white-space: nowrap;
}
.item-sub {
  font-size: 10px;
  color: var(--sun-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}
.tag {
  flex: none;
  font-size: 9px;
  line-height: 1;
  padding: 2px 5px;
  border-radius: 8px;
  border: 1px solid;
}
.tag--danger {
  color: var(--sun-error-text);
  border-color: var(--sun-error-border);
  background: var(--sun-error-bg);
}
.tag--warn {
  color: var(--sun-warn-text);
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
}
.tag--info {
  color: var(--sun-info-text);
  border-color: var(--sun-info-border);
  background: var(--sun-info-bg);
}
.empty {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  padding: 6px 0;
}
</style>
