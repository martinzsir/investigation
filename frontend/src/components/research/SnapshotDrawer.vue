<script setup lang="ts">
// RC-206 快照抽屉：创建（立即落库、非防抖、不推进画布版本）、列表、
// 回滚（二次确认；服务端先存「回滚前自动恢复点」再整体替换画布表达层）。
// 纯展示 + 事件上抛，请求与回滚后的 stale 标记在 ResearchCanvas。
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NDrawer,
  NDrawerContent,
  NEmpty,
  NInput,
  NPopconfirm,
  NSpin,
  NTag,
} from 'naive-ui'
import {
  validateSnapshotLabel,
  type CanvasSnapshot,
} from '../../domain/canvas'

const props = defineProps<{
  show: boolean
  snapshots: CanvasSnapshot[]
  loading?: boolean
  creating?: boolean
  /** 正在回滚的快照 id（按钮 loading） */
  rollingBackId?: string | null
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'create', label: string): void
  (e: 'rollback', snapshotId: string): void
}>()

const label = ref('')
const labelErr = ref('')

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      label.value = ''
      labelErr.value = ''
    }
  },
)

const canCreate = computed(() => !props.creating && label.value.trim().length > 0)

function onCreate(): void {
  const err = validateSnapshotLabel(label.value)
  if (err) {
    labelErr.value = err
    return
  }
  emit('create', label.value.trim())
  label.value = ''
  labelErr.value = ''
}

function originText(s: CanvasSnapshot): string {
  if (s.origin === 'report') return '报告生成'
  return '手动'
}
</script>

<template>
  <NDrawer
    :show="show"
    :width="420"
    :z-index="920"
    data-testid="snapshot-drawer"
    @update:show="emit('update:show', $event)"
  >
    <NDrawerContent title="画布快照" closable>
      <div class="snap-body">
        <!-- 创建区 -->
        <div class="create-box">
          <NInput
            v-model:value="label"
            placeholder="快照备注，如：初查假设形成（1-100 字）"
            maxlength="120"
            data-testid="snapshot-label"
            :status="labelErr ? 'error' : undefined"
            @update:value="labelErr = ''"
          />
          <div class="create-foot">
            <span v-if="labelErr" class="err" data-testid="snapshot-label-err">
              {{ labelErr }}
            </span>
            <span v-else class="dim">立即保存当前画布，不会打断自动保存节奏</span>
            <NButton
              size="small"
              type="primary"
              :disabled="!canCreate"
              :loading="creating"
              data-testid="snapshot-create"
              @click="onCreate"
            >
              创建快照
            </NButton>
          </div>
        </div>

        <!-- 列表区 -->
        <div class="list-head">历史快照（{{ snapshots.length }}）</div>
        <NSpin v-if="loading" size="small" class="spin" />
        <NEmpty
          v-else-if="snapshots.length === 0"
          description="尚无快照"
          class="empty"
          data-testid="snapshot-empty"
        />
        <ul v-else class="snap-list">
          <li
            v-for="s in snapshots"
            :key="s.snapshot_id"
            class="snap-item"
            :data-testid="`snapshot-item-${s.snapshot_id}`"
          >
            <div class="snap-main">
              <div class="snap-label" :title="s.label">{{ s.label }}</div>
              <div class="snap-meta dim">
                <NTag size="tiny" :bordered="false">{{ originText(s) }}</NTag>
                <span>{{ s.created_by || '—' }}</span>
                <span>{{ s.created_at }}</span>
              </div>
              <div class="snap-counts dim">
                节点 {{ s.node_count }} · 连线 {{ s.edge_count }}
              </div>
            </div>
            <NPopconfirm
              @positive-click="emit('rollback', s.snapshot_id)"
            >
              <template #trigger>
                <NButton
                  size="tiny"
                  :loading="rollingBackId === s.snapshot_id"
                  :data-testid="`snapshot-rollback-${s.snapshot_id}`"
                >
                  回滚
                </NButton>
              </template>
              回滚到该快照？当前画布将被整体替换（系统会先自动保存一个恢复点，可随时再回滚回来）；业务事实数据不受影响。
            </NPopconfirm>
          </li>
        </ul>
      </div>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.snap-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.create-box {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.create-foot {
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
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.list-head {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.spin {
  align-self: flex-start;
}
.empty {
  padding: 24px 0;
}
.snap-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.snap-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 8px 10px;
  background: var(--sun-bg-card);
}
.snap-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--sun-text-primary);
  word-break: break-all;
}
.snap-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 3px;
}
.snap-counts {
  margin-top: 2px;
}
</style>
