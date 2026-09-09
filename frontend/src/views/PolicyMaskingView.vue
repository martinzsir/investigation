<script setup lang="ts">
// 权限与字段遮蔽（MVP-4，/c/masking）。
// 红线九：策略 fail-closed——未声明对象 = 拒绝（斜纹行明示），绝不是允许；
// 属性遮蔽所见即所得（partial 保前3后4，与后端 core/policy.py 同口径）；
// 权限变更改变全队可见面 → 🔴 危险确认 + 理由必填，保存即生效。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NSelect, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { policiesApi, type ObjectPolicy, type PropertyPolicy, type MaskMode, type ViewDef } from '../api/endpoints/policies'
import { modelApi, type ObjectType } from '../api/endpoints/model'
import { presentError, isApiError } from '../api/errors'
import { MATRIX_ROLES, ROLE_RANK, objectCell, canWriteConfig, matrixCoverage, MASK_MODE_LABEL } from '../domain/policyMatrix'
import { previewValue, MASK_SAMPLES, maskHint } from '../domain/maskPreview'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const objectTypes = ref<ObjectType[]>([])
const objectPolicies = ref<ObjectPolicy[]>([])
const linkPolicies = ref<{ link: string; roles: string[]; min_clearance: number }[]>([])
const propertyPolicies = ref<PropertyPolicy[]>([])
const views = ref<ViewDef[]>([])

const canWrite = computed(() => canWriteConfig(auth.clearance))

