<script setup lang="ts">
// 庙算工作台（FE-P-011，/c/miaosuan）：双轨覆盖 + 五间热力 + 候补池。
// FE-T-014 红线二：candidates 是派生只读建议——独立虚线候补区展示，
// 不携带/不回写任何交叉等级升格字段；restricted 内间线索只露 id + 原因。
import { computed, ref, watch } from 'vue'
import { NSpin, NTag, useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { researchApi, type HypothesesDto } from '../api/endpoints/research'
import { useCaseStore } from '../stores/case'
import { presentError, isApiError } from '../api/errors'
import { CANDIDATE_ZONE_CLASS, restrictedList } from '../domain/miaoSuan'
import DualTrackCompare from '../components/research/DualTrackCompare.vue'
import HeatGrid from '../components/research/HeatGrid.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const router = useRouter()
const message = useMessage()

const loading = ref(false)
const data = ref<HypothesesDto | null>(null)

const candidates = computed(() => data.value?.candidates ?? [])
const restricted = computed(() => restrictedList(data.value?.restricted))

async function load(): Promise<void> {
  if (!cs.currentCaseId) {
    data.value = null
    return
  }
  loading.value = true
  try {
    data.value = await researchApi.hypotheses(cs.currentCaseId)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

watch(() => cs.currentCaseId, () => void load(), { immediate: true })

function openClue(clueId: string): void {
  void router.push(`/c/clue/${encodeURIComponent(clueId)}`)
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>庙算工作台</h2>
      <p class="dim hint">
        五间覆盖的双轨核对（分析师声明 vs 数据实证）与交叉等级热力。派生口径：所有建议只读，
        不直接改写线索等级。
      </p>
    </div>

    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="庙算派生按案件计算" />

    <NSpin v-else :show="loading">
      <EmptyState
        v-if="data && !data.available"
        type="empty"
        title="庙算暂不可用"
        desc="案件尚未 BUILD 语义层或无五间标注；完成接入与本体构建后再试"
      />

      <template v-else-if="data">
        <DualTrackCompare :coverage="data.coverage" />
        <HeatGrid :heatmap="data.heatmap" />

        <!-- 候补池：FE-T-014 虚线隔离区（派生建议，不改等级） -->
        <div :class="['card', CANDIDATE_ZONE_CLASS]">
          <div class="card-title">
            候补线索池（派生建议 · 只读）
            <NTag size="tiny" :bordered="false" type="warning">不改变交叉等级</NTag>
          </div>
          <p class="dim zone-note">
            候补为待查线索 top{{ candidates.length || 20 }} 的派生提示，结构上不含升格字段；
            采纳须经人工研判与规则检测，不会自动回写线索等级。
          </p>
          <EmptyState
            v-if="!candidates.length"
            type="empty"
            title="暂无候补线索"
            desc="所有待查线索均已进入正式等级视图"
          />
          <ul v-else class="cand-list">
            <li v-for="c in candidates" :key="c.clue_id" class="cand-item">
              <div class="cand-head">
                <button class="link-btn" @click="openClue(c.clue_id)">{{ c.title }}</button>
                <span class="mono dim cand-id">{{ c.clue_id }}</span>
                <NTag
                  v-for="j in c.jian_types"
                  :key="j"
                  size="tiny"
                  :bordered="false"
                  type="info"
                >{{ j }}</NTag>
                <NTag v-if="c.level" size="tiny" :bordered="false">{{ c.level }}</NTag>
                <span v-if="c.priority_score !== null" class="cand-score mono">
                  优先级 {{ c.priority_score }}
                </span>
              </div>
              <p class="dim cand-reason">{{ c.reason }}</p>
            </li>
          </ul>
        </div>

        <!-- 无权内间线索：灰显，只露 id + 原因（不泄露内容） -->
        <div v-if="restricted.length" class="card restricted">
          <div class="card-title">无权查看的内间线索（{{ restricted.length }}）</div>
          <ul class="restricted-list">
            <li v-for="r in restricted" :key="r.clue_id" class="restricted-item">
              <span class="mono">🔒 {{ r.clue_id }}</span>
              <span class="dim">{{ r.reason }}</span>
            </li>
          </ul>
        </div>
      </template>
    </NSpin>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-head h2 {
  margin: 0;
  font-size: 18px;
}
.hint {
  font-size: 12px;
  margin: 4px 0 0;
}
.card {
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-title {
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
/* FE-T-014 候补隔离区：虚线边框视觉分区 */
.candidate-zone {
  border-style: dashed;
  border-color: var(--sun-warn-border);
}
.zone-note {
  font-size: 12px;
  margin: 0;
}
.cand-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cand-item {
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cand-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cand-id {
  font-size: 11px;
}
.cand-score {
  margin-left: auto;
  font-size: 12px;
  color: var(--sun-warn-text);
}
.cand-reason {
  font-size: 12px;
  margin: 0;
}
.link-btn {
  background: none;
  border: none;
  color: var(--sun-info-text, var(--sun-ok-text));
  cursor: pointer;
  padding: 0;
  font-size: 13px;
  font-weight: 600;
}
.restricted {
  opacity: 0.85;
}
.restricted-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.restricted-item {
  display: flex;
  gap: 10px;
  font-size: 12px;
}
.mono {
  font-family: var(--sun-font-mono);
}
.dim {
  color: var(--sun-text-tertiary);
}
</style>
