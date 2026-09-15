<script setup lang="ts">
// 权限与字段遮蔽（S4-F1 策略编辑器；/c/masking 与本体管理器 policies 双挂载）。
// 三级 tab：对象级（角色×对象矩阵）/ 链接级（可编辑）/ 属性级（对象-属性联动下拉）。
// 红线：策略 fail-closed——未声明对象=拒绝（斜纹行），绝不是允许；
//   属性遮蔽所见即所得（partial 保前3后4，与后端 core/policy.py 同口径）；
//   E1-1 引用悬空只警告不阻止；E1-2 遮蔽枚举由下拉限定；E1-4 清空策略红条警示；
//   R6 未知字段（如 _note）随整对象回传原样保留；
//   权限变更改变全队可见面 → 🔴 危险确认 + 理由必填，保存即生效。
import { computed, ref, watch } from 'vue'
import {
  NButton, NCheckbox, NSelect, NSpin, NTabPane, NTabs, useMessage,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  policiesApi,
  type LinkPolicy,
  type MaskMode,
  type ObjectPolicy,
  type PropertyPolicy,
  type ViewDef,
} from '../api/endpoints/policies'
import { modelApi, type LinkType, type ObjectType } from '../api/endpoints/model'
import { presentError, isApiError } from '../api/errors'
import {
  MATRIX_ROLES, ROLE_RANK, objectCell, canWriteConfig, matrixCoverage,
  MASK_MODE_LABEL,
} from '../domain/policyMatrix'
import { previewValue, MASK_SAMPLES, maskHint } from '../domain/maskPreview'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

type TabKey = 'object' | 'link' | 'property' | 'views'
const activeTab = ref<TabKey>('object')

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const objectTypes = ref<ObjectType[]>([])
const links = ref<LinkType[]>([])
const objectPolicies = ref<ObjectPolicy[]>([])
const linkPolicies = ref<LinkPolicy[]>([])
const propertyPolicies = ref<PropertyPolicy[]>([])
const views = ref<ViewDef[]>([])

const canWrite = computed(() => canWriteConfig(auth.clearance))

