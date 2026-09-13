<script setup lang="ts">
// M5 RC-301：画布只读问答侧栏。
// 调用 canvasApi.chat，展示 facts（有据）/ pending（待核实），引用角标可点。
import { ref } from 'vue'
import { NButton, NInput, NSpin, NTag } from 'naive-ui'
import { ChatbubbleOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { canvasApi } from '../../api/endpoints/canvas'
import type { CanvasChatEnvelope, CanvasChatFact } from '../../domain/canvas'

const props = defineProps<{
  caseId: string
  clueId: string
}>()

const emit = defineEmits<{
  /** 点击引用角标：定位画布节点 */
  (e: 'cite-click', ref: string): void
}>()

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  facts?: CanvasChatFact[]
  pending?: string[]
  warnings?: string[]
}

const messages = ref<ChatMsg[]>([])
const question = ref('')
const loading = ref(false)
const errorMsg = ref('')
const submittingIdx = ref<string | null>(null)

const RECOMMENDED = [
  '这条线索最关键的待核实点是什么？',
  '哪些事实已有数据行支撑？',
  '现有证据能否证实假设？',
]

async function send() {
  const q = question.value.trim()
  if (!q || loading.value) return
  messages.value.push({ role: 'user', content: q })
  question.value = ''
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await canvasApi.chat(props.caseId, props.clueId, q)
    messages.value.push({
      role: 'assistant',
      content: res.answer,
      facts: res.facts,
      pending: res.pending,
      warnings: res.warnings,
    })
  } catch (e: any) {
    const msg = e?.response?.data?.message || e?.message || '回答生成失败'
    errorMsg.value = msg
    messages.value.push({ role: 'assistant', content: msg })
  } finally {
    loading.value = false
  }
}

async function adoptSuggestion(text: string, key: string) {
  submittingIdx.value = key
  try {
    const res = await canvasApi.submitSuggestion(
      props.caseId, props.clueId, text)
    // 在该 pending 项后追加一条提示消息
    messages.value.push({
      role: 'assistant',
      content: res.message,
      facts: [],
      pending: [],
      warnings: [],
    })
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e?.message || '提交失败'
  } finally {
    submittingIdx.value = null
  }
}

function useRecommended(q: string) {
  question.value = q
}
</script>

<template>
  <div class="canvas-chat-panel" data-testid="canvas-chat-panel">
    <div class="cc-header">
      <NIcon :component="ChatbubbleOutline" />
      <span>画布问答</span>
      <NTag size="small" type="info" round>只读</NTag>
    </div>

    <div class="cc-messages">
      <div v-if="messages.length === 0" class="cc-empty">
        <p>就当刻画布提问，回答中的事实句会带可点击引用。</p>
        <div class="cc-recommend">
          <NButton
            v-for="r in RECOMMENDED"
            :key="r"
            size="small"
            quaternary
            @click="useRecommended(r)"
          >
            {{ r }}
          </NButton>
        </div>
      </div>

      <div
        v-for="(m, i) in messages"
        :key="i"
        class="cc-msg"
        :class="m.role"
      >
        <div class="cc-role">{{ m.role === 'user' ? '我' : '助手' }}</div>
        <div class="cc-bubble">
          <template v-if="m.role === 'assistant' && m.facts">
            <div v-if="m.facts.length" class="cc-facts">
              <div class="cc-sec-title">事实与依据</div>
              <div v-for="(f, j) in m.facts" :key="j" class="cc-fact">
                <span>{{ f.sentence }}</span>
                <span
                  v-for="c in f.citations"
                  :key="c"
                  class="cc-cite"
                  @click="emit('cite-click', c)"
                >[{{ c }}]</span>
              </div>
            </div>
            <div v-if="m.pending?.length" class="cc-pending">
              <div class="cc-sec-title">模型推测（待核实）</div>
              <div
                v-for="(p, j) in m.pending"
                :key="j"
                class="cc-pending-item"
              >
                <span>{{ p }}</span>
                <NButton
                  size="tiny"
                  type="primary"
                  quaternary
                  :loading="submittingIdx === `${i}-${j}`"
                  @click="adoptSuggestion(p, `${i}-${j}`)"
                >
                  提交为核实建议
                </NButton>
              </div>
            </div>
            <div v-if="m.warnings?.length" class="cc-warn">
              {{ m.warnings.length }} 条表述因缺乏依据已转入待核实
            </div>
          </template>
          <template v-else>
            {{ m.content }}
          </template>
        </div>
      </div>

      <div v-if="loading" class="cc-loading">
        <NSpin size="small" /> 正在生成回答…
      </div>
    </div>

    <div v-if="errorMsg" class="cc-error">{{ errorMsg }}</div>

    <div class="cc-input">
      <NInput
        v-model:value="question"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 4 }"
        maxlength="500"
        placeholder="输入问题（≤500 字）"
        @keydown.enter.exact.prevent="send"
      />
      <NButton type="primary" :loading="loading" @click="send">发送</NButton>
    </div>
  </div>
</template>

<style scoped>
.canvas-chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--sun-bg-card);
}
.cc-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--sun-border);
  font-weight: 600;
}
.cc-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cc-empty {
  color: var(--sun-text-secondary);
  font-size: 13px;
}
.cc-recommend {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 12px;
  align-items: flex-start;
}
.cc-msg {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cc-msg.user {
  align-items: flex-end;
}
.cc-role {
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.cc-bubble {
  max-width: 85%;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
}
.cc-msg.user .cc-bubble {
  background: var(--sun-primary);
  color: #fff;
}
.cc-msg.assistant .cc-bubble {
  background: var(--sun-bg-elevated);
  border: 1px solid var(--sun-border);
}
.cc-sec-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-secondary);
  margin-bottom: 4px;
}
.cc-fact {
  margin-bottom: 4px;
}
.cc-cite {
  display: inline-block;
  margin-left: 4px;
  padding: 0 4px;
  font-size: 11px;
  color: var(--sun-primary);
  cursor: pointer;
  text-decoration: underline;
}
.cc-pending {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--sun-border);
}
.cc-pending-item {
  color: var(--sun-text-tertiary);
  font-style: italic;
}
.cc-warn {
  margin-top: 6px;
  font-size: 11px;
  color: var(--sun-warn);
}
.cc-loading {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.cc-error {
  padding: 8px 12px;
  font-size: 12px;
  color: var(--sun-danger);
}
.cc-input {
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid var(--sun-border);
}
.cc-input :deep(.n-input) {
  flex: 1;
}
</style>
