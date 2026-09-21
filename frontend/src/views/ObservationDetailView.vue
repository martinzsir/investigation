<script setup lang="ts">
// 观察档案详情：判据 + 证伪条件 + 事实明细 + 证据引用。
//
// 三件套的分工（对应"直接性 vs 庞杂数据"的张力）
//   判据       —— 一句话说明"这些说明了什么"（入口，先读这个）
//   事实明细   —— 可逐条核对的行（下钻才铺开）
//   证伪条件   —— 什么情况下这不成立（帮正兵反驳，不是只让他接受）
// 缺任何一件，正兵要么陷在庞杂数据里，要么只拿到一个无从辩驳的结论。
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpin, NTag, NEmpty, NButton, useMessage } from 'naive-ui'
import { useCaseStore } from '../stores/case'
import {
  observationsApi,
  type ObservationDetailResult,
  type HypothesisChoice,
} from '../api/endpoints/observations'
import { presentError } from '../api/errors'

const cs = useCaseStore()
const route = useRoute()
const router = useRouter()
const message = useMessage()

const loading = ref(false)
const errorMsg = ref('')
const data = ref<ObservationDetailResult | null>(null)

const observationId = computed(() => String(route.params.observationId ?? ''))

/** 事实明细默认折叠数量：判据是入口，明细按需展开 */
const FACT_PREVIEW = 10
const showAllFacts = ref(false)
const visibleFacts = computed(() => {
  const f = data.value?.facts ?? []
  return showAllFacts.value ? f : f.slice(0, FACT_PREVIEW)
})

/** 事实行文本：日期 + 偏移 + 类型 + 摘要（人能读的一行） */
function factText(f: {
  date: string | null
  offset_days: number | null
  type: string | null
  role: string | null
  brief: string | null
}): string {
  const parts: string[] = []
  if (f.date) parts.push(String(f.date))
  if (typeof f.offset_days === 'number') {
    parts.push(`偏移 ${f.offset_days > 0 ? '+' : ''}${f.offset_days} 天`)
  }
  if (f.role) parts.push(String(f.role))
  const head = parts.join(' · ')
  return f.brief ? (head ? `${head}｜${f.brief}` : String(f.brief)) : head || '（无摘要）'
}

/** 证据引用文本：聚合量 / 语义层定位 */
function refText(r: Record<string, unknown>): string {
  const kind = String(r.kind ?? '')
  if (kind === 'aggregate') {
    // 五间交叉的 aggregate 不带 ref（表级覆盖度，无行级定位），
    // 但带 source/table/obj_type 让用户能看懂"是哪个数据源的几行"。
    const src = r.source ? String(r.source) : ''
    const tbl = r.table ? String(r.table) : ''
    const head = src && tbl ? `${src}（${tbl}）` : src || tbl || ''
    return head ? `${head} 行数=${r.value}` : `聚合量 ${r.metric}：${r.value}`
  }
  return `${kind}：${r.ref ?? ''}`
}

async function load(): Promise<void> {
  if (!cs.currentCaseId || !observationId.value) return
  loading.value = true
  errorMsg.value = ''
  try {
    data.value = await observationsApi.detail(cs.currentCaseId, observationId.value)
  } catch (e) {
    errorMsg.value = presentError(e).title
    data.value = null
  } finally {
    loading.value = false
  }
}

// ---- 提升为线索 ----
const promoting = ref(false)
const showPromote = ref(false)
const hypotheses = ref<HypothesisChoice[]>([])
/** 选中的假设（必选：没有证伪目标的线索无法处置） */
const pickedHypothesis = ref<string>('')
const promoteNote = ref('')

/** 假设选项：id + 描述 + 证伪条件（不硬编码，本体声明什么就显示什么） */
const hypothesisOptions = computed(() =>
  hypotheses.value.map((h) => ({
    label: h.falsification
      ? `${h.id} ${h.description}`
      : `${h.id} ${h.description}`,
    value: h.id,
  })),
)

const pickedDesc = computed(() => {
  const h = hypotheses.value.find((x) => x.id === pickedHypothesis.value)
  return h ?? null
})

async function ensureHypotheses(): Promise<void> {
  if (hypotheses.value.length || !cs.currentCaseId) return
  try {
    const r = await observationsApi.hypotheses(cs.currentCaseId)
    hypotheses.value = r.hypotheses ?? []
  } catch {
    hypotheses.value = [] // 取不到就空：不允许在无选项时硬填假设
  }
}

