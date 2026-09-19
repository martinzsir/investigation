<script setup lang="ts">
// ★ 案件级镜头启停面板（lenses.json；启停后自动入队 RESCAN）。
// 从 EvidencePanel 迁出为独立组件：设置页（/settings 选案件）与研判画布弹窗共用。
// 挂载/案件切换时自动加载清单；开关只读真值刷新（不乐观更新，失败回读避免漂移）。
import { ref, watch } from 'vue'
import { NButton, NSwitch, useMessage } from 'naive-ui'
import { lensesApi, type LensSpecItem } from '../../api/endpoints/lenses'
import { presentError } from '../../api/errors'

const props = defineProps<{
  /** 案件 ID（空串不加载） */
  caseId: string
}>()

const message = useMessage()

const lenses = ref<LensSpecItem[]>([])
const lensError = ref('')
const lensBusyId = ref('')
const loading = ref(false)

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

async function onLensSwitch(l: LensSpecItem, enabled: boolean): Promise<void> {
  if (lensBusyId.value) return
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
    <ul v-else class="lsp-list">
      <li
        v-for="l in lenses"
        :key="l.skill_id"
        class="lsp-item"
        :data-skill-id="l.skill_id"
      >
        <div class="lsp-main">
          <span class="lsp-name">{{ l.name }}</span>
          <span v-if="l.requires_params" class="lsp-tag">定向</span>
          <span v-if="l.mode === 'draft'" class="lsp-tag lsp-tag--draft">草案</span>
          <code class="mono dim lsp-id">{{ l.skill_id }}</code>
          <span v-if="!l.pack_enabled" class="lsp-tag lsp-tag--off">包级停用</span>
        </div>
        <NSwitch
          size="small"
          :value="l.enabled"
          :loading="lensBusyId === l.skill_id"
          :disabled="!l.pack_enabled || lensBusyId === l.skill_id"
          :data-testid="`lens-switch-${l.skill_id}`"
          @update:value="(v: boolean) => onLensSwitch(l, v)"
        />
      </li>
    </ul>
    <p class="dim lsp-hint">
      启停为本案件级配置：停用后该镜头不再进入本案件批量检测（RESCAN 后生效）；
      定向镜头可从画布工具栏带参运行。
    </p>
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
.lsp-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.lsp-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 6px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  font-size: 12px;
}
.lsp-main {
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
}
.lsp-tag {
  display: inline-block;
  padding: 0 5px;
  font-size: 10px;
  line-height: 15px;
  border: 1px solid var(--sun-border-active);
  border-radius: 8px;
  color: var(--sun-border-active);
}
.lsp-tag--draft {
  border-color: var(--sun-warn-border);
  color: var(--sun-warn-text);
}
.lsp-tag--off {
  border-color: var(--sun-error-border, var(--sun-warn-border));
  color: var(--sun-error-text, var(--sun-warn-text));
}
.lsp-hint {
  margin: 0;
  font-size: 11px;
}
</style>
