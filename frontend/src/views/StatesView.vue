<script setup lang="ts">
// S4-F3 状态机编辑器（states.json，本体管理器壳内挂载）。
// 状态增删 + 合法转出勾选 + 终态🔒保护。
// 🔴 R3：终态不可设置转出目标（UI 硬锁定 + 服务端硬阻止）；
// E3-2：删除被动作引用的状态阻止保存（列出引用动作）；
// 取消终态标记走危险确认 + 理由必填（§8.4）。
import { computed, ref, watch } from 'vue'
import {
  NButton, NCheckbox, NInput, NInputNumber, NSelect, NSpin, NTag,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  governanceApi,
  type StateDecl,
} from '../api/endpoints/governance'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import {
  formatChains,
  normalizeState,
  unterminalChanges,
  validateStates,
} from '../domain/stateEdit'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()

const loading = ref(false)
const loadError = ref('')
const states = ref<StateDecl[]>([])
const beforeSnapshot = ref<StateDecl[]>([])
const transitions = ref<Record<string, string[]>>({})
const referencedBy = ref<Record<string, string[]>>({})
const selectedName = ref('')

const canWrite = computed(() => canWriteConfig(auth.clearance))
const oldNames = computed(() => beforeSnapshot.value.map((s) => s.name))

const TONE_OPTIONS = ['warning', 'info', 'muted', 'success', 'danger'].map((t) => ({ label: t, value: t }))
const ROLE_OPTIONS = [
  { label: '任意角色（any）', value: 'any' },
  { label: '仅 human', value: 'human' },
]

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    states.value = []
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const res = await governanceApi.listStates(cs.currentCaseId)
    states.value = res.states.map((s) => ({ ...s }))
    transitions.value = JSON.parse(JSON.stringify(res.transitions))
    referencedBy.value = res.referenced_by ?? {}
    beforeSnapshot.value = JSON.parse(JSON.stringify(res.states)) as StateDecl[]
    if (!states.value.some((s) => s.name === selectedName.value)) {
      selectedName.value = states.value[0]?.name ?? ''
    }
  } catch (e) {
    states.value = []
    loadError.value = isApiError(e) ? e.message : presentError(e).title
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const selected = computed(
  () => states.value.find((s) => s.name === selectedName.value) ?? null,
)

const chains = computed(() => formatChains(states.value, transitions.value))

const validation = computed(() =>
  validateStates(states.value, transitions.value, oldNames.value, referencedBy.value),
)

function isExisting(name: string): boolean {
  return beforeSnapshot.value.some((s) => s.name === name)
}

function addState(): void {
  const s = normalizeState({ name: '', label: '', terminal: false })
  states.value.push(s)
  selectedName.value = ''
}

function removeState(s: StateDecl): void {
  const i = states.value.findIndex((x) => x === s)
  if (i < 0) return
  // 连带清理迁移表（E3-2 引用动作的删除仍会被 validateStates 阻止）
  delete transitions.value[s.name]
  for (const tos of Object.values(transitions.value)) {
    const j = tos.indexOf(s.name)
    if (j >= 0) tos.splice(j, 1)
  }
  states.value.splice(i, 1)
  if (selectedName.value === s.name) {
    selectedName.value = states.value[Math.max(0, i - 1)]?.name ?? ''
  }
}

/** 转出候选：除自身外的状态（终态由模板硬锁定，不在此勾选） */
function outgoingCandidates(s: StateDecl): StateDecl[] {
  return states.value.filter((x) => x.name && x.name !== s.name)
}
function isOutgoing(s: StateDecl, target: string): boolean {
  return (transitions.value[s.name] ?? []).includes(target)
}
function toggleOutgoing(s: StateDecl, target: string, checked: boolean): void {
  if (s.terminal) return // R3 双保险
  const list = transitions.value[s.name] ?? (transitions.value[s.name] = [])
  const i = list.indexOf(target)
  if (checked && i < 0) list.push(target)
  if (!checked && i >= 0) list.splice(i, 1)
}

// ---- 保存 ----
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
const pendingUnterminal = ref<string[]>([])
const isDangerous = computed(() => pendingUnterminal.value.length > 0)

function askSave(): void {
  const { errors, warnings } = validation.value
  if (errors.length) {
    window.alert(`无法保存（${errors.length} 项阻止性问题）：\n\n${errors.join('\n')}`)
    return
  }
  if (warnings.length) window.alert(`请确认以下警告后继续：\n\n${warnings.join('\n')}`)
  pendingUnterminal.value = unterminalChanges(beforeSnapshot.value, states.value)
  confirmReason.value = ''
  confirmOpen.value = true
}

async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  confirmSaving.value = true
  try {
    const payload = states.value.map((s) => {
      const { _unknown_keys, ...rest } = s
      return rest as StateDecl
    })
    const res = await governanceApi.saveStates(
      cs.currentCaseId,
      payload,
      transitions.value,
      confirmReason.value.trim() || undefined,
    )
    pendingUnterminal.value = res.unterminal
    confirmOpen.value = false
    await load()
  } catch (e) {
    window.alert(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}
</script>

<template>
  <div class="st-page">
    <div class="st-ro-banner">
      线索状态机声明（状态集 + 合法迁移）。🔒 终态一旦进入不可转出（R3，硬阻止）；
      新增状态会被 actions.json 引用，删除被引用状态将被阻止。保存前过整包 loader 校验。
    </div>

    <NSpin v-if="loading" size="medium" class="st-spin" />
    <div v-else-if="loadError" class="st-error">{{ loadError }}</div>

    <template v-else>
      <div class="st-body">
        <!-- 左：状态列表 -->
        <aside class="st-list">
          <div class="st-list-head">
            <span class="dim">状态（{{ states.length }}）</span>
            <NButton v-if="canWrite" size="tiny" dashed @click="addState">+ 新建</NButton>
          </div>
          <button
            v-for="s in states"
            :key="s.name || Math.random()"
            type="button"
            class="st-item"
            :class="{ active: s === selected, invalid: !s.name }"
            @click="selectedName = s.name"
          >
            <span class="st-item-line">
              <span class="st-item-name">{{ s.name || '（未命名）' }}</span>
              <span v-if="s.terminal">🔒</span>
            </span>
            <span class="dim st-item-meta">
              {{ (referencedBy[s.name] ?? []).length ? `${referencedBy[s.name].length} 个动作引用` : '无动作引用' }}
            </span>
          </button>
        </aside>

        <!-- 右：状态详情 -->
        <section v-if="selected" class="st-detail">
          <div class="st-detail-head">
            <input
              v-model="selected.name"
              class="st-name-input"
              :disabled="isExisting(selected.name) || !canWrite"
              placeholder="状态名（如：已起诉）"
            />
            <NTag v-if="isExisting(selected.name)" size="tiny" :bordered="false">🔒 name 不可改</NTag>
            <NButton
              v-if="canWrite"
              size="tiny"
              quaternary
              type="error"
              @click="removeState(selected)"
            >删除状态</NButton>
          </div>

          <div v-if="(referencedBy[selected.name] ?? []).length" class="st-ref">
            被动作引用：<span class="mono">{{ referencedBy[selected.name].join('、') }}</span>
            （删除前须先改这些动作的目标/前置状态）
          </div>

          <div class="st-grid">
            <label class="st-field">
              <span class="st-label">显示名 label</span>
              <NInput v-model:value="selected.label" size="small" :disabled="!canWrite" />
            </label>
            <label class="st-field">
              <span class="st-label">色调 tone</span>
              <NSelect v-model:value="selected.tone" size="small" :options="TONE_OPTIONS" :disabled="!canWrite" />
            </label>
            <label class="st-field">
              <span class="st-label">角色要求 requires_role</span>
              <NSelect v-model:value="selected.requires_role" size="small" :options="ROLE_OPTIONS" :disabled="!canWrite" />
            </label>
            <label class="st-field st-field-inline">
              <NCheckbox
                :checked="!!selected.requires_basis"
                :disabled="!canWrite"
                @update:checked="(v: boolean) => { if (!selected) return; selected.requires_basis = v }"
              >进入需依据 requires_basis</NCheckbox>
            </label>
            <label class="st-field">
              <span class="st-label">SLA（天，可选）</span>
              <NInputNumber
                :value="selected.sla_days ?? null"
                size="small"
                :min="0"
                :show-button="false"
                :disabled="!canWrite"
                placeholder="不限"
                @update:value="(v: number | null) => { if (!selected) return; if (v === null) delete selected.sla_days; else selected.sla_days = v }"
              />
            </label>
            <label class="st-field">
              <span class="st-label">结论映射 outcome（可选）</span>
              <NInput v-model:value="selected.outcome" size="small" :disabled="!canWrite" placeholder="如 verified / excluded" />
            </label>
          </div>

          <!-- 终态标记 -->
          <div class="st-danger-box">
            <NCheckbox
              :checked="!!selected.terminal"
              :disabled="!canWrite"
              @update:checked="(v: boolean) => { if (!selected) return; selected.terminal = v }"
            >
              <span class="st-danger-text">🔒 终态 terminal</span>
            </NCheckbox>
            <span class="dim st-hint">终态一旦进入不可再转出；取消终态标记需危险确认 + 理由（§8.4）</span>
          </div>

          <!-- 合法转出 -->
          <div class="st-section">
            <div class="st-section-title dim">合法转出到</div>
            <div v-if="selected.terminal" class="st-terminal-lock">
              🔒 「{{ selected.name }}」是终态，不可设置转出目标。如需变更，请先取消其终态标记（该操作需危险确认）。
            </div>
            <div v-else class="st-checks">
              <label v-for="t in outgoingCandidates(selected)" :key="t.name" class="st-check">
                <NCheckbox
                  :checked="isOutgoing(selected, t.name)"
                  :disabled="!canWrite"
                  @update:checked="(v: boolean) => { if (!selected) return; toggleOutgoing(selected, t.name, v) }"
                >
                  {{ t.name }}{{ t.terminal ? ' 🔒' : '' }}
                </NCheckbox>
              </label>
              <span v-if="!outgoingCandidates(selected).length" class="dim">没有可选目标状态</span>
            </div>
          </div>

          <div v-if="selected._unknown_keys && selected._unknown_keys.length" class="st-unknown">
            本状态有 {{ selected._unknown_keys.length }} 个界面未覆盖字段
            （<span class="mono">{{ selected._unknown_keys.join('、') }}</span>），保存时原样保留。
          </div>
        </section>
      </div>

      <!-- 文本链总览 -->
      <div class="st-chains card">
        <div class="st-chains-title">状态转移总览</div>
        <div v-for="(line, i) in chains" :key="i" class="st-chain-line mono">{{ line }}</div>
      </div>

      <div v-if="canWrite" class="st-savebar">
        <NButton type="primary" :danger="isDangerous" @click="askSave">保存状态机</NButton>
        <span v-if="validation.errors.length" class="st-save-err">
          有 {{ validation.errors.length }} 项阻止性问题待修复
        </span>
        <span v-else-if="isDangerous" class="st-save-err">含取消终态标记的危险变更，需填写理由</span>
      </div>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="isDangerous"
      :title="isDangerous ? '🔴 危险操作：取消终态保护' : '保存状态机'"
      :detail="isDangerous
        ? `取消终态标记：${pendingUnterminal.join('、')}；取消后这些状态将可被再次转出，已结案状态可能被更改（审计链风险）`
        : 'states.json 状态与迁移表整体替换，保存即对全队生效（不自动触发重跑）'"
      :loading="confirmSaving"
      reason-placeholder="请写明取消终态保护的依据（必填，审计留痕）"
      @confirm="doSave"
    />
  </div>
</template>

<style scoped>
.st-page { display: flex; flex-direction: column; gap: 10px; }
.st-ro-banner {
  background: var(--sun-bg-card-hover); border: 1px dashed var(--sun-border);
  border-radius: 6px; padding: 8px 12px; font-size: 12px; color: var(--sun-text-secondary);
}
.st-spin { display: block; padding: 32px 0; }
.st-error { color: var(--sun-error-text); font-size: 13px; padding: 24px 0; text-align: center; }
.st-body { display: flex; gap: 12px; align-items: flex-start; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
.st-list {
  flex: 0 0 220px; display: flex; flex-direction: column; gap: 4px;
  max-height: calc(100vh - 300px); overflow: auto;
}
.st-list-head { display: flex; align-items: center; justify-content: space-between; padding: 2px 4px 6px; font-size: 12px; }
.st-item {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  width: 100%; text-align: left; border: none; background: transparent;
  border-radius: 6px; padding: 6px 8px; cursor: pointer; font: inherit; color: inherit;
}
.st-item:hover { background: var(--sun-bg-card-hover); }
.st-item.active { background: var(--sun-input-bg); outline: 1px solid var(--sun-border-active); }
.st-item.invalid .st-item-name { color: var(--sun-error-text); }
.st-item-line { display: flex; align-items: center; gap: 6px; }
.st-item-name { font-size: 13px; font-weight: 500; }
.st-item-meta { font-size: 11px; }
.st-detail { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.st-detail-head { display: flex; align-items: center; gap: 10px; }
.st-name-input {
  font-size: 15px; font-weight: 600; padding: 4px 8px; min-width: 200px;
  background: var(--sun-input-bg); border: 1px solid var(--sun-border); border-radius: 4px; color: inherit;
}
.st-name-input:disabled { opacity: 0.7; }
.st-ref {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 6px 10px; font-size: 12px;
}
.st-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.st-field { display: flex; flex-direction: column; gap: 4px; }
.st-field-inline { justify-content: flex-end; padding-bottom: 4px; font-size: 13px; }
.st-label { font-size: 12px; color: var(--sun-text-secondary); }
.st-danger-box {
  border: 1px solid var(--sun-error-border); background: var(--sun-error-bg);
  border-radius: 6px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px;
}
.st-danger-text { color: var(--sun-error-text); font-weight: 600; font-size: 13px; }
.st-hint { font-size: 11px; }
.st-section { border-top: 1px solid var(--sun-border); padding-top: 8px; display: flex; flex-direction: column; gap: 8px; }
.st-section-title { font-size: 12px; font-weight: 600; }
.st-terminal-lock {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.st-checks { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.st-check { font-size: 13px; display: inline-flex; align-items: center; }
.st-unknown {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 6px 10px; font-size: 12px;
}
.card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 10px 12px;
}
.st-chains-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
.st-chain-line { font-size: 12px; line-height: 1.9; color: var(--sun-text-secondary); }
.st-savebar { display: flex; align-items: center; gap: 12px; border-top: 1px solid var(--sun-border); padding-top: 10px; }
.st-save-err { color: var(--sun-error-text); font-size: 12px; }
</style>