async function openPromote(): Promise<void> {
  await ensureHypotheses()
  showPromote.value = true
}

async function doPromote(): Promise<void> {
  if (!cs.currentCaseId || !observationId.value) return
  if (!pickedHypothesis.value) {
    message.warning('必须指定待验证的假设——没有证伪目标的线索无法处置')
    return
  }
  promoting.value = true
  try {
    const r = await observationsApi.promote(cs.currentCaseId, observationId.value, {
      hypothesis: pickedHypothesis.value,
      note: promoteNote.value,
    })
    message.success(`已入队提升（任务 ${r.task_id}），完成后可在线索列表查看`)
    showPromote.value = false
    void load()
  } catch (e) {
    message.error(presentError(e).title)
  } finally {
    promoting.value = false
  }
}

async function setDisposition(d: '已认领' | '已归档' | '未认领'): Promise<void> {
  if (!cs.currentCaseId || !observationId.value) return
  try {
    await observationsApi.setDisposition(cs.currentCaseId, observationId.value, d)
    message.success(`已${d === '未认领' ? '取消认领' : d}`)
    void load()
  } catch (e) {
    message.error(presentError(e).title)
  }
}

onMounted(load)
</script>

<template>
  <div class="od-view">
    <header class="od-head">
      <NButton size="tiny" text @click="router.back()">← 返回观察档案</NButton>
      <div v-if="data" class="od-tags">
        <NTag size="tiny" round>{{ data.lens_name }}</NTag>
        <NTag
          size="tiny"
          round
          :type="data.disposition === '已提升' ? 'success' : 'warning'"
        >{{ data.disposition }}</NTag>
        <NTag v-if="data.degraded" size="tiny" round type="error" :title="data.degraded_reason">
          数据未齐
        </NTag>
      </div>
    </header>

    <NSpin :show="loading">
      <div v-if="errorMsg" class="od-err">{{ errorMsg }}</div>
      <NEmpty v-else-if="!loading && !data" description="观察不存在或已不在当前版本" />

      <template v-else-if="data">
        <h2 class="od-title">{{ data.title }}</h2>
        <p class="od-obj">
          <span v-if="data.subject">主体：{{ data.subject }}</span>
          <span v-if="data.project"> · 项目：{{ data.project }}</span>
          <span v-if="data.param_source" class="dim"> · 靶心 {{ data.param_source }}</span>
        </p>

        <!-- ① 判据：先读这一句 -->
        <section class="od-sec">
          <h3 class="od-sec-title">判据</h3>
          <p v-if="data.basis" class="od-basis">{{ data.basis }}</p>
          <p v-else class="od-basis dim">（本条观察未生成判据）</p>
        </section>

        <!-- ② 证伪条件：什么情况下这不成立（可辩驳性） -->
        <section v-if="data.falsification" class="od-sec">
          <h3 class="od-sec-title">证伪条件</h3>
          <p class="od-falsify">{{ data.falsification }}</p>
        </section>

        <!-- ③ 事实明细：下钻才铺开 -->
        <section class="od-sec">
          <h3 class="od-sec-title">
            事实明细
            <span class="dim">（{{ data.facts.length }} 条）</span>
          </h3>
          <NEmpty v-if="!data.facts.length" description="本条观察无事件明细（关系类观察以证据引用为主）" />
          <template v-else>
            <ul class="od-facts">
              <li v-for="(f, i) in visibleFacts" :key="i" class="od-fact">
                {{ factText(f) }}
              </li>
            </ul>
            <NButton
              v-if="data.facts.length > FACT_PREVIEW"
              size="tiny"
              text
              type="primary"
              @click="showAllFacts = !showAllFacts"
            >
              {{ showAllFacts ? '收起' : `展开全部 ${data.facts.length} 条` }}
            </NButton>
          </template>
        </section>

        <!-- ④ 证据引用：可定位回语义层真实行 -->
        <section class="od-sec">
          <h3 class="od-sec-title">
            证据引用
            <span class="dim">（{{ data.evidence_refs.length }} 条）</span>
          </h3>
          <ul class="od-facts">
            <li v-for="(r, i) in data.evidence_refs" :key="i" class="od-fact od-fact--ref">
              {{ refText(r) }}
            </li>
          </ul>
        </section>

        <!-- ⑤ 数据未齐说明：降级不静默 -->
        <section v-if="data.degraded" class="od-sec">
          <p class="od-degrade">数据未齐：{{ data.degraded_reason || '部分数据源未接入，产出不完整' }}</p>
        </section>

        <!-- ⑥ 已提升 → 可跳回线索 -->
        <section v-if="data.promoted_clue_id" class="od-sec">
          <NButton
            size="small"
            secondary
            type="success"
            @click="router.push(`/c/clue/${data.promoted_clue_id}`)"
          >
            查看提升后的线索（假设 {{ data.promoted_hypothesis }}）
          </NButton>
        </section>

        <!-- ⑦ 提升/认领动作 -->
        <section v-else class="od-sec od-actions">
          <NButton size="small" type="primary" @click="openPromote">
            提升为线索
          </NButton>
          <NButton
            v-if="data.disposition !== '已认领'"
            size="small"
            secondary
            @click="setDisposition('已认领')"
          >认领</NButton>
          <NButton
            v-else
            size="small"
            secondary
            @click="setDisposition('未认领')"
          >取消认领</NButton>
          <NButton size="small" quaternary @click="setDisposition('已归档')">
            归档
          </NButton>
        </section>

        <!-- 提升面板：假设必选 -->
        <section v-if="showPromote" class="od-promote">
          <h3 class="od-sec-title">提升为线索</h3>
          <p class="od-hint">
            提升即断言「这批观察构成疑点」。必须指定待验证的假设——
            线索是待证明的命题，没有证伪目标就无法处置。
          </p>
          <NSelect
            v-model:value="pickedHypothesis"
            :options="hypothesisOptions"
            size="small"
            placeholder="选择待验证的假设"
            class="op-sel"
            data-testid="promote-hypothesis"
          />
          <p v-if="pickedDesc" class="op-desc">
            <b>{{ pickedDesc.id }}</b> {{ pickedDesc.description }}
            <span v-if="pickedDesc.falsification" class="dim">
              · 证伪：{{ pickedDesc.falsification }}
            </span>
          </p>
          <NInput
            v-model:value="promoteNote"
            size="small"
            type="textarea"
            :rows="2"
            placeholder="备注（可选）"
            class="op-note"
          />
          <div class="op-btns">
            <NButton
              size="small"
              type="primary"
              :loading="promoting"
              :disabled="!pickedHypothesis"
              @click="doPromote"
            >确认提升</NButton>
            <NButton size="small" quaternary @click="showPromote = false">
              取消
            </NButton>
          </div>
        </section>
      </template>
    </NSpin>
  </div>
