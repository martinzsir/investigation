<script setup lang="ts">
// 对象模型设计器（MVP-4，/c/designer）。
// objects/links 类型层整包编辑：值类型仅 string/integer/decimal/date/boolean；
// 结构变更 = 改变机器语义 → 🔴 危险确认 + 理由必填；保存前可整包校验（load_pack 不写盘）。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NInput, NTag, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import { modelApi, VALUE_TYPES, type ObjectType, type LinkType } from '../api/endpoints/model'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import EmptyState from '../components/common/EmptyState.vue'
import ConfigConfirmDialog from '../components/config/ConfigConfirmDialog.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const message = useMessage()

const loading = ref(false)
const objects = ref<ObjectType[]>([])
const links = ref<LinkType[]>([])
const pack = ref('default')

type Tab = 'objects' | 'links'
const tab = ref<Tab>('objects')
const editing = ref(false)
const editText = ref('')
const editError = ref('')
const validating = ref(false)

const canWrite = computed(() => canWriteConfig(auth.clearance))

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    objects.value = []
    links.value = []
    return
  }
  loading.value = true
  try {
    const [o, l] = await Promise.all([
      modelApi.listObjects(cs.currentCaseId),
      modelApi.listLinks(cs.currentCaseId),
    ])
    objects.value = o.objects
    links.value = l.links
    pack.value = o.pack
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

function startEdit(): void {
  editText.value = JSON.stringify(tab.value === 'objects' ? objects.value : links.value, null, 2)
  editError.value = ''
  editing.value = true
}
function cancelEdit(): void {
  editing.value = false
  editError.value = ''
}

// 保存确认
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)

function askSave(): void {
  const parsed = parseEdit()
  if (parsed === null) return
  if (tab.value === 'objects') {
    const err = validateObjects(parsed as ObjectType[])
    if (err) {
      editError.value = err
      return
    }
  }
  confirmReason.value = ''
  confirmOpen.value = true
}

function parseEdit(): unknown | null {
  try {
    const v = JSON.parse(editText.value)
    if (!Array.isArray(v)) {
      editError.value = '内容必须是 JSON 数组'
      return null
    }
    return v
  } catch (e) {
    editError.value = `JSON 格式错误：${(e as Error).message}`
    return null
  }
}

function validateObjects(list: ObjectType[]): string {
  for (const o of list) {
    if (!o.name) return '存在缺少 name 的对象'
    if (o.kind !== 'entity' && o.kind !== 'event') return `${o.name}：kind 必须是 entity|event`
    for (const [prop, t] of Object.entries(o.properties ?? {})) {
      if (!(VALUE_TYPES as readonly string[]).includes(t)) {
        return `${o.name}.${prop}：值类型 "${t}" 非法（仅 ${VALUE_TYPES.join('/')}）`
      }
    }
  }
  return ''
}

async function doSave(): Promise<void> {
  if (!cs.currentCaseId) return
  const parsed = parseEdit()
  if (parsed === null) return
  confirmSaving.value = true
  try {
    if (tab.value === 'objects') {
      await modelApi.saveObjects(cs.currentCaseId, parsed as ObjectType[], confirmReason.value)
    } else {
      await modelApi.saveLinks(cs.currentCaseId, parsed as LinkType[], confirmReason.value)
    }
    message.success('模型声明已保存并留痕；如需重跑请在任务中心触发 RESCAN')
    confirmOpen.value = false
    editing.value = false
    await load()
  } catch (e) {
    // load_pack 校验失败：400 错误原文直接展示（不落盘）
    message.error(isApiError(e) ? e.message : presentError(e).title)
    editError.value = isApiError(e) ? e.message : presentError(e).title
  } finally {
    confirmSaving.value = false
  }
}

async function validatePack(): Promise<void> {
  if (!cs.currentCaseId) return
  validating.value = true
  try {
    await modelApi.validate(cs.currentCaseId)
    message.success('整包校验通过（load_pack 合法）')
  } catch (e) {
    message.error(isApiError(e) ? `校验未通过：${e.message}` : presentError(e).title)
  } finally {
    validating.value = false
  }
}

