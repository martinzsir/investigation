<script setup lang="ts">
// RC-203 连线关系选择气泡：拖拽/点选两节点后，当两端存在多种合法人工关系
// 时弹出；只列矩阵白名单关系，备注 ≤200 字。非法连线不进入本组件（父组件
// 直接 toast canConnect.reason）。
import { computed, ref, watch } from 'vue'
import { NButton, NInput } from 'naive-ui'
import {
  LIMITS,
  validateEdgeNote,
  type CanvasNode,
  type ManualRel,
} from '../../domain/canvas'

const props = defineProps<{
  show: boolean
  source: CanvasNode | null
  target: CanvasNode | null
  rels: ManualRel[]
}>()

const emit = defineEmits<{
  (e: 'pick', payload: { rel: ManualRel; note: string }): void
  (e: 'cancel'): void
}>()

const note = ref('')
const noteErr = ref('')

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      note.value = ''
      noteErr.value = ''
    }
  },
)

const relHints: Record<ManualRel, string> = {
  推断为: '该事实/实体支持推出此假设',
  证实: '该待核实项/书证支持假设成立',
  查否: '该待核实项/书证否定该假设',
  补充说明: '备注对该节点的补充说明',
}

const single = computed(() => props.rels.length === 1)

function pick(rel: ManualRel): void {
  const err = validateEdgeNote(note.value)
  if (err) {
    noteErr.value = err
    return
  }
  emit('pick', { rel, note: note.value.trim() })
}
</script>

<template>
  <div
    v-if="show && source && target"
    class="edge-pop"
    data-testid="edge-create-popover"
  >
    <div class="pop-title">
      建立人工关系
    </div>
    <div class="pop-pair dim">
      <span class="ellipsis">{{ source.label }}</span>
      <span class="arrow">→</span>
      <span class="ellipsis">{{ target.label }}</span>
    </div>
    <div class="rel-list">
      <NButton
        v-for="rel in rels"
        :key="rel"
        size="small"
        type="primary"
        ghost
        class="rel-btn"
        :data-testid="`edge-rel-${rel}`"
        @click="pick(rel)"
      >
        {{ rel }}
      </NButton>
    </div>
    <p v-if="!single" class="dim hint">{{ relHints[rels[0]] }}</p>
    <NInput
      v-model:value="note"
      type="textarea"
      :rows="2"
      :maxlength="LIMITS.edgeNote + 30"
      placeholder="连线备注（可选，≤200 字）"
      data-testid="edge-note"
      :status="noteErr ? 'error' : undefined"
      @update:value="noteErr = ''"
    />
    <div class="pop-foot">
      <span v-if="noteErr" class="err" data-testid="edge-note-err">{{ noteErr }}</span>
      <NButton size="tiny" quaternary data-testid="edge-cancel" @click="emit('cancel')">
        取消
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.edge-pop {
  position: absolute;
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 40;
  width: 340px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--sun-border-active, #4a6b82);
  border-radius: 8px;
  background: var(--sun-bg-card, #fff);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
}
.pop-title {
  font-size: 13px;
  font-weight: 600;
}
.pop-pair {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.arrow {
  flex: none;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rel-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.rel-btn {
  font-weight: 600;
}
.hint {
  margin: 0;
}
.pop-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.err {
  font-size: 11px;
  color: #d05656;
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
