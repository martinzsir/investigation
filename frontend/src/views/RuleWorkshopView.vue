<script setup lang="ts">
// 规则工坊（MVP-4，/c/rules）。
// 红线一：function/stage/hit_when 等结构字段只读，永不渲染编辑态；
// 红线 FE-T-012：params/enabled 变更 = 机器行为变更 → 🔴 危险确认 + 理由必填 + RESCAN 提示；
// LLM 起草走守卫层（离线默认 503），草稿永不落盘。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NInput, NSwitch, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { rulesApi, type Rule } from '../api/endpoints/rules'
import { presentError, isApiError } from '../api/errors'
import {
  canEditMachineBehavior, ruleTextError, diffRule, triggersRescan,
  isDangerousChange, validateRuleEdit, sanitizeEditBody,
} from '../domain/ruleEdit'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const rules = ref<Rule[]>([])
const catalog = ref<string[]>([])

/** 每条规则的编辑态 */
interface EditState {
  ruleText: string
  paramsText: string
  paramsError: string
  enabled: boolean
  dirty: boolean
}
const edits = ref<Record<string, EditState>>({})
const busyId = ref('')

// 确认对话框
const confirmOpen = ref(false)
const confirmTarget = ref<Rule | null>(null)
const confirmReason = ref('')
const confirmSaving = ref(false)

const canMachine = computed(() => canEditMachineBehavior(auth.clearance))

