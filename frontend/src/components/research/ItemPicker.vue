<script setup lang="ts">
// 案件物品登记（ITM 画布层）：往研判画布上「加物品」。
//
// 三条会咬人的规则（纯逻辑都在 domain/item-picker.ts 里，这里只负责呈现）：
//   R-1 无凭证**且**无特征描述时不给提交——后端会拒，但那是一条 400 通用
//       错，正兵看不出是自己忘了填。空摘要会让所有无凭证赃物合成一个实体。
//   R-2 无凭证允许登记（赃物本来就没编号），但必须标「未锚定，不进消歧」，
//       不能看着像能用。
//   R-3 半截坐标按「都没有」处理：补 0 会把"不知道在哪"画成"在几内亚湾"，
//       而地图天生看上去精确。
//
// 登记成功**不自动关闭**弹窗：正兵要看清系统给的判定（是否已锚定、
// 是否可落地图），自动关闭会让他连"未锚定"提示都来不及看。
import { computed, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NInput,
  NModal,
  NSelect,
  NSpace,
  NSpin,
  NTag,
} from 'naive-ui'
import { canvasApi } from '../../api/endpoints/canvas'
import {
  ITEM_IDENTIFIER_OPTIONS,
  ITEM_TYPES,
  buildItemPayload,
  canSubmitItem,
  itemStatus,
  normalizeCoord,
  sensitiveKinds,
  type ItemDraft,
  type ItemTypeCode,
} from '../../domain/item-picker'

const props = defineProps<{ show: boolean; caseId: string }>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'submit', node: Record<string, unknown>): void
}>()

function blankDraft(): ItemDraft {
  return {
    title: '',
    itemType: 'vehicle' as ItemTypeCode,
    identifiers: [{ kind: 'plate', value: '' }],
    descriptorText: '',
    lat: '',
    lng: '',
    holderRaw: '',
  }
}

const draft = ref<ItemDraft>(blankDraft())
const busy = ref(false)
const err = ref('')
const done = ref<{ credentialed: boolean; unidentified: boolean; mappable: boolean } | null>(null)

const typeOptions = ITEM_TYPES.map((t) => ({ label: t.label, value: t.code }))
const kindOptions = ITEM_IDENTIFIER_OPTIONS.map((o) => ({
  label: o.sensitive ? `${o.label}（敏感·仅存摘要）` : o.label,
  value: o.kind,
}))

watch(
  () => props.show,
  (v) => {
    if (!v) return
    draft.value = blankDraft()
    err.value = ''
    done.value = null
  },
)

const status = computed(() => itemStatus(draft.value))
const submittable = computed(() => canSubmitItem(draft.value) && !busy.value)
const coord = computed(() => normalizeCoord(draft.value.lat, draft.value.lng))
const sensitives = computed(() => sensitiveKinds(draft.value))

function addRow(): void {
  draft.value.identifiers.push({ kind: 'serial', value: '' })
}
function dropRow(i: number): void {
  draft.value.identifiers.splice(i, 1)
  if (!draft.value.identifiers.length) {
    draft.value.identifiers.push({ kind: 'none', value: '' })
  }
}

