<script setup lang="ts">
// 案件主体选择器（CAN-13 / CAN-14）：往研判画布上「加主体」。
//
// 三条会咬人的规则（都在 domain/subject-picker.ts 里可单测，这里只负责呈现）：
//   R-1 重名不自裁——名录里一个名字对应多个主键时，**不取第一个**，
//       而是列出候选让正兵裁决。取 pks[0] 会把三维证据静默配到另一个人身上。
//   R-2 名录外手打允许建节点（走访听到的人确实可能不在库里），
//       但必须 anchored=false，界面写明「无可用研判」，不能看着像能用。
//   R-3 名录读不出来（语义层未连接）时必须显示原因，
//       不能显示一个空下拉——那会被读成「库里没人」。
//
// 选择器只产出 props（buildSubjectProps），不直接建节点：
// 建节点与建提升边是画布的事，选择器不该知道画布怎么存。
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NEmpty, NInput, NModal, NRadio, NSpace, NSpin, NTag } from 'naive-ui'
import { canvasApi, type SubjectEntry } from '../../api/endpoints/canvas'
import {
  buildSubjectProps,
  canSubmit,
  filterCandidates,
  pickStatus,
  resolveSelection,
  type SubjectSelection,
} from '../../domain/subject-picker'

const props = defineProps<{ show: boolean; caseId: string }>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'submit', payload: { props: Record<string, unknown>; sel: SubjectSelection }): void
}>()

const loading = ref(false)
const loadErr = ref('')
const dirReady = ref(true)
const entries = ref<SubjectEntry[]>([])
const query = ref('')
/** 当前选中条目（点名录项） */
const picked = ref<SubjectEntry | null>(null)
/** 重名时人工指定的主键 */
const requestedPk = ref<string>('')

async function reload(): Promise<void> {
  loading.value = true
  loadErr.value = ''
  try {
    const r = await canvasApi.listSubjects(props.caseId, '', 200)
    dirReady.value = r.semantic_ready !== false
    entries.value = r.subjects ?? []
    if (!dirReady.value) loadErr.value = String(r.reason ?? '主体名录不可用')
  } catch (e) {
    dirReady.value = false
    entries.value = []
    loadErr.value = `主体名录读取失败：${String((e as Error)?.message ?? e)}`
  } finally {
    loading.value = false
  }
}

watch(
  () => props.show,
  (v) => {
    if (!v) return
    query.value = ''
    picked.value = null
    requestedPk.value = ''
    void reload()
  },
)

/** 名录命中（按输入过滤）；空查询给前若干条，避免一开就上百条 */
const matched = computed<SubjectEntry[]>(() => filterCandidates(entries.value, query.value, 50))

/** 手打判定：只有当输入没命中任何名录条目时，才视为「名录外」 */
const typedIsManual = computed<boolean>(() => {
  const q = query.value.trim()
  if (!q) return false
  return !matched.value.some((e) => e.name === q)
})

const selection = computed<SubjectSelection>(() =>
  resolveSelection(picked.value, typedIsManual.value ? query.value : '', requestedPk.value || null),
)

const status = computed(() => pickStatus(selection.value))
const submittable = computed(() => canSubmit(selection.value))

function pick(e: SubjectEntry): void {
  picked.value = e
  // 换人就清掉上一次的裁决结果——否则会用 A 的主键去裁决 B
  requestedPk.value = ''
  query.value = e.name
}

function clearPick(): void {
  picked.value = null
  requestedPk.value = ''
}

function submit(): void {
  if (!submittable.value) return
  const sel = selection.value
  emit('submit', {
    props: buildSubjectProps(sel, (picked.value?.sub_type as never) ?? 'person'),
    sel,
  })
  emit('update:show', false)
}

const statusTag = computed(() => {
  switch (status.value) {
    case 'ok':
      return { type: 'success' as const, text: '已锚定，可发起研判' }
    case 'need_disambiguate':
      return { type: 'warning' as const, text: '同名异人，须人工裁决' }
    case 'unanchored':
      return { type: 'warning' as const, text: '未锚定，无可用研判' }
    default:
      return { type: 'default' as const, text: '未选择' }
  }
})
</script>

<template>
  <NModal
    :show="show"
    @update:show="(v: boolean) => emit('update:show', v)"
    preset="card"
    title="加入研判主体"
    style="width: 620px"
  >
    <NSpace vertical :size="12">
      <!-- R-3：名录不可用时把原因说出来，而不是给一个空下拉 -->
      <NAlert v-if="loadErr" type="warning" :show-icon="false">{{ loadErr }}</NAlert>

      <NInput
        v-model:value="query"
        placeholder="搜索主体姓名／单位名；名录外可手打（建出后标未锚定）"
        clearable
        @update:value="clearPick"
      />

      <NSpin v-if="loading" size="small" />

      <NEmpty v-else-if="!loading && matched.length === 0 && !typedIsManual" description="名录中无匹配主体" />

      <div v-else-if="!loading && matched.length > 0" style="max-height: 240px; overflow: auto">
        <div
          v-for="e in matched"
          :key="e.name"
          class="sp-row"
          :class="{ 'sp-row-on': picked && picked.name === e.name }"
          @click="pick(e)"
        >
          <span class="sp-name">{{ e.name }}</span>
          <NTag v-if="e.relational" size="tiny" type="warning">关系型指代</NTag>
          <NTag v-else-if="e.person_pk_ambiguous" size="tiny" type="warning">同名异人</NTag>
          <NTag v-else-if="e.person_pk" size="tiny" type="success">已锚定</NTag>
          <NTag v-else size="tiny">无主键</NTag>
        </div>
      </div>

      <!-- R-1：重名候选裁决。不列候选就等于逼系统自裁 -->
      <div v-if="status === 'need_disambiguate'">
        <NAlert type="warning" :show-icon="false">
          「{{ selection.name }}」在语义层有多个候选主键，系统不会代为选择——
          选错会让空间、时间、关系三维证据配到另一个人身上。
        </NAlert>
        <NSpace vertical :size="6" style="margin-top: 8px">
          <NRadio
            v-for="pk in (picked?.pk_candidates ?? [])"
            :key="pk"
            :checked="requestedPk === pk"
            :value="pk"
            @update:checked="requestedPk = pk"
          >
            {{ pk }}
          </NRadio>
        </NSpace>
      </div>

      <!-- R-2：未锚定仍然可建，但原因必须写在脸上 -->
      <NAlert v-else-if="status === 'unanchored' && selection.reason" type="warning" :show-icon="false">
        {{ selection.reason }}
      </NAlert>
    </NSpace>

    <template #footer>
      <NSpace justify="space-between" align="center" style="width: 100%">
        <NTag :type="statusTag.type" size="small">{{ statusTag.text }}</NTag>
        <NSpace>
          <NButton size="small" @click="emit('update:show', false)">取消</NButton>
          <NButton size="small" type="primary" :disabled="!submittable" @click="submit">
            {{ status === 'unanchored' ? '仍要加入（标未锚定）' : '加入画布' }}
          </NButton>
        </NSpace>
      </NSpace>
    </template>
  </NModal>
</template>

<style scoped>
.sp-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
}
.sp-row:hover {
  background: rgba(128, 128, 128, 0.12);
}
.sp-row-on {
  background: rgba(24, 160, 88, 0.16);
}
.sp-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
