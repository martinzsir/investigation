<script setup lang="ts">
// S4-F2 Action Type 编辑器（actions.json，本体管理器壳内挂载）。
// 左：动作列表；右：表单（name 既有不可改 / title / target_status / only_from /
// 🔴terminal / 🔴requires_role / parameters / side_effects）。
// 红线：terminal/requires_role 变更走危险确认 + 理由必填（UC-S4-8/9/10/11）；
// R5 悬空状态/不可达前置、枚举越界本地阻止保存；未知字段原样保留 + 提示（R6）。
// 安全门禁在服务端（governance.py），本页危险确认只是 UX，不假装前端可兜底。
import { computed, ref, watch } from 'vue'
import {
  NButton, NCheckbox, NInput, NSelect, NSpin, NTag,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  governanceApi,
  type ActionDecl,
  type GovernanceEnums,
} from '../api/endpoints/governance'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import {
  actionDangerChanges,
  normalizeAction,
  unknownFieldSummary,
  validateActions,
  ROLE_LABEL,
  SIDE_EFFECT_LABEL,
} from '../domain/actionEdit'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()

const loading = ref(false)
const loadError = ref('')
const actions = ref<ActionDecl[]>([])
const beforeSnapshot = ref<ActionDecl[]>([])
const enums = ref<GovernanceEnums>({ requires_role: ['any', 'human'], side_effects: [], derive: [] })
const stateNames = ref<string[]>([])
const stateTerminal = ref<Record<string, boolean>>({})
const transitions = ref<Record<string, string[]>>({})
const selectedName = ref('')

const canWrite = computed(() => canWriteConfig(auth.clearance))
const unknowns = computed(() => unknownFieldSummary(actions.value))