async function load(): Promise<void> {
  if (!cs.currentCaseId) return
  loading.value = true
  try {
    const [pol, obj, lnk, vw] = await Promise.all([
      policiesApi.get(cs.currentCaseId),
      modelApi.listObjects(cs.currentCaseId),
      modelApi.listLinks(cs.currentCaseId),
      policiesApi.listViews(cs.currentCaseId),
    ])
    objectPolicies.value = pol.object_policies.map((p) => ({ ...p, roles: [...p.roles] }))
    linkPolicies.value = pol.link_policies.map((p) => ({ ...p, roles: [...p.roles] }))
    propertyPolicies.value = pol.property_policies.map((p) => ({ ...p }))
    objectTypes.value = obj.objects
    links.value = lnk.links
    views.value = vw.views
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

const objectNames = computed(() => objectTypes.value.map((o) => o.name))
const linkNames = computed(() => links.value.map((l) => l.name))
const undeclared = computed(() => matrixCoverage(objectNames.value, objectPolicies.value).undeclared)

// E1-4：策略清空（fail-closed 方向安全 = 全部不可访问，但需明确告知）
const emptyPolicy = computed(() => objectPolicies.value.length === 0)

// E1-1：属性策略引用悬空（警告不阻止——本体先改、策略后补是常见顺序）
const danglingProps = computed(() =>
  propertyPolicies.value
    .map((p) => {
      if (!objectNames.value.includes(p.object)) return `${p.object || '?'}.${p.property || '?'}（对象不存在）`
      const props = objectProperties(p.object)
      if (!props.includes(p.property)) return `${p.object}.${p.property || '?'}（属性不存在）`
      return ''
    })
    .filter(Boolean),
)

// E1-1：链接策略引用悬空（警告不阻止）
const danglingLinks = computed(() =>
  linkPolicies.value
    .filter((p) => p.link && !linkNames.value.includes(p.link))
    .map((p) => p.link),
)

function objectProperties(objName: string): string[] {
  return Object.keys(objectTypes.value.find((o) => o.name === objName)?.properties ?? {})
}

// ---- 对象矩阵 ----
function policyOf(name: string): ObjectPolicy | undefined {
  return objectPolicies.value.find((p) => p.object === name)
}
function cellState(name: string, role: string): 'allowed' | 'denied' | 'undeclared' {
  return objectCell(policyOf(name), role)
}
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
  { label: '默认允许（deny 名单外可读）', value: 'allow' as const },
  { label: '默认拒绝（仅 allow_roles 可读）', value: 'deny' as const },
]
const objectOptions = computed(() => objectNames.value.map((n) => ({ label: n, value: n })))
function propertyOptionsFor(objName: string) {
  return objectProperties(objName).map((p) => ({ label: p, value: p }))
}
function linkOptionsFor(current: string) {
  const opts = linkNames.value.map((n) => ({ label: n, value: n }))
  if (current && !linkNames.value.includes(current)) opts.unshift({ label: `${current}（links.json 中不存在）`, value: current })
  return opts
}

// ---- 链接策略（S4 起可编辑）----
function addLinkPolicy(): void {
  linkPolicies.value.push({ link: linkNames.value[0] ?? '', roles: [...MATRIX_ROLES], min_clearance: 0 })
}
function removeLinkPolicy(i: number): void {
  linkPolicies.value.splice(i, 1)
}
function toggleLinkRole(p: LinkPolicy, role: string, v: boolean): void {
  const set = new Set(p.roles)
  if (v) set.add(role)
  else set.delete(role)
  p.roles = MATRIX_ROLES.filter((r) => set.has(r))
}

// ---- 属性策略 ----
function addPropertyPolicy(): void {
  const first = objectTypes.value[0]
  propertyPolicies.value.push({
    object: first?.name ?? '',
    property: '',
    default: 'allow',
    mask: 'none',
    allow_roles: [],
  })
}
function removePropertyPolicy(i: number): void {
  propertyPolicies.value.splice(i, 1)
}
function toggleAllowRole(p: PropertyPolicy, role: string, v: boolean): void {
  const set = new Set(p.allow_roles ?? [])
  if (v) set.add(role)
  else set.delete(role)
  p.allow_roles = MATRIX_ROLES.filter((r) => set.has(r))
}
/** 切换对象时清空已选属性，避免残留悬空属性 */
function onPropObjectChange(p: PropertyPolicy): void {
  p.property = ''
  p.allow_roles = []
}

// ---- 保存（危险确认）----
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
function askSave(): void {
  const badObj = propertyPolicies.value.filter((p) => !p.object || !p.property)
  if (badObj.length) {
    message.error('属性策略存在未填对象/属性的行，请补全或删除')
    return
  }
  const badLink = linkPolicies.value.filter((p) => !p.link)
  if (badLink.length) {
    message.error('链接策略存在未选链接的行，请补全或删除')
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
        对象 / 链接 / 属性三级策略；未声明一律 fail-closed 拒绝。遮蔽预览与后端同口径：partial 保前 3 后 4（如 139****1234）。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="权限策略按案件快照归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 权限变更改变全队可见面，保存即生效并记入审计链；低 clearance 角色看不到本页编辑能力。
      </div>

      <NSpin :show="loading">
        <NTabs v-model:value="activeTab" type="line" animated class="pm-tabs">
          <!-- 对象级：角色×对象矩阵 -->
          <NTabPane name="object" tab="对象级">
            <div v-if="undeclared.length" class="failclosed-bar">
              ⛔ {{ undeclared.length }} 个对象未声明访问策略，当前按 fail-closed 拒绝一切访问：
              <span class="mono">{{ undeclared.join('、') }}</span>
            </div>
            <div v-if="emptyPolicy" class="failclosed-bar">
              ⛔ 对象策略为空：fail-closed 下<b>所有对象将不可访问</b>（方向是安全的，但请确认这不是误清空）。
            </div>
            <div class="card">
              <div class="card-title">对象权限矩阵（格=该角色对该对象的访问判定；角色下限=最低 rank）</div>
              <div class="grid-wrap">
                <table class="matrix">
                  <thead>
                    <tr>
                      <th class="obj-col">对象</th>
                      <th v-for="r in MATRIX_ROLES" :key="r" class="role-col">{{ r }}</th>
                      <th class="rank-col">角色下限</th>
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
          </NTabPane>

          <!-- 链接级（S4 起可编辑） -->
          <NTabPane name="link" :tab="`链接级${danglingLinks.length ? ' ⚠' : ''}`">
            <div v-if="danglingLinks.length" class="warn-bar">
              ⚠ {{ danglingLinks.length }} 条链接策略引用了 links.json 中不存在的链接（<span class="mono">{{ danglingLinks.join('、') }}</span>）。
              本体先改、策略后补是常见顺序，保存不阻止，但请尽快补齐。
            </div>
            <div class="card">
              <div class="card-title">链接权限策略</div>
              <div class="grid-wrap">
                <table class="grid">
                  <thead>
                    <tr><th>链接</th><th>授权角色</th><th>角色下限</th><th></th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(l, i) in linkPolicies" :key="i">
                      <td class="mono">
                        <NSelect
                          :value="l.link" size="tiny" :options="linkOptionsFor(l.link)"
                          :disabled="!canWrite"
                          @update:value="(v: string) => { l.link = v }"
                        />
                      </td>
                      <td class="role-cells">
                        <label v-for="r in MATRIX_ROLES" :key="r" class="role-chip">
                          <NCheckbox
                            :checked="l.roles.includes(r)"
                            :disabled="!canWrite"
                            @update:checked="(v: boolean) => toggleLinkRole(l, r, v)"
                          >{{ r }}</NCheckbox>
                        </label>
                      </td>
                      <td class="rank-cell">
                        <NSelect
                          :value="l.min_clearance" size="tiny" :options="rankOptions"
                          :disabled="!canWrite"
                          @update:value="(v: number) => { l.min_clearance = v }"
                        />
                      </td>
                      <td><NButton v-if="canWrite" size="tiny" quaternary type="error" @click="removeLinkPolicy(i)">删除</NButton></td>
                    </tr>
                    <tr v-if="!linkPolicies.length"><td colspan="4" class="dim">暂无链接策略</td></tr>
                  </tbody>
                </table>
              </div>
              <div v-if="canWrite" class="add-row">
                <NButton size="small" dashed @click="addLinkPolicy">+ 添加链接策略</NButton>
              </div>
            </div>
          </NTabPane>

          <!-- 属性级 -->
          <NTabPane name="property" :tab="`属性级${danglingProps.length ? ' ⚠' : ''}`">
            <div v-if="danglingProps.length" class="warn-bar">
              ⚠ {{ danglingProps.length }} 条属性策略引用了不存在的对象/属性（<span class="mono">{{ danglingProps.join('、') }}</span>）。
              仅警告、不阻止保存（E1-1：本体先改、策略后补）。
            </div>
            <div class="card">
              <div class="card-title">属性级遮蔽策略</div>
              <div class="grid-wrap">
                <table class="grid">
                  <thead>
                    <tr><th>对象</th><th>属性</th><th>默认</th><th>放行角色</th><th>遮蔽</th><th>预览（13901231234）</th><th></th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(p, i) in propertyPolicies" :key="i">
                      <td>
                        <NSelect
                          :value="p.object" size="tiny" :options="objectOptions"
                          :disabled="!canWrite"
                          @update:value="(v: string) => { p.object = v; onPropObjectChange(p) }"
                        />
                      </td>
                      <td>
                        <NSelect
                          :value="p.property" size="tiny"
                          :options="propertyOptionsFor(p.object)"
                          :disabled="!canWrite || !p.object"
                          filterable
                          @update:value="(v: string) => { p.property = v }"
                        />
                      </td>
                      <td>
                        <NSelect
                          :value="p.default" size="tiny" :options="defaultOptions" :disabled="!canWrite"
                          @update:value="(v: 'allow' | 'deny') => { p.default = v }"
                        />
                      </td>
                      <td class="role-cells">
                        <label v-for="r in MATRIX_ROLES" :key="r" class="role-chip">
                          <NCheckbox
                            :checked="(p.allow_roles ?? []).includes(r)"
                            :disabled="!canWrite"
                            @update:checked="(v: boolean) => toggleAllowRole(p, r, v)"
                          >{{ r }}</NCheckbox>
                        </label>
                      </td>
                      <td>
                        <NSelect
                          :value="p.mask ?? 'none'" size="tiny" :options="maskOptions" :disabled="!canWrite"
                          @update:value="(v: MaskMode) => { p.mask = v }"
                        />
                      </td>
                      <td class="mono preview">{{ previewValue('13901231234', p.mask) }}</td>
                      <td><NButton v-if="canWrite" size="tiny" quaternary type="error" @click="removePropertyPolicy(i)">删除</NButton></td>
                    </tr>
                    <tr v-if="!propertyPolicies.length"><td colspan="7" class="dim">暂无属性遮蔽策略</td></tr>
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
          </NTabPane>

          <!-- Object Views（只读，views.json 挂载时承载） -->
          <NTabPane name="views" tab="角色视图（只读）">
            <div class="card">
              <div class="card-title">角色视图（Object Views，按角色投影列子集；在 views.json 编辑器维护）</div>
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
                    <tr v-if="!views.length"><td colspan="4" class="dim">暂无角色视图</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </NTabPane>
        </NTabs>

        <div v-if="canWrite" class="save-bar">
          <NButton type="primary" danger @click="askSave">保存权限策略（危险变更）</NButton>
          <span class="dim">保存前请对照矩阵确认无越权可见</span>
        </div>

        <!-- F1.3 fail-closed 常驻提示（§8.2） -->
        <div class="failclosed-constant">
          ⚠️ <b>fail-closed</b>：未在此声明的对象/属性一律拒绝访问。这是本系统的安全默认值 —— 漏配的结果是「看不到」，不是「被看到」。
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
.pm-tabs { margin-top: 2px; }
.failclosed-bar {
  background: var(--sun-error-bg); border: 1px solid var(--sun-error-border);
  color: var(--sun-error-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
  margin-bottom: 10px;
}
.warn-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
  margin-bottom: 10px;
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
.rank-col, .rank-cell { width: 150px; }
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
.grid th, .grid td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--sun-border); vertical-align: middle; }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; white-space: nowrap; }
.grid .n-select { width: 100%; min-width: 130px; }
.role-cells { display: flex; flex-wrap: wrap; gap: 2px 10px; }
.role-chip { display: inline-flex; align-items: center; font-size: 12px; white-space: nowrap; }
.preview { color: var(--sun-info-text, var(--sun-ok-text)); white-space: nowrap; }
.add-row { display: flex; }
.preview-samples { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12px; }
.sample { white-space: nowrap; }
.save-bar { display: flex; align-items: center; gap: 12px; }
.failclosed-constant {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px; padding: 8px 12px; font-size: 12px;
  position: sticky; bottom: 0;
}
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