function propCount(o: ObjectType): number {
  return Object.keys(o.properties ?? {}).length
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>对象模型设计器</h2>
      <p class="dim hint">
        类型层声明「对象/关系是什么」（{{ pack }} 包）；值类型仅 {{ VALUE_TYPES.join(' / ') }}。
        结构变更影响全部检测与物化，保存即记入审计链。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="模型声明按案件快照归属" />

    <template v-else>
      <div class="notice-bar">
        ⚠ 类型层只声明结构；数据从哪来、怎么清洗在 ETL 管道（bindings）配置。改结构后需 RESCAN 才生效。
      </div>

      <NSpin :show="loading">
        <div class="toolbar">
          <div class="tabs">
            <button class="tab" :class="{ active: tab === 'objects' }" @click="tab = 'objects'; editing = false">
              对象（{{ objects.length }}）
            </button>
            <button class="tab" :class="{ active: tab === 'links' }" @click="tab = 'links'; editing = false">
              链接（{{ links.length }}）
            </button>
          </div>
          <div class="toolbar-right">
            <NButton size="small" :loading="validating" @click="validatePack">整包校验</NButton>
            <NButton v-if="!editing" type="primary" size="small" :disabled="!canWrite" @click="startEdit">
              {{ canWrite ? '编辑整包 JSON' : '🔒 需偏将及以上' }}
            </NButton>
          </div>
        </div>

        <!-- 对象表 -->
        <div v-if="tab === 'objects' && !editing" class="grid-wrap">
          <table class="grid">
            <thead>
              <tr><th>对象</th><th>标题</th><th>kind</th><th>身份属性</th><th>间类</th><th>属性数</th></tr>
            </thead>
            <tbody>
              <tr v-for="o in objects" :key="o.name">
                <td class="mono">{{ o.name }}</td>
                <td>{{ o.title ?? '' }}</td>
                <td><NTag size="tiny" :bordered="false" :type="o.kind === 'event' ? 'warning' : 'info'">{{ o.kind }}</NTag></td>
                <td class="mono">{{ o.name_property ?? '' }}</td>
                <td>{{ o.jian ?? '' }}</td>
                <td>{{ propCount(o) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 链接表 -->
        <div v-if="tab === 'links' && !editing" class="grid-wrap">
          <table class="grid">
            <thead>
              <tr><th>链接</th><th>标题</th><th>from</th><th>to</th><th>间类</th></tr>
            </thead>
            <tbody>
              <tr v-for="l in links" :key="l.name">
                <td class="mono">{{ l.name }}</td>
                <td>{{ l.title ?? '' }}</td>
                <td class="mono">{{ l.from_obj }}</td>
                <td class="mono">→ {{ l.to_obj }}</td>
                <td>{{ l.jian ?? '' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- JSON 编辑 -->
        <div v-if="editing" class="edit-wrap">
          <NInput v-model:value="editText" type="textarea" :rows="18" class="mono edit-area" />
          <div v-if="editError" class="field-error">{{ editError }}</div>
          <div class="edit-actions">
            <NButton size="small" @click="cancelEdit">取消</NButton>
            <NButton type="primary" danger size="small" @click="askSave">校验并保存（危险变更）</NButton>
          </div>
        </div>
      </NSpin>
    </template>

    <ConfigConfirmDialog
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :dangerous="true"
      :detail="tab === 'objects' ? `objects.json 整包替换（${objects.length} 个对象声明）` : `links.json 整包替换（${links.length} 个链接声明）`"
      :loading="confirmSaving"
      title="模型结构变更确认"
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
.toolbar { display: flex; align-items: center; justify-content: space-between; }
.tabs { display: flex; gap: 4px; }
.tab {
  border: 1px solid var(--sun-border); background: var(--sun-bg-card); color: var(--sun-text-secondary);
  border-radius: 6px 6px 0 0; padding: 6px 14px; font-size: 13px; cursor: pointer;
}
.tab.active { color: var(--sun-text-primary); font-weight: 600; border-bottom-color: var(--sun-bg-card); position: relative; top: 1px; }
.toolbar-right { display: flex; gap: 8px; }
.grid-wrap {
  border: 1px solid var(--sun-border); border-radius: 0 6px 6px 6px;
  background: var(--sun-bg-card); overflow: auto;
}
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--sun-border); }
.grid th { color: var(--sun-text-tertiary); font-weight: 500; font-size: 12px; white-space: nowrap; }
.edit-wrap { display: flex; flex-direction: column; gap: 8px; }
.edit-area { font-size: 12px; }
.field-error { font-size: 12px; color: var(--sun-error-text); }
.edit-actions { display: flex; justify-content: flex-end; gap: 8px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
