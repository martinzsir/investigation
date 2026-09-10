<script setup lang="ts">
// ★ FE-C-014 ClueStatusMachine（承重墙）：线索处置状态机 + 已立案四重门禁。
// 门禁序（不渲染 > 置灰 > 报错）：
//   1 角色：system/ai 等机器会话或秩级不足 → 「已立案」按钮不渲染（非置灰，FE-T-003）
//   2 前置状态：非「已固证」→ 置灰提示「需先完成固证」
//   3 法定依据：legal_basis 空 → 提交禁用
//   4 二次确认：重述不可逆 + 署名「将以 X（角色）名义写入审计链」
// 降级态（FE-T-010）：所有写按钮禁用且写明原因。
import { computed, ref } from 'vue'
import { NModal, NButton, NInput, NIcon } from 'naive-ui'
import { WarningOutline, LockClosedOutline } from '@vicons/ionicons5'
import {
  actionTarget,
  actionTitle,
  allowedActions,
  degradeReason,
  fileGate,
  isControlledTerminal,
  stateDecl,
  CLUE_STATUS,
  type ClueAction,
  type ClueStatus,
} from '../../domain/clue'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'

const props = defineProps<{
  status: ClueStatus | string
  role: string
  operator: string
  degraded: boolean
  loading?: boolean
}>()

const emit = defineEmits<{
  submit: [payload: { action: ClueAction; note?: string; reason?: string; legal_basis?: string }]
}>()

const { config: cfg } = useCaseOntologyConfig()

// 写动作集合由 actions 声明现算（D1：only_from 优先；file 按钮单独走四重门禁渲染）
const actions = computed(() =>
  allowedActions(props.status, cfg.value).filter((a) => a !== 'file'),
)
const gate = computed(() =>
  fileGate(props.role, props.status, legalBasis.value, props.degraded, cfg.value),
)
const degradeMsg = computed(() => degradeReason(props.degraded))
// 受控终态名（默认「已立案」，名称随声明）
const filedStatus = computed(
  () => cfg.value.states.find((s) => s.terminal && s.requires_role === 'human')?.name
    ?? CLUE_STATUS.FILED,
)
const isFiledNow = computed(() => isControlledTerminal(props.status, cfg.value))
const fileLabel = computed(() => actionTitle('file', cfg.value))

// 确认弹窗状态
const modalOpen = ref(false)
const pending = ref<ClueAction | null>(null)
const note = ref('')
const reason = ref('')
const legalBasis = ref('')

const isFile = computed(() => pending.value === 'file')
// 排除类动作：参数声明 required 的非 legal_basis 文本参数（默认 exclude.reason）
const requiredReasonParam = computed(() => {
  if (!pending.value) return null
  const decl = cfg.value.actions.find((a) => a.name === pending.value)
  return decl?.parameters.find((p) => p.required && p.name !== 'legal_basis') ?? null
})
const isExclude = computed(() => pending.value === 'exclude')
const targetStatus = computed(() => (pending.value ? actionTarget(pending.value, cfg.value) : ''))
const pendingTitle = computed(() => (pending.value ? actionTitle(pending.value, cfg.value) : ''))
const statusLabel = computed(() => stateDecl(props.status, cfg.value)?.label ?? props.status)

// 必填理由类参数（默认仅 exclude.reason；声明驱动，泛化到任意同类动作）
const reasonRequired = computed(() => requiredReasonParam.value !== null)
const reasonFieldLabel = computed(() =>
  isExclude.value ? '排除理由（必填）' : `${requiredReasonParam.value?.description ?? '理由'}（必填）`)
const confirmDisabled = computed(() => {
  if (props.loading) return true
  if (reasonRequired.value && !reason.value.trim()) return true
  if (isFile.value) {
    // 门禁 3：法定依据必填
    return !legalBasis.value.trim()
  }
  return false
})

function open(a: ClueAction): void {
  if (props.degraded) return
  pending.value = a
  note.value = ''
  reason.value = ''
  legalBasis.value = ''
  modalOpen.value = true
}

function confirm(): void {
  if (!pending.value || confirmDisabled.value) return
  const a = pending.value
  emit('submit', {
    action: a,
    note: note.value.trim() || undefined,
    reason: reason.value.trim() || undefined,
    legal_basis: isFile.value ? legalBasis.value.trim() : undefined,
  })
  modalOpen.value = false
  pending.value = null
}

