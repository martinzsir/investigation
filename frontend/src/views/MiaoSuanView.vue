<script setup lang="ts">
// 庙算工作台（FE-P-011，/c/miaosuan）：双轨覆盖 + 五间热力 + 候补池。
// FE-T-014 红线二：candidates 是派生只读建议——独立虚线候补区展示，
// 不携带/不回写任何交叉等级升格字段；restricted 内间线索只露 id + 原因。
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'
import {
  researchApi,
  verdictTone,
  type HypothesesDto,
  type ManualHypothesesDto,
  type SamplingDto,
} from '../api/endpoints/research'
import { useCaseStore } from '../stores/case'
import { presentError, isApiError } from '../api/errors'
import { CANDIDATE_ZONE_CLASS, restrictedList } from '../domain/miaoSuan'
import DualTrackCompare from '../components/research/DualTrackCompare.vue'
import HeatGrid from '../components/research/HeatGrid.vue'
import EmptyState from '../components/common/EmptyState.vue'

const cs = useCaseStore()
const router = useRouter()
const route = useRoute()
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

/** 带 ?from= 进入详情：返回时回庙算工作台，而不是被甩到线索列表 */
function openClue(clueId: string): void {
  void router.push({
    path: `/c/clue/${encodeURIComponent(clueId)}`,
    query: { from: route.path },
  })
}

// ---------- P1/P2：人工假设 ----------
// 只持久化人工部分：自动假设从 findings 派生（每次 BUILD 重算），落盘会
// 造成产物与数据漂移；人工假设是正兵判断，不该被重扫冲掉。
const manual = ref<ManualHypothesesDto | null>(null)
const mBusy = ref(false)
const showAdd = ref(false)
const form = ref({
  description: '',
  falsification: '',
  procedure: '',
  evidence_needed: '',
  data_sources: '',
})

function splitList(s: string): string[] {
  return s.split(/[,，、;；\n]/).map((x) => x.trim()).filter(Boolean)
}

async function loadManual(): Promise<void> {
  if (!cs.currentCaseId) return
  try {
    manual.value = await researchApi.manualList(cs.currentCaseId)
  } catch {
    manual.value = null
  }
}

async function submitAdd(): Promise<void> {
  if (!cs.currentCaseId || !form.value.description.trim()) return
  if (!form.value.falsification.trim()) {
    message.error('请填写证伪条件（庙算要求每条假设可自动证伪）')
    return
  }
  mBusy.value = true
  try {
    manual.value = await researchApi.manualAdd(cs.currentCaseId, {
      description: form.value.description.trim(),
      falsification: form.value.falsification.trim(),
      procedure: form.value.procedure.trim(),
      evidence_needed: splitList(form.value.evidence_needed),
      data_sources: splitList(form.value.data_sources),
    })
    form.value = {
      description: '', falsification: '', procedure: '',
      evidence_needed: '', data_sources: '',
    }
    showAdd.value = false
    message.success('人工假设已添加')
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    mBusy.value = false
  }
}

async function removeManual(id: string): Promise<void> {
  if (!cs.currentCaseId) return
  mBusy.value = true
  try {
    manual.value = await researchApi.manualRemove(cs.currentCaseId, id)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    mBusy.value = false
  }
}

/** 上移（人工假设排序；正兵判断优先的排前面） */
async function moveUp(idx: number): Promise<void> {
  const items = manual.value?.items ?? []
  if (idx <= 0 || idx >= items.length || !cs.currentCaseId) return
  const order = items.map((x) => x.id)
  ;[order[idx - 1], order[idx]] = [order[idx], order[idx - 1]]
  mBusy.value = true
  try {
    manual.value = await researchApi.manualReorder(cs.currentCaseId, order)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    mBusy.value = false
  }
}

// ---------- P3：采样预演 ----------
// core/sampling.py 1% 采样验证假设方向，避免盲投全量算力。
// 红线：只给方向建议，是否投全量由正兵拍板（不自动触发全量扫描）。
const sampling = ref<SamplingDto | null>(null)
const sBusy = ref(false)
const selIds = ref<string[]>([])
const ratio = ref(0.01)

/** 可选假设：人工假设 + 自动假设（H1..H5，来自模式库/规则声明） */
const sampleOptions = computed(() => {
  const out: { label: string; value: string }[] = []
  for (const m of manual.value?.items ?? []) {
    out.push({ label: `${m.id} ${m.description}（人工）`, value: m.id })
  }
  for (const id of ['H1', 'H2', 'H3', 'H4', 'H5']) {
    out.push({ label: `${id}（自动生成）`, value: id })
  }
  return out
})

async function runSampling(): Promise<void> {
  if (!cs.currentCaseId || !selIds.value.length) {
    message.warning('请先选择要预演的假设')
    return
  }
  sBusy.value = true
  try {
    sampling.value = await researchApi.samplingPreflight(
      cs.currentCaseId, selIds.value, ratio.value)
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    sBusy.value = false
  }
}

