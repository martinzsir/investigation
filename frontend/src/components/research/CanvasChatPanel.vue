<script setup lang="ts">
// M5 RC-301：画布只读问答侧栏。
// 调用 canvasApi.chatStream（SSE 流式），展示 facts（有据）/ pending（待核实），引用角标可点。
// 双模式：direct（默认）/ react（AgentScope ReAct + MCP 只读工具多轮推理）。
import { ref, nextTick } from 'vue'
import { NButton, NInput, NSpin, NTag, NRadioGroup, NRadioButton } from 'naive-ui'
import { ChatbubbleOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { canvasApi } from '../../api/endpoints/canvas'
import type { ChatStreamHandlers } from '../../api/endpoints/canvas'
import type {
  CanvasChatArtifact,
  CanvasChatEnvelope,
  CanvasChatFact,
} from '../../domain/canvas'
import type { StreamHandle } from '../../api/transport/types'

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
  streaming?: boolean
  /** ReAct 模式专属：思考过程增量文本 */
  thinking?: string
  /** ReAct 模式专属：工具调用列表（按到达顺序） */
  toolCalls?: Array<{ id: string; name: string; state?: string }>
  /** ReAct 模式专属：工具确定性产物（非 null 时 content 即产物原文） */
  artifact?: CanvasChatArtifact | null
}

/**
 * 轻量级 Markdown 渲染 + 引用标记转可点击 span。
 * 支持：代码块、标题(#/##/###)、粗体、斜体、行内代码、
 * 无序列表、有序列表、表格、引用块(>)、分割线(---)。
 * 引用标记 [cite:xxx] / [pb:xxx] / [vi_xxx] / [R6] /
 * @local#row/xxx / 裸 vi_xxx 转为 <span class="cc-cite" data-ref="xxx">。
 *
 * 管线纪律（曾因正则互相污染导致整页样式崩坏）：
 *  1. 先 HTML 转义，并在「生成任何自制标签之前」清理 LLM 的 HTML 残片；
 *  2. 代码块/行内代码/引用一律先抽成 NUL 占位符，最后统一恢复——
 *     后续 markdown 正则永远碰不到它们的 HTML，杜绝重复嵌套与属性泄漏；
 *  3. 块级元素正则吞掉行尾换行，收尾再清除块级标签相邻的 <br>。
 */