async function submit(): Promise<void> {
  if (!submittable.value) return
  busy.value = true
  err.value = ''
  done.value = null
  try {
    const res = await canvasApi.buildItem(props.caseId, buildItemPayload(draft.value))
    done.value = {
      credentialed: res.credentialed,
      unidentified: res.unidentified,
      mappable: res.mappable,
    }
    emit('submit', res.node as unknown as Record<string, unknown>)
  } catch (e) {
    err.value = `登记失败：${String((e as Error)?.message ?? e)}`
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <NModal
    :show="show"
    @update:show="(v: boolean) => emit('update:show', v)"
    preset="card"
    title="登记重点物品"
    style="width: 640px"
  >
    <NSpace vertical :size="12">
      <NSpace align="center" :size="8">
        <span style="width: 64px">名称</span>
        <NInput
          v-model:value="draft.title"
          placeholder="如：黑色帕萨特／文三路房产／2024Q1 发票"
          style="flex: 1"
        />
        <NSelect
          v-model:value="draft.itemType"
          :options="typeOptions"
          style="width: 150px"
        />
      </NSpace>

      <NSpace align="center" :size="8">
        <span style="width: 64px">持有人</span>
        <NInput
          v-model:value="draft.holderRaw"
          placeholder="可留空；用于生成持有链起点"
          style="flex: 1"
        />
      </NSpace>

      <div>
        <div style="margin-bottom: 6px">凭证（决定能否参与消歧）</div>
        <NSpace
          v-for="(idf, i) in draft.identifiers"
          :key="i"
          align="center"
          :size="8"
          style="margin-bottom: 6px"
        >
          <NSelect v-model:value="idf.kind" :options="kindOptions" style="width: 200px" />
          <NInput v-model:value="idf.value" placeholder="凭证值" style="flex: 1" />
          <NButton size="tiny" @click="dropRow(i)">移除</NButton>
        </NSpace>
        <NButton size="tiny" @click="addRow">+ 加一项凭证</NButton>
      </div>

      <!-- R-1：无凭证必须靠特征描述区分，否则会被合成一个实体 -->
      <div>
        <div style="margin-bottom: 6px">
          特征描述
          <NTag v-if="status === 'unanchored'" size="tiny" type="warning">必填</NTag>
        </div>
        <NInput
          v-model:value="draft.descriptorText"
          type="textarea"
          :rows="2"
          placeholder="无凭证物品靠特征区分，如：银色 U 盘，内有账本扫描件"
        />
      </div>

      <NSpace align="center" :size="8">
        <span style="width: 64px">坐标</span>
        <NInput v-model:value="draft.lat" placeholder="纬度" style="width: 130px" />
        <NInput v-model:value="draft.lng" placeholder="经度" style="width: 130px" />
        <!-- R-3：只填一半时明确说出来，不静默补 0 -->
        <NTag v-if="coord.partial" size="tiny" type="warning">
          只填了一半，按「无坐标」处理（不补 0）
        </NTag>
      </NSpace>

      <NAlert v-if="sensitives.length" type="warning" :show-icon="false">
        含敏感凭证（{{ sensitives.join('、') }}）：明文不入库，仅存摘要，
        不可用于关联。
      </NAlert>

      <NAlert v-if="status === 'empty'" type="default" :show-icon="false">
        请至少填一项凭证或特征描述——两者皆无时无法区分实体，会被与其他
        无凭证物品静默合并。
      </NAlert>

      <NAlert v-if="status === 'unanchored'" type="warning" :show-icon="false">
        无凭证物品：登记后标「未锚定」，不参与消歧、不进交汇打分。
      </NAlert>

      <NAlert v-if="err" type="error" :show-icon="false">{{ err }}</NAlert>

      <!-- 成功不自动关闭：判定结果要看得见 -->
      <NAlert v-if="done" type="success" :show-icon="false">
        已登记并加入画布。
        {{ done.credentialed ? '已锚定凭证，可参与消歧。' : '未锚定，不进消歧。' }}
        {{ done.mappable ? '可在地图落点。' : '无坐标，不参与地图落点。' }}
      </NAlert>

      <NSpin v-if="busy" size="small" />
    </NSpace>

    <template #footer>
      <NSpace justify="space-between" align="center" style="width: 100%">
        <NTag :type="status === 'ok' ? 'success' : 'warning'" size="small">
          {{ status === 'ok' ? '已锚定' : status === 'unanchored' ? '未锚定' : '信息不足' }}
        </NTag>
        <NSpace>
          <NButton size="small" @click="emit('update:show', false)">
            {{ done ? '关闭' : '取消' }}
          </NButton>
          <NButton
            size="small"
            type="primary"
            :disabled="!submittable"
            :loading="busy"
            @click="submit"
          >
            登记并加入
          </NButton>
        </NSpace>
      </NSpace>
    </template>
  </NModal>
</template>