function syncEdit(r: Rule): void {
  edits.value[r.id] = {
    ruleText: r.rule_text ?? '',
    paramsText: JSON.stringify(r.params ?? {}, null, 2),
    paramsError: '',
    enabled: Boolean(r.enabled),
    dirty: false,
  }
}

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    rules.value = []
    return
  }
  loading.value = true
  try {
    const res = await rulesApi.list(cs.currentCaseId)
    rules.value = res.rules
    catalog.value = res.function_catalog
    res.rules.forEach(syncEdit)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

function markDirty(r: Rule): void {
  const ed = edits.value[r.id]
  if (!ed) return
  ed.dirty =
    ed.ruleText !== (r.rule_text ?? '') ||
    ed.enabled !== Boolean(r.enabled) ||
    ed.paramsText.trim() !== JSON.stringify(r.params ?? {}, null, 2)
}

function buildBody(r: Rule): { body: Parameters<typeof rulesApi.update>[2] | null; error: string } {
  const ed = edits.value[r.id]
  if (!ed) return { body: null, error: '编辑态缺失' }
  const body: Parameters<typeof rulesApi.update>[2] = {}
  if (ed.ruleText !== (r.rule_text ?? '')) body.rule_text = ed.ruleText
  if (ed.enabled !== Boolean(r.enabled)) body.enabled = ed.enabled
  if (ed.paramsText.trim() !== JSON.stringify(r.params ?? {}, null, 2)) {
    try {
      const params = JSON.parse(ed.paramsText)
      if (typeof params !== 'object' || params === null || Array.isArray(params)) {
        return { body: null, error: '参数必须是 JSON 对象（键值对）' }
      }
      body.params = params
    } catch {
      return { body: null, error: '参数 JSON 格式错误' }
    }
  }
  const err = validateRuleEdit(r, body, auth.clearance)
  if (err) return { body: null, error: err }
  return { body, error: '' }
}

function pendingChanged(r: Rule): Array<'rule_text' | 'params' | 'enabled'> {
  const built = buildBody(r)
  if (!built.body) return []
  return diffRule(r, built.body)
}

function askSave(r: Rule): void {
  const { error } = buildBody(r)
  if (error) {
    message.error(error)
    return
  }
  confirmTarget.value = r
  confirmReason.value = ''
  confirmOpen.value = true
}

const confirmDangerous = computed(() =>
  confirmTarget.value ? isDangerousChange(pendingChanged(confirmTarget.value)) : false,
)
const confirmDetail = computed(() => {
  if (!confirmTarget.value) return ''
  const changed = pendingChanged(confirmTarget.value)
  const labels: Record<string, string> = { rule_text: '判据文本', params: '阈值参数', enabled: '启停状态' }
  return `${confirmTarget.value.id} ${confirmTarget.value.title ?? ''}：${changed.map((c) => labels[c]).join('、')}`
})

async function doSave(): Promise<void> {
  const r = confirmTarget.value
  if (!r || !cs.currentCaseId) return
  const { body, error } = buildBody(r)
  if (error || !body) {
    message.error(error || '编辑态缺失')
    return
  }
  const sendBody = sanitizeEditBody({ ...body, reason: confirmReason.value })
  confirmSaving.value = true
  try {
    const res = await rulesApi.update(cs.currentCaseId, r.id, sendBody)
    message.success(
      res.rescan_task
        ? `已保存并留痕；阈值/启停变更已入队 RESCAN 重跑（任务 ${res.rescan_task.id}）`
        : '判据修订已保存并留痕（不触发重跑）',
    )
    confirmOpen.value = false
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}

// LLM 起草（守卫层：离线默认 503）
const draftQuestion = ref('')
const draftBusy = ref(false)
const draftResult = ref('')
async function draft(): Promise<void> {
  if (!cs.currentCaseId || !draftQuestion.value.trim()) return
  draftBusy.value = true
  draftResult.value = ''
  try {
    const res = await rulesApi.draft(cs.currentCaseId, { question: draftQuestion.value })
    draftResult.value = res.rule_text
    message.warning('草稿仅供参考，须人工改写为判据后手动保存（草稿不落盘）')
  } catch (e) {
    if (isApiError(e) && (e.code === 'INTERNAL' || e.httpStatus === 503)) {
      message.warning('当前为离线内核，LLM 通道未启用：请人工撰写判据（模式 + 反常理由 + 边界排除）')
    } else {
      message.error(isApiError(e) ? e.message : presentError(e).title)
    }
  } finally {
    draftBusy.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>规则工坊</h2>
      <p class="dim hint">
        判据文本是给人看的解释口径，机器只执行 <code>function + params</code>；
        改判据/阈值改这里，不改检测器代码。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="规则按案件快照归属，请先选择案件" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 结构字段（函数/阶段/间类/命中条件）只读，由案件包声明；可改的只有判据文本、阈值参数、启停。
      </div>

      <NSpin :show="loading">
        <div v-for="r in rules" :key="r.id" class="rule-card">
          <div class="rule-head">
            <span class="rule-id mono">{{ r.id }}</span>
            <span class="rule-title">{{ r.title }}</span>
            <NTag size="small" :bordered="false">{{ r.function }}</NTag>
            <NTag v-if="r.stage" size="small" :bordered="false" type="info">{{ r.stage }}</NTag>
            <span v-for="j in r.jian_types ?? []" :key="j" class="jian-chip">{{ j }}</span>
            <div class="rule-head-right">
              <span class="dim en-label">{{ r.enabled ? '启用中' : '已停用' }}</span>
              <NSwitch
                :value="edits[r.id]?.enabled"
                :disabled="!canMachine"
                @update:value="(v: boolean) => { if (edits[r.id]) { edits[r.id].enabled = v; markDirty(r) } }"
              />
            </div>
          </div>

          <!-- 上区：判据文本（可编辑，不触发重跑） -->
          <div class="zone zone-text">
            <div class="zone-label">判据文本（自然语言，人工解释口径）</div>
            <NInput
              :value="edits[r.id]?.ruleText"
              type="textarea"
              :rows="4"
              :status="edits[r.id] && ruleTextError(edits[r.id].ruleText) ? 'warning' : undefined"
              @update:value="(v: string) => { if (edits[r.id]) { edits[r.id].ruleText = v; markDirty(r) } }"
            />
            <div v-if="edits[r.id] && ruleTextError(edits[r.id].ruleText)" class="field-warn">
              {{ ruleTextError(edits[r.id].ruleText) }}
            </div>
          </div>

          <!-- 下区：机器行为（偏将及以上可写） -->
          <div class="zone zone-machine" :class="{ 'zone-locked': !canMachine }">
            <div class="zone-label">
              机器挂钩 <code>{{ r.function }}</code> · 参数（params）
              <span v-if="!canMachine" class="lock-tag">🔒 阈值/启停需偏将及以上（clearance≥2），当前只读</span>
            </div>
            <NInput
              :value="edits[r.id]?.paramsText"
              type="textarea"
              :rows="5"
              class="mono params-edit"
              :disabled="!canMachine"
              @update:value="(v: string) => { if (edits[r.id]) { edits[r.id].paramsText = v; markDirty(r) } }"
            />
          </div>

          <div class="rule-foot">
            <NButton
              type="primary"
              size="small"
              :disabled="!edits[r.id]?.dirty"
              :loading="busyId === r.id"
              @click="askSave(r)"
            >
              保存变更
            </NButton>
            <span v-if="triggersRescan(pendingChanged(r))" class="rescan-hint">
              ⚠ 含阈值/启停变更，保存后将入队 RESCAN 重跑
            </span>
          </div>
        </div>

        <!-- 函数目录（只读参考） + LLM 起草 -->
        <div class="catalog-card">
          <div class="zone-label">函数目录（只读参考；params 键名以此为准）</div>
          <div class="catalog-chips">
            <NTag v-for="f in catalog" :key="f" size="small" :bordered="false" class="mono">{{ f }}</NTag>
          </div>
          <div class="draft-row">
            <div class="zone-label">LLM 起草助手（离线内核默认不可用，草稿永不落盘）</div>
            <div class="draft-input">
              <NInput
                v-model:value="draftQuestion"
                size="small"
                placeholder="描述想识别的反常模式，例如：凌晨向境外账户集中转出且金额接近当日报告限额"
              />
              <NButton size="small" :loading="draftBusy" :disabled="!draftQuestion.trim()" @click="draft">
                生成草稿
              </NButton>
            </div>
            <div v-if="draftResult" class="draft-result dim">{{ draftResult }}</div>
          </div>
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="confirmDangerous"
      :detail="confirmDetail"
      :loading="confirmSaving"
      title="规则变更确认"
      @confirm="doSave"
    />
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.hint { font-size: 12px; margin: 4px 0 0; }
.notice-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.rule-card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.rule-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rule-id { font-weight: 700; }
.rule-title { font-size: 14px; font-weight: 600; }
.rule-head-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.en-label { font-size: 11px; }
.jian-chip {
  font-size: 11px; padding: 0 8px; border-radius: 10px;
  border: 1px solid var(--sun-border); color: var(--sun-text-secondary);
}
.zone { display: flex; flex-direction: column; gap: 6px; }
.zone-label { font-size: 12px; color: var(--sun-text-secondary); }
.zone-machine { border-top: 1px dashed var(--sun-border); padding-top: 8px; }
.zone-locked { opacity: 0.75; }
.lock-tag { color: var(--sun-warn-text); margin-left: 8px; }
.params-edit { font-size: 12px; }
.field-warn { font-size: 12px; color: var(--sun-warn-text); }
.rule-foot { display: flex; align-items: center; gap: 12px; }
.rescan-hint { font-size: 12px; color: var(--sun-error-text); }
.catalog-card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.catalog-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.draft-row { display: flex; flex-direction: column; gap: 6px; border-top: 1px dashed var(--sun-border); padding-top: 10px; }
.draft-input { display: flex; gap: 8px; }
.draft-result {
  font-size: 12px; border: 1px dashed var(--sun-border); border-radius: 4px;
  padding: 8px 10px; white-space: pre-wrap;
}
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