function renderMarkdownWithCites(content: string): string {
  if (!content) return ''

  // 占位符表（NUL 不会出现在正常输出中；首尾双 NUL 防止数字前缀互串）
  const codeBlocks: string[] = []
  const inlineCodes: string[] = []
  const cites: string[] = []
  const ph = (kind: string, i: number) => `\x00${kind}${i}\x00`

  // 1. HTML 转义（防注入）
  let html = content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

  // 2. 清理 LLM 输出的 HTML 残片（此刻字符串中只有转义文本，没有自制标签，
  //    绝不能在后面的步骤里做这种全局清理——会删掉自制标签结尾的 ">）
  html = html.replace(/"&gt;/g, '')

  // 3. 提取代码块
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, _lang, code) => {
    const i = codeBlocks.push(
      `<pre class="md-pre"><code>${code.replace(/\n$/, '')}</code></pre>`) - 1
    return ph('CB', i)
  })

  // 4. 提取行内代码（先于引用，代码里的 vi_xxx 不做链接化）
  html = html.replace(/`([^`\n]+)`/g, (_m, code) => {
    const i = inlineCodes.push(`<code class="md-code">${code}</code>`) - 1
    return ph('CI', i)
  })

  // 5. 提取全部引用为占位符（一次性扫描，避免多个正则互相套娃）
  const pushCite = (ref: string, label?: string): string => {
    const i = cites.push(
      `<span class="cc-cite" data-ref="${ref}">${label ?? `[${ref}]`}</span>`) - 1
    return ph('CT', i)
  }
  html = html.replace(
    /\[(?:cite|pb):([^\[\]]+)\]|\[(vi_[a-f0-9]{8,})\]|\[(R\d+)\]|@local#row\/[a-f0-9]+|\bvi_[a-f0-9]{8,}\b/g,
    (m: string, multi?: string, viBr?: string, rule?: string) => {
      if (multi) {
        // 逗号分隔多引用：各生成独立 chip 连排
        return multi.split(',').map(s => s.trim()).filter(Boolean)
          .map(r => pushCite(r)).join('')
      }
      if (viBr) return pushCite(viBr, viBr)
      if (rule) return pushCite(rule)
      // @local#row/xxx 或裸 vi_xxx：原文即标签
      return pushCite(m, m)
    },
  )

  // 6. 标题（# 统一降为 h2，吞掉行尾换行）
  html = html.replace(/^#### (.+)$\n?/gm, '<h4 class="md-h4">$1</h4>')
  html = html.replace(/^### (.+)$\n?/gm, '<h3 class="md-h3">$1</h3>')
  html = html.replace(/^## (.+)$\n?/gm, '<h2 class="md-h2">$1</h2>')
  html = html.replace(/^# (.+)$\n?/gm, '<h2 class="md-h2">$1</h2>')

  // 7. 分割线
  html = html.replace(/^---+$\n?/gm, '<hr class="md-hr">')

  // 8. 引用块
  html = html.replace(
    /^&gt; (.+)$\n?/gm, '<blockquote class="md-quote">$1</blockquote>')

  // 9. 表格（管道语法）
  html = html.replace(
    /(?:^\|.+\|$\n?)+/gm,
    (match) => {
      const lines = match.trim().split('\n')
      if (lines.length < 2) return match
      // 检测分隔行
      const sepIdx = lines.findIndex(l => /^\|[\s:|-]+\|$/.test(l))
      if (sepIdx < 1) return match
      const headerCells = lines[0].split('|').filter(c => c.trim())
      const bodyLines = lines.slice(sepIdx + 1)
      let tbl = '<table class="md-table"><thead><tr>'
      headerCells.forEach(c => { tbl += `<th>${c.trim()}</th>` })
      tbl += '</tr></thead><tbody>'
      bodyLines.forEach(line => {
        const cells = line.split('|').filter(c => c.trim())
        tbl += '<tr>'
        cells.forEach(c => { tbl += `<td>${c.trim()}</td>` })
        tbl += '</tr>'
      })
      tbl += '</tbody></table>'
      return tbl
    },
  )

  // 10. 无序列表
  html = html.replace(
    /(?:^[-*] (.+)$\n?)+/gm,
    (match) => {
      const items = match.trim().split('\n')
        .map(l => l.replace(/^[-*] /, ''))
      return `<ul class="md-ul">${items.map(i => `<li>${i}</li>`).join('')}</ul>`
    },
  )

  // 11. 有序列表
  html = html.replace(
    /(?:^\d+\. (.+)$\n?)+/gm,
    (match) => {
      const items = match.trim().split('\n')
        .map(l => l.replace(/^\d+\. /, ''))
      return `<ol class="md-ol">${items.map(i => `<li>${i}</li>`).join('')}</ol>`
    },
  )

  // 12. 粗体 / 斜体
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>')

  // 13. 换行 → <br>，然后清除块级标签相邻的 <br>（块级元素自带 margin）
  html = html.replace(/\n/g, '<br>')
  html = html.replace(
    /(?:<br>)+(<(?:h[234]|ul|ol|pre|blockquote|table|hr)\b)/g, '$1')
  html = html.replace(
    /(<\/(?:h[234]|ul|ol|pre|blockquote|table)>|<hr[^>]*>)(?:<br>)+/g, '$1')
  // 连续 3+ <br> 压为段落间距 2 个
  html = html.replace(/(?:<br>){3,}/g, '<br><br>')

  // 14. 恢复占位符（最后一步，恢复后不再跑任何正则）
  html = html.replace(/\x00CB(\d+)\x00/g,
    (_m, i) => codeBlocks[Number(i)])
  html = html.replace(/\x00CI(\d+)\x00/g,
    (_m, i) => inlineCodes[Number(i)])
  html = html.replace(/\x00CT(\d+)\x00/g,
    (_m, i) => cites[Number(i)])

  return html
}

/** 答案区域点击代理：检测 data-ref 属性 */
function handleAnswerClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  const citeEl = target.closest('[data-ref]') as HTMLElement | null
  if (citeEl) {
    e.preventDefault()
    emit('cite-click', citeEl.dataset.ref || '')
  }
}

const messages = ref<ChatMsg[]>([])
const question = ref('')
const loading = ref(false)
const errorMsg = ref('')
const submittingIdx = ref<string | null>(null)
/** 双模式开关：direct=单轮 LLM / react=AgentScope ReAct + MCP 只读工具 */
const chatMode = ref<'direct' | 'react'>('direct')
/** 当前流式连接句柄（可中断） */
let streamHandle: StreamHandle | null = null

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

  // 创建占位助手消息（流式追加内容）
  const assistantIdx = messages.value.push({
    role: 'assistant', content: '', facts: [], pending: [], warnings: [],
    streaming: true,
    thinking: '',
    toolCalls: [],
  }) - 1

  // 滚动到底部
  await nextTick()
  scrollMessagesToBottom()

  const handlers: ChatStreamHandlers = {
    onThinking: (text) => {
      const m = messages.value[assistantIdx]
      m.thinking = (m.thinking || '') + text
      scrollMessagesToBottom()
    },
    onToolCall: (name, id) => {
      const m = messages.value[assistantIdx]
      if (!m.toolCalls) m.toolCalls = []
      m.toolCalls.push({ id, name })
      scrollMessagesToBottom()
    },
    onToolResult: (name, id, state) => {
      const m = messages.value[assistantIdx]
      const tc = m.toolCalls?.find(t => t.id === id)
      if (tc) {
        tc.state = state || 'done'
      } else {
        if (!m.toolCalls) m.toolCalls = []
        m.toolCalls.push({ id, name, state: state || 'done' })
      }
      scrollMessagesToBottom()
    },
    onDelta: (text) => {
      messages.value[assistantIdx].content += text
      scrollMessagesToBottom()
    },
    onDone: (result) => {
      // artifact 非空时服务端已把 answer 替换为产物原文（md 报告），
      // 流式期间模型的一句话总结同步被覆盖
      messages.value[assistantIdx].content = result.answer
      messages.value[assistantIdx].facts = result.facts
      messages.value[assistantIdx].pending = result.pending
      messages.value[assistantIdx].warnings = result.warnings
      messages.value[assistantIdx].artifact = result.artifact ?? null
      messages.value[assistantIdx].streaming = false
    },
    onErrorEvent: (error, message) => {
      errorMsg.value = message
      messages.value[assistantIdx].content = message
      messages.value[assistantIdx].streaming = false
    },
    onTransportError: () => {
      errorMsg.value = '网络异常或连接中断'
      if (messages.value[assistantIdx].streaming) {
        messages.value[assistantIdx].content = messages.value[assistantIdx].content
          || '网络异常，请重试'
        messages.value[assistantIdx].streaming = false
      }
    },
    onClose: () => {
      loading.value = false
      streamHandle = null
    },
  }

  streamHandle = canvasApi.chatStream(
    props.caseId, props.clueId, q, chatMode.value, handlers)
}

/** 中断当前流式 */
function stopStream() {
  if (streamHandle) {
    streamHandle.close()
    streamHandle = null
    loading.value = false
  }
}

function scrollMessagesToBottom() {
  const el = document.querySelector('.cc-messages')
  if (el) el.scrollTop = el.scrollHeight
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
      <div class="cc-mode-switch">
        <NRadioGroup v-model:value="chatMode" size="small" name="cc-mode">
          <NRadioButton value="direct">单轮</NRadioButton>
          <NRadioButton value="react">ReAct</NRadioButton>
        </NRadioGroup>
      </div>
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
        <div class="cc-bubble" :class="{ streaming: m.streaming }">
          <template v-if="m.role === 'assistant' && m.facts">
            <!-- ReAct 思考过程（折叠） -->
            <details v-if="m.thinking" class="cc-thinking" open>
              <summary>思考过程</summary>
              <div class="cc-thinking-text">{{ m.thinking }}</div>
            </details>
            <!-- ReAct 工具调用列表 -->
            <div v-if="m.toolCalls?.length" class="cc-tools">
              <span class="cc-sec-title">工具调用</span>
              <div class="cc-tool-list">
                <NTag
                  v-for="(t, j) in m.toolCalls"
                  :key="t.id || j"
                  size="small"
                  :type="t.state ? 'success' : 'info'"
                  round
                >
                  {{ t.name }}
                  <span v-if="t.state" class="cc-tool-state">✓</span>
                  <span v-else class="cc-tool-pending">…</span>
                </NTag>
              </div>
            </div>
            <!-- 工具确定性产物标头（如十段式研判报告，正文不经引用分流） -->
            <div v-if="m.artifact" class="cc-artifact-head">
              <NTag size="small" type="success" round>
                {{ m.artifact.type_name }}
              </NTag>
              <span class="cc-artifact-src">{{ m.artifact.tool }} 确定性产物</span>
              <NTag v-if="m.artifact.degraded" size="small" type="warning" round>
                含降级声明
              </NTag>
            </div>
            <!-- 最终答案（content）：Markdown 渲染 + 引用标记可点击 -->
            <div
              v-if="m.content"
              class="cc-answer"
              :class="{ 'cc-answer-artifact': m.artifact }"
              @click="handleAnswerClick"
              v-html="renderMarkdownWithCites(m.content)"
            />
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
        <NSpin size="small" />
        <span>正在生成回答…</span>
        <NButton size="tiny" quaternary type="error" @click="stopStream">停止</NButton>
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
        :disabled="loading"
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
.cc-mode-switch {
  margin-left: auto;
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
.cc-thinking {
  margin-bottom: 6px;
  padding: 4px 6px;
  border-left: 2px solid var(--sun-border);
  font-size: 12px;
  color: var(--sun-text-tertiary);
}
.cc-thinking summary {
  cursor: pointer;
  font-weight: 500;
  color: var(--sun-text-secondary);
}
.cc-thinking-text {
  margin-top: 4px;
  white-space: pre-wrap;
  font-style: italic;
}
.cc-tools {
  margin-bottom: 6px;
}
.cc-tool-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}
.cc-tool-state {
  margin-left: 2px;
  color: var(--sun-success, #18a058);
}
.cc-tool-pending {
  margin-left: 2px;
  opacity: 0.6;
}
.cc-artifact-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 8px;
  padding-bottom: 6px;
  border-bottom: 1px dashed var(--sun-border);
}
.cc-artifact-src {
  font-size: 11.5px;
  color: var(--sun-text-secondary);
}
.cc-answer {
  line-height: 1.7;
  word-break: break-word;
}
/* 报告产物：标题/表格更紧凑，区分于普通问答 */
.cc-answer-artifact {
  font-size: 12.5px;
}
.cc-answer-artifact :deep(.md-h2) {
  font-size: 14px;
  margin: 12px 0 6px;
}
.cc-answer :deep(.md-h2) {
  font-size: 15px;
  font-weight: 700;
  margin: 10px 0 6px;
}
.cc-answer :deep(.md-h3) {
  font-size: 14px;
  font-weight: 600;
  margin: 8px 0 4px;
}
.cc-answer :deep(.md-h4) {
  font-size: 13px;
  font-weight: 600;
  margin: 6px 0 3px;
}
.cc-answer :deep(.md-ul),
.cc-answer :deep(.md-ol) {
  margin: 4px 0 6px 18px;
  padding: 0;
}
.cc-answer :deep(.md-ul) {
  list-style: disc;
}
.cc-answer :deep(.md-ol) {
  list-style: decimal;
}
.cc-answer :deep(.md-ul li),
.cc-answer :deep(.md-ol li) {
  margin-bottom: 3px;
}
.cc-answer :deep(strong) {
  font-weight: 600;
  color: var(--sun-text-primary);
}
.cc-answer :deep(.md-code) {
  font-family: var(--sun-font-mono, 'JetBrains Mono', monospace);
  font-size: 11.5px;
  background: var(--sun-bg-code, rgba(255, 255, 255, 0.07));
  padding: 1px 4px;
  border-radius: 3px;
}
.cc-answer :deep(.md-pre) {
  background: var(--sun-bg-code, rgba(255, 255, 255, 0.07));
  padding: 8px 10px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 6px 0;
}
.cc-answer :deep(.md-pre code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  background: none;
  padding: 0;
}
.cc-answer :deep(.md-table) {
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
  margin: 6px 0;
}
.cc-answer :deep(.md-table th),
.cc-answer :deep(.md-table td) {
  border: 1px solid var(--sun-border);
  padding: 3px 6px;
  text-align: left;
}
.cc-answer :deep(.md-table th) {
  background: var(--sun-bg-elevated);
  font-weight: 600;
}
.cc-answer :deep(.md-hr) {
  border: none;
  border-top: 1px dashed var(--sun-border);
  margin: 8px 0;
}
.cc-answer :deep(.md-quote) {
  border-left: 3px solid var(--sun-border);
  padding-left: 8px;
  margin: 4px 0;
  color: var(--sun-text-secondary);
  font-size: 12px;
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
.cc-cite,
.cc-answer :deep(.cc-cite) {
  display: inline-flex;
  align-items: center;
  margin: 0 2px 0 3px;
  padding: 0 5px;
  font-family: var(--sun-font-mono, monospace);
  font-size: 10.5px;
  line-height: 1.7;
  color: var(--sun-primary);
  background: var(--sun-bg-code, rgba(255, 255, 255, 0.07));
  border-radius: 4px;
  cursor: pointer;
  text-decoration: none;
  white-space: nowrap;
  transition: opacity 0.15s ease;
}
.cc-cite:hover,
.cc-answer :deep(.cc-cite:hover) {
  opacity: 0.75;
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
.cc-bubble.streaming::after {
  content: '▋';
  animation: cc-blink 1s infinite;
  margin-left: 2px;
}
@keyframes cc-blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
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
