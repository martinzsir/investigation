<script setup lang="ts">
// ★ 案件级镜头启停面板（lenses.json；启停后自动入队 RESCAN）。
// 从 EvidencePanel 迁出为独立组件：设置页（/settings 选案件）与研判画布弹窗共用。
// 挂载/案件切换时自动加载清单；开关只读真值刷新（不乐观更新，失败回读避免漂移）。
//
// 本轮补全「启停决策所需说明」（此前只有中文名，用户只能盲开关）：
//   ① 用途说明：pack.json 的 description，缺失时按本体维度/依赖兜底生成；
//   ② 数据依赖：consumes_labels（本体 objects/links 的 title，换本体自动跟随）；
//   ③ 停用后果：produces_dims_labels + RESCAN 说明，停用前二次确认；
//   ④ 标签解释：定向/草案/包级停用 均带悬浮说明（NTooltip）；
//   ⑤ 禁用原因：包级停用时明确"由管理员全局停用，本案件无法开启"；
//   ⑥ 上下文说明：context='settings' 讲配置语义，'canvas' 讲对当前研判的影响。
// 所有中文名一律来自后端按本体 title 解析的 *_labels，前端不硬编码业务词表。
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NModal,
  NSwitch,
  NTag,
  NTooltip,
  useMessage,
} from 'naive-ui'
import {
  lensDesc,
  lensDisableImpact,
  lensesApi,
  type LensSpecItem,
} from '../../api/endpoints/lenses'
import { presentError } from '../../api/errors'

const props = defineProps<{
  /** 案件 ID（空串不加载） */
  caseId: string
  /** 展示上下文：设置页讲配置语义；画布弹窗讲对当前研判的影响 */
  context?: 'settings' | 'canvas'
}>()

const message = useMessage()

const lenses = ref<LensSpecItem[]>([])
const lensError = ref('')
const lensBusyId = ref('')
const loading = ref(false)

/** 待确认的停用目标（启用无需确认：启用只会增加线索，不造成丢失） */
const pending = ref<LensSpecItem | null>(null)

