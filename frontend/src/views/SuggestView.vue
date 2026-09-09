<script setup lang="ts">
// FE-P-008 接入建议确认（MVP-3，/c/suggest）。
// 红线：顶部「待核实草案，非生效声明」；采纳/驳回只记裁决 + 审计，永不自动改写 bindings。
// 无建议时说明「数据质量良好」；未选择裁决时确认按钮禁用。
import { computed, ref, watch } from 'vue'
import { NSpin, NButton, NRadioGroup, NRadio, NInput, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { recommendationsApi } from '../api/endpoints/recommendations'
import { presentError, isApiError } from '../api/errors'
import {
  DE_DRAFT_NOTICE, deRecoStatus, DE_STATUS_META, isDecidable,
  hasRecommendations, deConfidenceTone, type DeReco, type DeDecision,
} from '../domain/recommend'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const message = useMessage()

const loading = ref(false)
const recos = ref<DeReco[]>([])
/** 每单裁决选择：rid → adopt|reject */
const choice = ref<Record<string, DeDecision>>({})
/** 每单备注 */
const note = ref<Record<string, string>>({})
const busyId = ref('')

async function load(): Promise<void> {
  if (!cs.currentCaseId) { recos.value = []; return }
  loading.value = true
  try {
    const res = await recommendationsApi.list(cs.currentCaseId)
    recos.value = res.items ?? []
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, load, { immediate: true })

/** 是否有任意待核实建议（区分空态） */
const hasAny = computed(() => recos.value.length > 0)
/** 是否有含推荐项的单 */
const hasAnyReco = computed(() => recos.value.some((r) => hasRecommendations(r)))

async function decide(r: DeReco): Promise<void> {
  if (!cs.currentCaseId) return
  const decision = choice.value[r.rid]
  if (!decision) return
  busyId.value = r.rid
  try {
    await recommendationsApi.decide(cs.currentCaseId, r.rid, decision, note.value[r.rid] ?? '')
    message.success(decision === 'adopt' ? '已记录采纳裁决（不会自动改写配置）' : '已记录驳回裁决')
    await load()
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    busyId.value = ''
  }
}

function toneClass(tone: string): string {
  return `conf-${tone}`
}
</script>

<template>
  <div class="page">
    <div class="page-head"><h2>接入建议确认</h2></div>

    <EmptyState
      v-if="!cs.currentCaseId"
      type="empty"
      title="请先选择案件"
      desc="数据元建议按案件归属，请先选择案件"
    />

    <template v-else>
      <!-- 红线：待核实草案，非生效声明 -->
      <div class="draft-bar">⚠ {{ DE_DRAFT_NOTICE }}</div>

      <NSpin :show="loading">
        <EmptyState
          v-if="!loading && !hasAny"
          type="empty"
          title="暂无接入建议"
          desc="完成数据导入并生成数据元推荐后，待核实草案将出现在此处"
        />
        <EmptyState
          v-else-if="!loading && !hasAnyReco"
          type="empty"
          title="数据质量良好"
          desc="已生成建议但未发现需要人工确认的数据元映射，无需裁决"
        />

        <div v-else class="reco-list">
          <div v-for="r in recos" :key="r.rid" class="reco-card">
            <div class="reco-head">
              <span class="reco-file">{{ r.filename ?? r.upload_id }}</span>
              <span class="reco-status" :class="`st-${DE_STATUS_META[deRecoStatus(r.status)].tone}`">
                {{ DE_STATUS_META[deRecoStatus(r.status)].label }}
              </span>
            </div>
            <div class="reco-meta dim">
              {{ r.created_by || '—' }} · {{ (r.created_at || '').replace('T', ' ').slice(0, 19) }}
              <span v-if="r.decided_by"> · 裁决人 {{ r.decided_by }}</span>
            </div>

            <div v-if="hasRecommendations(r)" class="reco-items">
              <div v-for="(item, i) in r.recommendations" :key="i" class="reco-item">
                <div class="reco-item-main">
                  <span class="mono col-name">{{ item.col }}</span>
                  <span class="arrow">→</span>
                  <span class="de-name">{{ item.element_name ?? item.element_id }}</span>
                  <span class="mono de-id dim">{{ item.element_id }}</span>
                </div>
                <span class="conf-badge" :class="toneClass(deConfidenceTone(item.confidence))">
                  置信度 {{ Math.round(item.confidence * 100) }}%
                </span>
                <div v-if="item.evidence?.match_values?.length" class="reco-evidence dim">
                  样本：{{ item.evidence.match_values.join('、') }}
                </div>
              </div>
            </div>
            <p v-else class="dim hint">该上传件未产生数据元推荐（数据质量良好）。</p>

            <div v-if="isDecidable(r) && hasRecommendations(r)" class="reco-decide">
              <NRadioGroup v-model:value="choice[r.rid]">
                <NRadio value="adopt">采纳</NRadio>
                <NRadio value="reject">驳回</NRadio>
              </NRadioGroup>
              <NInput
                v-model:value="note[r.rid]"
                size="small"
                placeholder="裁决备注（可选）"
                class="reco-note"
              />
              <NButton
                type="primary"
                size="small"
                :disabled="!choice[r.rid]"
                :loading="busyId === r.rid"
                @click="decide(r)"
              >
                确认裁决
              </NButton>
            </div>
            <div v-else-if="r.note" class="reco-note-record dim">裁决备注：{{ r.note }}</div>
          </div>
        </div>
      </NSpin>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head h2 { margin: 0; font-size: 18px; }
.draft-bar {
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  color: var(--sun-warn-text); border-radius: 6px;
  padding: 8px 12px; font-size: 12px;
}
.reco-list { display: flex; flex-direction: column; gap: 12px; }
.reco-card {
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 12px 14px;
}
.reco-head { display: flex; align-items: center; gap: 10px; }
.reco-file { font-size: 14px; font-weight: 600; }
.reco-status {
  margin-left: auto; font-size: 11px; padding: 0 8px; border-radius: 10px; border: 1px solid;
}
.st-warn { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.st-ok { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.st-muted { color: var(--sun-text-tertiary); border-color: var(--sun-border); }
.reco-meta { font-size: 11px; margin: 4px 0 10px; }
.reco-items { display: flex; flex-direction: column; gap: 8px; }
.reco-item {
  border: 1px solid var(--sun-border); border-radius: 4px; padding: 8px 10px;
  display: flex; flex-direction: column; gap: 4px;
}
.reco-item-main { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.col-name { color: var(--sun-info-text, var(--sun-ok-text)); }
.arrow { color: var(--sun-text-tertiary); }
.de-name { font-weight: 600; }
.de-id { font-size: 11px; }
.conf-badge {
  align-self: flex-start; font-size: 11px; padding: 0 8px; border-radius: 10px; border: 1px solid;
}
.conf-ok { color: var(--sun-ok-text); border-color: var(--sun-ok-border); background: var(--sun-ok-bg); }
.conf-warn { color: var(--sun-warn-text); border-color: var(--sun-warn-border); background: var(--sun-warn-bg); }
.conf-error { color: var(--sun-error-text); border-color: var(--sun-error-border); background: var(--sun-error-bg); }
.reco-evidence { font-size: 11px; }
.reco-decide {
  display: flex; align-items: center; gap: 12px; margin-top: 12px;
  padding-top: 10px; border-top: 1px dashed var(--sun-border);
}
.reco-note { max-width: 260px; }
.reco-note-record { font-size: 12px; margin-top: 10px; }
.hint { font-size: 12px; }
.mono { font-family: var(--sun-font-mono); }
.dim { color: var(--sun-text-tertiary); }
</style>