watch(() => cs.currentCaseId, () => void loadManual(), { immediate: true })
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

        <!-- P1/P2 人工假设：只持久化人工部分，自动假设保持派生（随数据重算） -->
        <div class="card">
          <div class="card-title">
            人工假设（{{ manual?.items.length ?? 0 }}）
            <NTag size="tiny" :bordered="false" type="info">不随重扫丢失</NTag>
            <NButton size="tiny" @click="showAdd = !showAdd">
              {{ showAdd ? '收起' : '+ 新增' }}
            </NButton>
          </div>
          <p class="dim zone-note">
            自动生成的假设由数据派生、每次 BUILD 重算，不落盘；此处保存的是正兵
            手工添加的判断，重扫不会被冲掉。证伪条件必填——庙算要求每条假设可自动证伪。
          </p>

          <div v-if="showAdd" class="add-form">
            <NInput
              v-model:value="form.description"
              placeholder="假设描述（必填）"
              :disabled="mBusy"
              data-testid="hyp-desc-input"
            />
            <NInput
              v-model:value="form.falsification"
              placeholder="证伪条件（必填，如：流水无对价时间耦合则证伪）"
              :disabled="mBusy"
              data-testid="hyp-falsify-input"
            />
            <NInput
              v-model:value="form.evidence_needed"
              placeholder="所需证据（逗号分隔，选填）"
              :disabled="mBusy"
            />
            <NInput
              v-model:value="form.data_sources"
              placeholder="可调用数据源（逗号分隔，选填）"
              :disabled="mBusy"
            />
            <NInput
              v-model:value="form.procedure"
              placeholder="对应程序（选填）"
              :disabled="mBusy"
            />
            <div class="form-actions">
              <NButton size="small" type="primary" :loading="mBusy"
                data-testid="hyp-submit" @click="submitAdd">保存</NButton>
              <NButton size="small" @click="showAdd = false">取消</NButton>
            </div>
          </div>

          <EmptyState
            v-if="!(manual?.items.length)"
            type="empty"
            title="暂无人工假设"
            desc="点击「+ 新增」补充正兵判断（系统自动生成的假设由数据派生，不在此列）"
          />
          <ul v-else class="hyp-list">
            <li v-for="(m, i) in manual.items" :key="m.id" class="hyp-item"
              :data-testid="`hyp-item-${m.id}`">
              <div class="hyp-head">
                <span class="mono hyp-id">{{ m.id }}</span>
                <strong>{{ m.description }}</strong>
                <NTag size="tiny" :bordered="false">人工</NTag>
                <span class="hyp-actions">
                  <NButton size="tiny" :disabled="i === 0 || mBusy"
                    @click="moveUp(i)">↑</NButton>
                  <NButton size="tiny" :disabled="mBusy"
                    @click="removeManual(m.id)">删除</NButton>
                </span>
              </div>
              <p class="dim hyp-falsify">证伪：{{ m.falsification }}</p>
              <p v-if="m.evidence_needed.length" class="dim hyp-meta">
                所需证据：{{ m.evidence_needed.join('、') }}
              </p>
            </li>
          </ul>
        </div>

        <!-- P3 采样预演：1% 采样验证方向，避免盲投全量算力 -->
        <div class="card">
          <div class="card-title">
            采样预演
            <NTag size="tiny" :bordered="false" type="warning">仅方向建议</NTag>
          </div>
          <p class="dim zone-note">
            对小样本试跑已选假设，判断方向是否值得投入全量算力。命中率 ≥5% 方向明确、
            1%~5% 存疑（建议扩大到 5% 再验）、&lt;1% 方向否定。
            <strong>只给建议，是否投全量由正兵拍板。</strong>
          </p>
          <div class="sample-run">
            <NSelect
              v-model:value="selIds"
              multiple
              :options="sampleOptions"
              placeholder="选择要预演的假设"
              :disabled="sBusy"
              class="sample-select"
              data-testid="sample-select"
            />
            <NSelect
              v-model:value="ratio"
              :options="[
                { label: '1%', value: 0.01 },
                { label: '5%', value: 0.05 },
                { label: '10%', value: 0.1 },
              ]"
              :disabled="sBusy"
              class="sample-ratio"
            />
            <NButton size="small" type="primary" :loading="sBusy"
              data-testid="sample-run" @click="runSampling">跑预演</NButton>
          </div>

          <div v-if="sampling" class="sample-result" data-testid="sample-result">
            <div class="sample-overall">
              整体判定：
              <NTag size="small" :bordered="false" :type="verdictTone(sampling.overall_verdict)">
                {{ sampling.overall_verdict }}
              </NTag>
              <span class="dim">{{ sampling.suggest }}</span>
            </div>
            <ul class="sample-list">
              <li v-for="r in sampling.results" :key="r.hypothesis_id" class="sample-item">
                <span class="mono">{{ r.hypothesis_id }}</span>
                <span class="dim">
                  采样 {{ r.sampled_rows }} 行，命中 {{ r.hit_rows }} 行（{{ (r.hit_rate * 100).toFixed(2) }}%）
                </span>
                <NTag size="tiny" :bordered="false" :type="verdictTone(r.verdict)">
                  {{ r.verdict }}
                </NTag>
              </li>
            </ul>
          </div>
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
/* ---- P1/P2 人工假设 ---- */
.add-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
}
.form-actions {
  display: flex;
  gap: 8px;
}
.hyp-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hyp-item {
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.hyp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hyp-id {
  font-size: 11px;
  color: var(--sun-warn-text);
}
.hyp-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
.hyp-falsify,
.hyp-meta {
  margin: 0;
  font-size: 12px;
}

/* ---- P3 采样预演 ---- */
.sample-run {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.sample-select {
  flex: 1 1 240px;
  min-width: 200px;
}
.sample-ratio {
  width: 90px;
}
.sample-result {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
  border: 1px dashed var(--sun-border);
  border-radius: 6px;
}
.sample-overall {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
}
.sample-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.sample-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  flex-wrap: wrap;
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