async function load(): Promise<void> {
  if (!cs.currentCaseId) return
  loading.value = true
  try {
    const [pol, obj, vw] = await Promise.all([
      policiesApi.get(cs.currentCaseId),
      modelApi.listObjects(cs.currentCaseId),
      policiesApi.listViews(cs.currentCaseId),
    ])
    objectPolicies.value = pol.object_policies.map((p) => ({ ...p, roles: [...p.roles] }))
    linkPolicies.value = pol.link_policies.map((p) => ({ ...p, roles: [...p.roles] }))
    propertyPolicies.value = pol.property_policies.map((p) => ({ ...p }))
    objectTypes.value = obj.objects
    views.value = vw.views
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

/** 对象名全集（类型层声明） */
const objectNames = computed(() => objectTypes.value.map((o) => o.name))

/** 未声明对象（fail-closed 拒绝） */
const undeclared = computed(() => matrixCoverage(objectNames.value, objectPolicies.value).undeclared)

function policyOf(name: string): ObjectPolicy | undefined {
  return objectPolicies.value.find((p) => p.object === name)
}

function cellState(name: string, role: string): 'allowed' | 'denied' | 'undeclared' {
  return objectCell(policyOf(name), role)
}

/** 点击矩阵格：切换该角色授权（clearance 门槛由 min_clearance 联动） */
function toggleCell(name: string, role: string): void {
  if (!canWrite.value) return
  let p = policyOf(name)
  if (!p) {
    p = { object: name, roles: [], min_clearance: 0 }
    objectPolicies.value.push(p)
    return
  }
  const i = p.roles.indexOf(role)
  if (i >= 0) p.roles.splice(i, 1)
  else p.roles.push(role)
}

const rankOptions = MATRIX_ROLES.map((r) => ({ label: `${r}（rank ${ROLE_RANK[r]}）`, value: ROLE_RANK[r] }))
const maskOptions = (Object.keys(MASK_MODE_LABEL) as MaskMode[]).map((m) => ({ label: MASK_MODE_LABEL[m], value: m }))
const defaultOptions = [
  { label: '默认允许（deny 名单外可读）', value: 'allow' },
  { label: '默认拒绝（仅 allow_roles 可读）', value: 'deny' },
]

const dirty = ref(false)
function markDirty(): void {
  dirty.value = true
}

/** 新增属性策略行 */
function addPropertyPolicy(): void {
  const first = objectTypes.value[0]
  propertyPolicies.value.push({
    object: first?.name ?? '',
    property: '',
    default: 'allow',
    mask: 'none',
    allow_roles: [],
  })
  markDirty()
}
function removePropertyPolicy(i: number): void {
  propertyPolicies.value.splice(i, 1)
  markDirty()
}

// 保存确认
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
function askSave(): void {
  const bad = propertyPolicies.value.filter((p) => !p.object || !p.property)
  if (bad.length) {
    message.error('属性策略存在未填对象/属性的行，请补全或删除')
    return
  }
  confirmReason.value = ''
  confirmOpen.value = true
}
async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  confirmSaving.value = true
  try {
    await policiesApi.save(
      cs.currentCaseId,
      {
        object_policies: objectPolicies.value,
        link_policies: linkPolicies.value,
        property_policies: propertyPolicies.value,
      },
      confirmReason.value,
    )
    message.success('权限策略已保存并留痕，保存即对全队生效')
    confirmOpen.value = false
    dirty.value = false
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    confirmSaving.value = false
  }
}

</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>权限与字段遮蔽</h2>
      <p class="dim hint">
        对象/链接级策略 + 属性级遮蔽；未声明一律 fail-closed 拒绝（斜纹行）。
        遮蔽预览与后端同口径：partial 保前 3 后 4（如 139****1234）。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="权限策略按案件快照归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 权限变更改变全队可见面，保存即生效并记入审计链；低 clearance 角色看不到本页编辑能力。
      </div>

      <NSpin :show="loading">
        <!-- 未声明警示（红线九） -->
        <div v-if="undeclared.length" class="failclosed-bar">
          ⛔ {{ undeclared.length }} 个对象未声明访问策略，当前按 fail-closed 拒绝一切访问：
          <span class="mono">{{ undeclared.join('、') }}</span>
        </div>

        <!-- 对象权限矩阵 -->
        <div class="card">
          <div class="card-title">对象权限矩阵（格=该角色对该对象的访问判定）</div>
          <div class="grid-wrap">
            <table class="matrix">
              <thead>
                <tr>
                  <th class="obj-col">对象</th>
                  <th v-for="r in MATRIX_ROLES" :key="r" class="role-col">{{ r }}</th>
                  <th class="rank-col">最低 rank</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="name in objectNames" :key="name" :class="{ 'row-undeclared': !policyOf(name) }">
                  <td class="mono obj-col">{{ name }}</td>
                  <td
                    v-for="r in MATRIX_ROLES"
                    :key="r"
                    class="cell"
                    :class="[`cell-${cellState(name, r)}`, { 'state-denied': cellState(name, r) === 'undeclared' }]"
                    :title="cellState(name, r) === 'undeclared' ? '未声明策略：fail-closed 一律拒绝访问' : ''"
                    @click="toggleCell(name, r)"
                  >
                    <span v-if="cellState(name, r) === 'allowed'">✓</span>
                    <span v-else-if="cellState(name, r) === 'denied'">—</span>
                    <span v-else class="undeclared-mark">🔒 拒绝</span>
                  </td>
                  <td class="rank-col">
                    <NSelect
                      :value="policyOf(name)?.min_clearance ?? 0"
                      size="tiny"
                      :options="rankOptions"
                      :disabled="!canWrite"
                      @update:value="(v: number) => {
                        let p = policyOf(name)
                        if (!p) { p = { object: name, roles: [], min_clearance: v }; objectPolicies.push(p) }
                        else p.min_clearance = v
                        markDirty()
                      }"
                    />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="legend">
            <span class="lg lg-allowed">✓ 允许</span>
            <span class="lg lg-denied">— 已声明但不满足（拒绝）</span>
            <span class="lg lg-undeclared">🔒 未声明 = fail-closed 拒绝（斜纹）</span>
            <span v-if="canWrite" class="dim">点击格子可切换授权</span>
            <span v-else class="dim">🔒 需偏将及以上（clearance≥2）才可编辑</span>
          </div>
        </div>

        <!-- 属性遮蔽 -->
        <div class="card">
          <div class="card-title">属性级遮蔽策略</div>
          <div class="grid-wrap">
            <table class="grid">
              <thead>
                <tr><th>对象</th><th>属性</th><th>默认</th><th>遮蔽</th><th>预览（手机号 13901231234）</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="(p, i) in propertyPolicies" :key="i">
                  <td class="mono">{{ p.object }}</td>
                  <td class="mono">{{ p.property }}</td>
                  <td>
                    <NSelect
                      :value="p.default" size="tiny" :options="defaultOptions" :disabled="!canWrite"
                      @update:value="(v: 'allow' | 'deny') => { p.default = v; markDirty() }"
                    />
                  </td>
                  <td>
                    <NSelect
                      :value="p.mask ?? 'none'" size="tiny" :options="maskOptions" :disabled="!canWrite"
                      @update:value="(v: MaskMode) => { p.mask = v; markDirty() }"
                    />
                  </td>
                  <td class="mono preview">{{ previewValue('13901231234', p.mask) }}</td>
                  <td>
                    <NButton size="tiny" quaternary type="error" :disabled="!canWrite" @click="removePropertyPolicy(i)">删除</NButton>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="canWrite" class="add-row">
            <NButton size="small" dashed @click="addPropertyPolicy">+ 添加属性遮蔽策略</NButton>
          </div>
          <div class="preview-samples dim">
            遮蔽效果（{{ maskHint('partial') }}）：
            <span v-for="s in MASK_SAMPLES" :key="s.label" class="sample">
              {{ s.label }}：<span class="mono">{{ previewValue(s.value, 'partial') }}</span>
            </span>
          </div>
        </div>

        <!-- 链接策略（只读） -->
        <div class="card">
          <div class="card-title">链接级策略（只读展示）</div>
          <div class="grid-wrap">
            <table class="grid">
              <thead><tr><th>链接</th><th>授权角色</th><th>最低 rank</th></tr></thead>
              <tbody>
                <tr v-for="l in linkPolicies" :key="l.link">
                  <td class="mono">{{ l.link }}</td>
                  <td>{{ l.roles.join('、') }}</td>
                  <td>{{ l.min_clearance }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 角色视图（只读） -->
        <div class="card">
          <div class="card-title">角色视图（Object Views，按角色投影列子集）</div>
          <div class="grid-wrap">
            <table class="grid">
              <thead><tr><th>视图</th><th>基对象</th><th>暴露属性</th><th>授权角色</th></tr></thead>
              <tbody>
                <tr v-for="v in views" :key="v.name">
                  <td class="mono">{{ v.name }}</td>
                  <td class="mono">{{ v.base_object }}</td>
                  <td class="mono dim">{{ v.properties.join(', ') }}</td>
                  <td>{{ v.roles.filter((r) => r !== 'system').join('、') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="canWrite" class="save-bar">
          <NButton type="primary" danger :disabled="!dirty" @click="askSave">保存权限策略（危险变更）</NButton>
          <span class="dim">保存前请对照矩阵确认无越权可见</span>
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="true"
      detail="policies.json 三数组整体替换（对象/链接/属性策略），保存即对全队生效"
      :loading="confirmSaving"
      title="权限策略变更确认"
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
.failclosed-bar {
  background: var(--sun-error-bg); border: 1px solid var(--sun-error-border);
  color: var(--sun-error-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
}
.card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;
}
.card-title { font-size: 13px; font-weight: 600; }
.grid-wrap { overflow: auto; border: 1px solid var(--sun-border); border-radius: 4px; }
.matrix { width: 100%; border-collapse: collapse; font-size: 13px; }
.matrix th, .matrix td { padding: 6px 10px; border-bottom: 1px solid var(--sun-border); text-align: center; }
.matrix th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; }
.obj-col { text-align: left !important; white-space: nowrap; }
.role-col { width: 72px; }
.rank-col { width: 150px; }
.cell { cursor: pointer; user-select: none; font-weight: 700; }
.cell-allowed { background: var(--sun-ok-bg); color: var(--sun-ok-text); }
.cell-denied { color: var(--sun-text-tertiary); }
.cell-undeclared { background: repeating-linear-gradient(45deg, transparent, transparent 6px, var(--sun-border) 6px, var(--sun-border) 7px); color: var(--sun-error-text); }
.state-denied .undeclared-mark { font-size: 11px; white-space: nowrap; }
.state-denied { cursor: pointer; }
.undeclared-mark { opacity: 0.6; }
.row-undeclared .obj-col { color: var(--sun-error-text); }
.legend { display: flex; gap: 14px; align-items: center; font-size: 12px; flex-wrap: wrap; }
.lg { font-weight: 600; }
.lg-allowed { color: var(--sun-ok-text); }
.lg-denied { color: var(--sun-text-tertiary); }
.lg-undeclared { color: var(--sun-error-text); }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; white-space: nowrap; }
.grid .n-select { width: 100%; min-width: 130px; }
.preview { color: var(--sun-info-text, var(--sun-ok-text)); }
.add-row { display: flex; }
.preview-samples { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12px; }
.sample { white-space: nowrap; }
.save-bar { display: flex; align-items: center; gap: 12px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