function ensureArrays(a: ActionDecl): void {
  if (!Array.isArray(a.parameters)) a.parameters = []
  if (!Array.isArray(a.side_effects)) a.side_effects = []
}

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    actions.value = []
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const [al, sl] = await Promise.all([
      governanceApi.listActions(cs.currentCaseId),
      governanceApi.listStates(cs.currentCaseId),
    ])
    actions.value = al.actions
    actions.value.forEach(ensureArrays)
    enums.value = al.enums
    stateNames.value = al.state_names
    stateTerminal.value = Object.fromEntries(sl.states.map((s) => [s.name, Boolean(s.terminal)]))
    transitions.value = sl.transitions
    beforeSnapshot.value = JSON.parse(JSON.stringify(al.actions)) as ActionDecl[]
    if (!actions.value.some((a) => a.name === selectedName.value)) {
      selectedName.value = actions.value[0]?.name ?? ''
    }
  } catch (e) {
    actions.value = []
    loadError.value = isApiError(e) ? e.message : presentError(e).title
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const selected = computed(
  () => actions.value.find((a) => a.name === selectedName.value) ?? null,
)

const targetOptions = computed(() => stateNames.value.map((n) => ({
  label: stateTerminal.value[n] ? `${n}（终态）` : n,
  value: n,
})))
const roleOptions = computed(() => enums.value.requires_role.map((r) => ({
  label: ROLE_LABEL[r] ?? r,
  value: r,
})))
const effectOptions = computed(() => enums.value.side_effects.map((fx) => ({
  label: SIDE_EFFECT_LABEL[fx] ?? fx,
  value: fx,
})))

function isExisting(name: string): boolean {
  return beforeSnapshot.value.some((a) => a.name === name)
}

function addAction(): void {
  const a = normalizeAction({
    name: '',
    title: '',
    target_status: stateNames.value[0] ?? '',
    requires_role: 'any',
    terminal: false,
  })
  actions.value.push(a)
  selectedName.value = ''
}

function removeAction(a: ActionDecl): void {
  const i = actions.value.findIndex((x) => x === a)
  if (i >= 0) actions.value.splice(i, 1)
  if (selectedName.value === a.name) {
    selectedName.value = actions.value[Math.max(0, i - 1)]?.name ?? ''
  }
}

function addParam(a: ActionDecl): void {
  a.parameters.push({ name: '', type: 'string', required: false, description: '' })
}
function removeParam(a: ActionDecl, i: number): void {
  a.parameters.splice(i, 1)
}

// ---- only_from 勾选代理（undefined=不收紧，不写成空数组；loader 要求非空数组）----
function onlyFromChecked(a: ActionDecl, state: string): boolean {
  return (a.only_from ?? []).includes(state)
}
function toggleOnlyFrom(a: ActionDecl, state: string, checked: boolean): void {
  const set = new Set(a.only_from ?? [])
  if (checked) set.add(state)
  else set.delete(state)
  if (set.size === 0) a.only_from = undefined
  else a.only_from = stateNames.value.filter((s) => set.has(s))
}

// ---- 保存 ----
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
const pendingDanger = ref<ReturnType<typeof actionDangerChanges>>([])
const validation = computed(() =>
  validateActions(actions.value, {
    stateNames: stateNames.value,
    stateTerminal: stateTerminal.value,
    transitions: transitions.value,
    enums: enums.value,
  }),
)

function dangerDetail(changes: ReturnType<typeof actionDangerChanges>): string {
  return changes.map((c) => {
    const head = `${c.name}（${c.title}）`
    if (c.field === 'requires_role') {
      return `${head} · 角色要求：${c.before} → ${c.after}；将改变该动作的执行资格（终态动作尤其敏感）`
    }
    return `${head} · 终态：${c.before} → ${c.after}；取消终态后其目标状态将可被再次转出`
  }).join('；')
}

function askSave(): void {
  const { errors, warnings } = validation.value
  if (errors.length) {
    window.alert(`无法保存（${errors.length} 项阻止性问题）：\n\n${errors.join('\n')}`)
    return
  }
  if (warnings.length) window.alert(`请确认以下警告后继续：\n\n${warnings.join('\n')}`)
  pendingDanger.value = actionDangerChanges(beforeSnapshot.value, actions.value)
  confirmReason.value = ''
  confirmOpen.value = true
}

async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  confirmSaving.value = true
  try {
    // 剥离 GET 附带的界面提示键，其余键（含 derive/description/未知字段）原样回传
    const payload = actions.value.map((a) => {
      const { _unknown_keys, ...rest } = a
      return rest as ActionDecl
    })
    const res = await governanceApi.saveActions(
      cs.currentCaseId,
      payload,
      confirmReason.value.trim() || undefined,
    )
    pendingDanger.value = res.danger_changes
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
  <div class="ac-page">
    <div class="ac-ro-banner">
      Action Type 管立案/固证等终态动作。🔴 <span class="mono">terminal</span> /
      <span class="mono">requires_role</span> 是危险字段，改动必须危险确认 + 理由留痕；
      保存前在临时副本过整包 loader 校验，不合法不落盘。
    </div>

    <NSpin v-if="loading" size="medium" class="ac-spin" />
    <div v-else-if="loadError" class="ac-error">{{ loadError }}</div>
    <div v-else-if="!actions.length && canWrite" class="ac-empty">
      当前案件包没有动作声明。
      <NButton size="small" type="primary" @click="addAction">+ 新建动作</NButton>
    </div>
    <div v-else-if="!actions.length" class="ac-empty">当前案件包没有动作声明。</div>

    <div v-else class="ac-body">
      <!-- 左：动作列表 -->
      <aside class="ac-list">
        <div class="ac-list-head">
          <span class="dim">动作（{{ actions.length }}）</span>
          <NButton v-if="canWrite" size="tiny" dashed @click="addAction">+ 新建</NButton>
        </div>
        <button
          v-for="a in actions"
          :key="a.name || Math.random()"
          type="button"
          class="ac-item"
          :class="{ active: a === selected, invalid: !a.name }"
          @click="selectedName = a.name"
        >
          <span class="ac-item-line">
            <span class="mono ac-item-name">{{ a.name || '（未命名）' }}</span>
            <span v-if="a.terminal" title="终态动作">🔴</span>
            <span v-if="a.requires_role === 'human'" title="仅 human">
              <NTag size="tiny" :bordered="false">human</NTag>
            </span>
          </span>
          <span class="dim ac-item-title">{{ a.title }}</span>
        </button>
      </aside>

      <!-- 右：表单 -->
      <section v-if="selected" class="ac-detail">
        <div class="ac-detail-head">
          <input
            v-model="selected.name"
            class="ac-name-input mono"
            :disabled="isExisting(selected.name) || !canWrite"
            placeholder="动作 name（小写+下划线）"
          />
          <NTag v-if="isExisting(selected.name)" size="tiny" :bordered="false">🔒 name 不可改</NTag>
          <NButton
            v-if="canWrite"
            size="tiny"
            quaternary
            type="error"
            @click="removeAction(selected)"
          >删除动作</NButton>
        </div>

        <div class="ac-grid">
          <label class="ac-field">
            <span class="ac-label">标题 title</span>
            <NInput v-model:value="selected.title" size="small" :disabled="!canWrite" placeholder="如：立案" />
          </label>
          <label class="ac-field">
            <span class="ac-label">目标状态 target_status</span>
            <NSelect v-model:value="selected.target_status" size="small" :options="targetOptions" :disabled="!canWrite" />
          </label>
        </div>

        <!-- 前置状态 only_from -->
        <div class="ac-section">
          <div class="ac-section-title dim">
            前置状态 only_from
            <span class="ac-hint">（不勾选 = 不额外收紧，按状态迁移表反推可达来源）</span>
          </div>
          <div class="ac-checks">
            <label v-for="s in stateNames" :key="s" class="ac-check">
              <NCheckbox
                :checked="onlyFromChecked(selected, s)"
                :disabled="!canWrite"
                @update:checked="(v: boolean) => toggleOnlyFrom(selected, s, v)"
              >{{ s }}{{ stateTerminal[s] ? ' 🔒' : '' }}</NCheckbox>
            </label>
          </div>
        </div>

        <!-- 危险字段 -->
        <div class="ac-danger-box">
          <label class="ac-danger-field">
            <NCheckbox
              :checked="selected.terminal"
              :disabled="!canWrite"
              @update:checked="(v: boolean) => { selected.terminal = v }"
            >
              <span class="ac-danger-text">🔴 终态动作 terminal</span>
            </NCheckbox>
            <span class="dim ac-hint">勾选后该动作进入的状态不可再转出</span>
          </label>
          <label class="ac-danger-field">
            <span class="ac-danger-text">🔴 角色要求 requires_role</span>
            <NSelect
              v-model:value="selected.requires_role"
              size="small"
              :options="roleOptions"
              :disabled="!canWrite"
              style="max-width: 320px"
            />
          </label>
        </div>

        <!-- 参数 -->
        <div class="ac-section">
          <div class="ac-section-title dim">
            参数 parameters（{{ selected.parameters.length }}）
            <NButton v-if="canWrite" size="tiny" quaternary @click="addParam(selected)">+ 添加参数</NButton>
          </div>
          <table v-if="selected.parameters.length" class="ac-params">
            <thead>
              <tr><th>名称</th><th>类型</th><th>必填</th><th>说明</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="(p, i) in selected.parameters" :key="i">
                <td><NInput v-model:value="p.name" size="tiny" :disabled="!canWrite" placeholder="legal_basis" /></td>
                <td><NInput v-model:value="p.type" size="tiny" :disabled="!canWrite" placeholder="string" style="width:110px" /></td>
                <td><NCheckbox :checked="!!p.required" :disabled="!canWrite" @update:checked="(v: boolean) => { p.required = v }" /></td>
                <td><NInput v-model:value="p.description" size="tiny" :disabled="!canWrite" placeholder="参数说明" /></td>
                <td><NButton v-if="canWrite" size="tiny" quaternary type="error" @click="removeParam(selected, i)">删</NButton></td>
              </tr>
            </tbody>
          </table>
          <span v-else class="dim">（无参数）</span>
        </div>

        <!-- 副作用 -->
        <div class="ac-section">
          <div class="ac-section-title dim">副作用 side_effects</div>
          <div class="ac-checks">
            <label v-for="fx in enums.side_effects" :key="fx" class="ac-check">
              <NCheckbox
                :checked="selected.side_effects.includes(fx)"
                :disabled="!canWrite"
                @update:checked="(v: boolean) => {
                  const set = new Set(selected.side_effects)
                  if (v) set.add(fx); else set.delete(fx)
                  selected.side_effects = enums.side_effects.filter((x) => set.has(x))
                }"
              >
                <span class="mono">{{ fx }}</span>
                <span class="dim">（{{ SIDE_EFFECT_LABEL[fx] ?? fx }}）</span>
              </NCheckbox>
            </label>
          </div>
        </div>

        <label class="ac-field">
          <span class="ac-label">描述 description</span>
          <NInput v-model:value="selected.description" type="textarea" :rows="2" size="small" :disabled="!canWrite" />
        </label>

        <div v-if="selected.derive" class="ac-derive dim">
          可达性推导 derive：<span class="mono">{{ selected.derive }}</span>（系统规则，界面不开放编辑）
        </div>

        <!-- R6 未知字段提示 -->
        <div v-if="selected._unknown_keys && selected._unknown_keys.length" class="ac-unknown">
          本动作有 {{ selected._unknown_keys.length }} 个界面未覆盖字段
          （<span class="mono">{{ selected._unknown_keys.join('、') }}</span>），保存时原样保留。
        </div>
      </section>
    </div>

    <div v-if="canWrite && actions.length" class="ac-savebar">
      <NButton type="primary" danger @click="askSave">保存 Action Type（危险变更需理由）</NButton>
      <span v-if="validation.errors.length" class="ac-save-err">
        有 {{ validation.errors.length }} 项阻止性问题待修复
      </span>
      <span v-else-if="unknowns.length" class="dim">
        {{ unknowns.length }} 个动作含未覆盖字段，将原样保留
      </span>
    </div>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="pendingDanger.length > 0"
      :title="pendingDanger.length ? '🔴 危险操作：Action 治理字段变更' : '保存 Action Type'"
      :detail="pendingDanger.length ? dangerDetail(pendingDanger) : 'actions.json 动作声明整体替换，保存即对全队生效（不自动触发重跑）'"
      :loading="confirmSaving"
      reason-placeholder="请写明变更依据与预期效果（终态/角色变更必填，审计留痕）"
      @confirm="doSave"
    />
  </div>
</template>

<style scoped>
.ac-page { display: flex; flex-direction: column; gap: 10px; }
.ac-ro-banner {
  background: var(--sun-warn-bg);
  border: 1px dashed var(--sun-warn-border);
  color: var(--sun-warn-text);
  border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.ac-spin { display: block; padding: 32px 0; }
.ac-error { color: var(--sun-error-text); font-size: 13px; padding: 24px 0; text-align: center; }
.ac-empty {
  color: var(--sun-text-tertiary); font-size: 13px; padding: 24px 0;
  text-align: center; display: flex; flex-direction: column; gap: 10px; align-items: center;
}
.ac-body { display: flex; gap: 12px; align-items: flex-start; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
.ac-list {
  flex: 0 0 230px; display: flex; flex-direction: column; gap: 4px;
  max-height: calc(100vh - 280px); overflow: auto;
}
.ac-list-head { display: flex; align-items: center; justify-content: space-between; padding: 2px 4px 6px; font-size: 12px; }
.ac-item {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  width: 100%; text-align: left; border: none; background: transparent;
  border-radius: 6px; padding: 6px 8px; cursor: pointer; font: inherit; color: inherit;
}
.ac-item:hover { background: var(--sun-bg-card-hover); }
.ac-item.active { background: var(--sun-input-bg); outline: 1px solid var(--sun-border-active); }
.ac-item.invalid .ac-item-name { color: var(--sun-error-text); }
.ac-item-line { display: flex; align-items: center; gap: 6px; }
.ac-item-name { font-size: 13px; font-weight: 500; }
.ac-item-title { font-size: 11px; }
.ac-detail { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.ac-detail-head { display: flex; align-items: center; gap: 10px; }
.ac-name-input {
  font-size: 15px; font-weight: 600; padding: 4px 8px; min-width: 220px;
  background: var(--sun-input-bg); border: 1px solid var(--sun-border); border-radius: 4px; color: inherit;
}
.ac-name-input:disabled { opacity: 0.7; }
.ac-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.ac-field { display: flex; flex-direction: column; gap: 4px; }
.ac-label { font-size: 12px; color: var(--sun-text-secondary); }
.ac-section { border-top: 1px solid var(--sun-border); padding-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.ac-section-title { font-size: 12px; font-weight: 600; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ac-hint { font-weight: 400; font-size: 11px; }
.ac-checks { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.ac-check { font-size: 13px; display: inline-flex; align-items: center; }
.ac-danger-box {
  border: 1px solid var(--sun-error-border); background: var(--sun-error-bg);
  border-radius: 6px; padding: 10px 12px; display: flex; flex-direction: column; gap: 10px;
}
.ac-danger-field { display: flex; flex-direction: column; gap: 6px; }
.ac-danger-text { color: var(--sun-error-text); font-weight: 600; font-size: 13px; }
.ac-params { width: 100%; border-collapse: collapse; font-size: 12px; }
.ac-params th, .ac-params td { border: 1px solid var(--sun-border); padding: 5px 8px; text-align: left; vertical-align: middle; }
.ac-params th { background: var(--sun-bg-card-hover); font-weight: 600; }
.ac-derive { font-size: 12px; }
.ac-unknown {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 6px 10px; font-size: 12px;
}
.ac-savebar { display: flex; align-items: center; gap: 12px; border-top: 1px solid var(--sun-border); padding-top: 10px; }
.ac-save-err { color: var(--sun-error-text); font-size: 12px; }
</style>