function cancel(): void {
  modalOpen.value = false
  pending.value = null
}
</script>

<template>
  <div class="csm">
    <div class="csm-actions">
      <NButton
        v-for="a in actions"
        :key="a"
        size="small"
        :disabled="degraded || loading"
        :class="['csm-btn', `csm-btn--${a}`]"
        @click="open(a)"
      >
        {{ actionTitle(a, cfg) }}
      </NButton>

      <!-- 门禁 1：不通过则按钮不渲染（DOM 中不存在，FE-T-003） -->
      <template v-if="gate.render">
        <NButton
          size="small"
          class="csm-btn csm-btn--file"
          :disabled="!gate.enabled || loading"
          @click="open('file')"
        >
          <NIcon :component="WarningOutline" />
          {{ fileLabel }}（{{ filedStatus }}）
        </NButton>
      </template>

      <span v-if="isFiledNow" class="csm-terminal">
        <NIcon :component="LockClosedOutline" /> 终态：{{ filedStatus }}，不可再迁移
      </span>
    </div>

    <p v-if="degradeMsg" class="csm-degrade" role="alert">
      <NIcon :component="WarningOutline" /> {{ degradeMsg }}
    </p>
    <p
      v-else-if="gate.render && !gate.enabled && !isFiledNow"
      class="csm-hint"
    >
      {{ gate.reason }}
    </p>

    <!-- 写操作统一确认入口（FE-C-005）：门禁 4 二次确认 + 署名 -->
    <NModal
      v-model:show="modalOpen"
      preset="card"
      :title="`处置确认 · ${pendingTitle}`"
      class="csm-modal"
      :mask-closable="false"
    >
      <div class="confirm-body">
        <p class="confirm-transition">
          状态迁移：<b>{{ statusLabel }}</b> → <b class="target">{{ targetStatus }}</b>
        </p>

        <template v-if="isFile">
          <p class="confirm-warn">
            ⚠ {{ filedStatus }}为不可逆终态操作，将以 <b>{{ operator }}（{{ role }}）</b> 名义写入审计链。
          </p>
          <label class="field-label">法定依据 / 案号（必填）</label>
          <NInput v-model:value="legalBasis" placeholder="如：《中华人民共和国刑事诉讼法》第一百零七条 / 案号" />
        </template>

        <template v-else>
          <p class="confirm-note">
            将以 <b>{{ operator }}（{{ role }}）</b> 名义提交，操作进入审计链留痕。
          </p>
          <template v-if="reasonRequired">
            <label class="field-label">{{ reasonFieldLabel }}</label>
            <NInput v-model:value="reason" type="textarea" :rows="2" placeholder="经查证不成立的具体理由" />
          </template>
          <template v-else>
            <label class="field-label">备注（选填）</label>
            <NInput v-model:value="note" type="textarea" :rows="2" placeholder="处置说明" />
          </template>
        </template>
      </div>
      <template #footer>
        <div class="confirm-footer">
          <NButton size="small" @click="cancel">取消</NButton>
          <NButton
            size="small"
            type="primary"
            :disabled="confirmDisabled"
            :loading="loading"
            @click="confirm"
          >
            确认提交
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.csm-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.csm-btn {
  font-weight: 600;
}
.csm-btn--file {
  color: var(--sun-filed-text);
  border-color: var(--sun-filed-border);
  background: var(--sun-filed-bg);
}
.csm-btn--file:hover {
  color: var(--sun-filed-text);
  border-color: var(--sun-gold);
}
.csm-btn--exclude {
  color: var(--sun-text-secondary);
}
.csm-terminal {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sun-filed-text);
}
.csm-degrade {
  margin: 10px 0 0;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  border-radius: 4px;
  padding: 6px 10px;
}
.csm-hint {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--sun-warn-text);
}
.confirm-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.confirm-transition {
  margin: 0;
  font-size: 14px;
  color: var(--sun-text-secondary);
}
.confirm-transition .target {
  color: var(--sun-border-active);
}
.confirm-warn,
.confirm-note {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--sun-text-secondary);
}
.confirm-warn {
  color: var(--sun-filed-text);
  background: var(--sun-filed-bg);
  border: 1px solid var(--sun-filed-border);
  border-radius: 4px;
  padding: 8px 10px;
}
.field-label {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.confirm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