</template>

<style scoped>
.od-view {
  padding: 16px;
  max-width: 900px;
}
.od-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.od-tags {
  display: flex;
  gap: 6px;
}
.od-title {
  margin: 0 0 6px;
  font-size: 17px;
}
.od-obj {
  margin: 0 0 16px;
  font-size: 12px;
  color: var(--sun-text-secondary, #555);
}
.od-sec {
  margin-bottom: 18px;
}
.od-sec-title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
}
.od-basis {
  margin: 0;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.6;
  background: var(--sun-bg-secondary, #fafafa);
  border-left: 3px solid var(--sun-primary, #2080f0);
  border-radius: 0 4px 4px 0;
}
.od-falsify {
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.6;
  background: var(--sun-bg-secondary, #fafafa);
  border-left: 3px solid var(--sun-warning, #d89614);
  border-radius: 0 4px 4px 0;
}
.od-facts {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.od-fact {
  font-size: 12px;
  padding: 5px 8px;
  border: 1px solid var(--sun-border, #eee);
  border-radius: 4px;
  line-height: 1.5;
}
.od-fact--ref {
  color: var(--sun-text-tertiary);
  font-family: monospace;
  font-size: 11px;
}
.od-degrade {
  margin: 0;
  font-size: 12px;
  color: var(--sun-warning, #d89614);
}
.od-err {
  color: var(--sun-error, #d03050);
  font-size: 12px;
  padding: 8px 0;
}
.od-actions {
  display: flex;
  gap: 8px;
  padding-top: 4px;
}
.od-promote {
  margin-top: 4px;
  padding: 12px;
  border: 1px solid var(--sun-border, #e5e5e5);
  border-radius: 6px;
  background: var(--sun-bg-secondary, #fafafa);
}
.od-hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--sun-text-secondary, #555);
}
.op-sel {
  width: 100%;
  max-width: 420px;
}
.op-desc {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.5;
}
.op-note {
  margin-top: 8px;
  max-width: 420px;
}
.op-btns {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.dim {
  color: var(--sun-text-tertiary);
  font-weight: 400;
}
</style>