async function refreshLenses(): Promise<void> {
  if (!props.caseId) return
  loading.value = true
  try {
    const r = await lensesApi.list(props.caseId)
    lenses.value = r.lenses ?? []
    lensError.value = ''
  } catch (e) {
    lensError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// 案件切换即刷新（immediate：挂载时按当前 caseId 首载）
watch(
  () => props.caseId,
  () => void refreshLenses(),
  { immediate: true },
)

/** 开关拦截：停用走二次确认，启用直接生效 */
function onToggle(l: LensSpecItem, next: boolean): void {
  if (lensBusyId.value) return
  if (!l.pack_enabled) return // 包级停用：开关已禁用，不响应
  if (next) {
    void applySwitch(l, true)
  } else {
    pending.value = l
  }
}

async function applySwitch(l: LensSpecItem, enabled: boolean): Promise<void> {
  lensBusyId.value = l.skill_id
  try {
    const r = await lensesApi.switch(props.caseId, l.skill_id, { enabled })
    message.success(
      `${l.name} 已${enabled ? '启用' : '停用'}，已入队重扫（任务 ${r.rescan_task?.id}）`,
    )
  } catch (e) {
    message.error(presentError(e).title, { duration: 5000 })
  } finally {
    lensBusyId.value = ''
    await refreshLenses() // 回读生效真值（覆盖失败/幂等分支）
  }
}

function confirmDisable(): void {
  const l = pending.value
  pending.value = null
  if (l) void applySwitch(l, false)
}

/** 依赖中文名（本体 title，缺失回落机器名） */
function depLabels(l: LensSpecItem): string[] {
  return (l.consumes_labels ?? l.consumes_objects ?? []).filter(Boolean)
}

/** 维度中文名（本体 dimensions.json 的 name，缺失回落 code） */
function dimLabels(l: LensSpecItem): string[] {
  return (l.produces_dims_labels ?? l.produces_dims ?? []).filter(Boolean)
}

/** 禁用原因：包级停用 vs 请求进行中 */
function disabledReason(l: LensSpecItem): string {
  if (!l.pack_enabled) return '已由管理员全局停用（包级），本案件无法开启'
  if (lensBusyId.value === l.skill_id) return '正在提交…'
  return ''
}

const enabledCount = computed(
  () => lenses.value.filter((l) => l.enabled && l.pack_enabled).length,
)
</script>

<template>
  <div class="lsp" data-testid="lens-switch-panel">
    <p v-if="lensError" class="lsp-error" role="alert">
      镜头清单加载失败：{{ lensError }}
      <NButton text size="tiny" @click="refreshLenses">重试</NButton>
    </p>
    <p v-else-if="loading && lenses.length === 0" class="dim lsp-hint">
      正在加载镜头清单…
    </p>
    <p v-else-if="lenses.length === 0" class="dim lsp-hint">
      暂无可配置镜头（插件镜头包未挂载）。
    </p>

    <template v-else>
      <p class="dim lsp-summary">
        共 {{ lenses.length }} 个镜头，当前生效 {{ enabledCount }} 个。
      </p>
      <ul class="lsp-list">
        <li
          v-for="l in lenses"
          :key="l.skill_id"
          class="lsp-item"
          :class="{ 'lsp-item--off': !l.enabled }"
          :data-skill-id="l.skill_id"
        >
          <div class="lsp-head">
            <span class="lsp-name">{{ l.name }}</span>
            <NTooltip v-if="l.requires_params" trigger="hover">
              <template #trigger>
                <NTag size="tiny" round :bordered="false" class="lsp-tag">定向</NTag>
              </template>
              批量检测不自动跑，需从画布选中主体后手动带参运行
            </NTooltip>
            <NTooltip v-if="l.mode === 'draft'" trigger="hover">
              <template #trigger>
                <NTag size="tiny" round :bordered="false" type="warning" class="lsp-tag">草案</NTag>
              </template>
              产出不可复现，须人工核验后才成为证据，不直接进批量检测
            </NTooltip>
            <NTooltip v-if="!l.pack_enabled" trigger="hover">
              <template #trigger>
                <NTag size="tiny" round :bordered="false" type="error" class="lsp-tag">包级停用</NTag>
              </template>
              已由管理员全局停用，本案件无法开启
            </NTooltip>
            <code class="mono dim lsp-id">{{ l.skill_id }}</code>
            <NSwitch
              size="small"
              :value="l.enabled"
              :loading="lensBusyId === l.skill_id"
              :disabled="!l.pack_enabled || lensBusyId === l.skill_id"
              :data-testid="`lens-switch-${l.skill_id}`"
              @update:value="(v: boolean) => onToggle(l, v)"
            />
          </div>

          <!-- ① 用途说明 -->
          <p class="lsp-desc" :data-testid="`lens-desc-${l.skill_id}`">{{ lensDesc(l) }}</p>

          <!-- ②③ 产出维度 + 数据依赖 -->
          <p class="dim lsp-meta">
            <span v-if="dimLabels(l).length">
              产出维度：{{ dimLabels(l).join('、') }}
            </span>
            <span v-if="depLabels(l).length">
              · 依赖 {{ depLabels(l).length }} 类数据：{{ depLabels(l).join('、') }}
            </span>
          </p>

          <!-- ⑤ 禁用原因 -->
          <p v-if="disabledReason(l)" class="lsp-why">
            {{ disabledReason(l) }}
          </p>
        </li>
      </ul>
    </template>

    <!-- ⑥ 按上下文给不同说明 -->
    <p class="dim lsp-hint">
      <template v-if="context === 'canvas'">
        启停影响本案件整体研判：停用后该镜头的线索不再生成，新线索需重扫后才会出现在画布上。
      </template>
      <template v-else>
        启停为本案件级配置，不影响其他案件：停用后该镜头不再进入本案件批量检测，保存即入队重扫（RESCAN）后生效；定向镜头可从画布工具栏带参运行。
      </template>
    </p>

    <!-- ④ 停用二次确认 -->
    <NModal
      :show="pending !== null"
      preset="dialog"
      type="warning"
      title="确认停用该镜头？"
      :positive-text="'确认停用'"
      :negative-text="'取消'"
      data-testid="lens-disable-confirm"
      @positive-click="confirmDisable"
      @update:show="(v: boolean) => { if (!v) pending = null }"
    >
      <template v-if="pending">
        <p><strong>{{ pending.name }}</strong>（{{ pending.skill_id }}）</p>
        <p class="dim">{{ lensDesc(pending) }}</p>
        <p class="lsp-impact">{{ lensDisableImpact(pending) }}</p>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.lsp {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.lsp-error {
  margin: 0;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--sun-danger-text, var(--sun-warn-text));
}
.lsp-summary {
  margin: 0;
  font-size: 11px;
}
.lsp-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.lsp-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 8px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  font-size: 12px;
}
.lsp-item--off {
  opacity: 0.62;
}
.lsp-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.lsp-name {
  font-weight: 600;
}
.lsp-id {
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lsp-tag {
  font-size: 10px;
}
.lsp-desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--sun-text-2, inherit);
}
.lsp-meta {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
}
.lsp-why {
  margin: 0;
  font-size: 11px;
  color: var(--sun-warn-text, inherit);
}
.lsp-impact {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--sun-warn-text, inherit);
}
.lsp-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
}
</style>
