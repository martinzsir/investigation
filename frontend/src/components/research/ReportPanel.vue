<script setup lang="ts">
// M6 RC-304/305/306：研判报告面板。
// 生成报告（202 异步）、版本列表、正文阅读（引用角标可点）、导出 MD/Word。
import { ref, onMounted, onUnmounted, watch } from 'vue'
import {
  NButton, NSpin, NEmpty, NTag, NInput, NModal, useMessage,
} from 'naive-ui'
import { NIcon } from 'naive-ui'
import {
  DocumentOutline, CloseOutline, DownloadOutline,
  CopyOutline, RefreshOutline,
} from '@vicons/ionicons5'
import { canvasApi } from '../../api/endpoints/canvas'
import type {
  ReportListItem, ReportDetail,
} from '../../domain/canvas'

const props = defineProps<{
  caseId: string
  clueId: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'cite-click', ref: string): void
}>()

const message = useMessage()

const reports = ref<ReportListItem[]>([])
const loading = ref(false)
const generating = ref(false)
const selectedReport = ref<ReportDetail | null>(null)
const reportLoading = ref(false)
const showExtra = ref(false)
const extraRequest = ref('')
const pollingTimer = ref<number | null>(null)

const SECTION_TITLES: Record<string, string> = {
  overview: '一、线索概况',
  rules: '二、命中规则与判据',
  facts: '三、事实与依据',
  inferences: '四、研判推断',
  pending: '五、待核实事项',
  evidence: '六、书证清单',
  sources: '七、数据源清单',
  appendix: '附录：引用索引',
}

async function loadReports() {
  loading.value = true
  try {
    const res = await canvasApi.listReports(props.caseId, props.clueId)
    reports.value = res.reports
    // 自动选中第一个 ready 的
    const firstReady = reports.value.find((r) => r.status === 'ready')
    if (firstReady && !selectedReport.value) {
      await selectReport(firstReady.report_id)
    }
  } catch (e: any) {
    message.error(e?.message || '报告列表加载失败')
  } finally {
    loading.value = false
  }
}

async function selectReport(reportId: string) {
  reportLoading.value = true
  try {
    const res = await canvasApi.getReport(
      props.caseId, props.clueId, reportId)
    selectedReport.value = res.report
  } catch (e: any) {
    message.error(e?.message || '报告加载失败')
  } finally {
    reportLoading.value = false
  }
}

async function generate() {
  generating.value = true
  try {
    const extra = extraRequest.value.trim()
    await canvasApi.generateReport(
      props.caseId, props.clueId, extra || undefined)
    showExtra.value = false
    extraRequest.value = ''
    message.success('报告生成中，请稍后刷新查看')
    // 轮询任务终态
    startPolling()
  } catch (e: any) {
    message.error(e?.message || '报告生成失败')
  } finally {
    generating.value = false
  }
}

function startPolling() {
  if (pollingTimer.value) return
  pollingTimer.value = window.setInterval(async () => {
    await loadReports()
    // 如果有 generating 状态的报表变为 ready，停止轮询
    const hasGenerating = reports.value.some(
      (r) => r.status === 'generating')
    if (!hasGenerating && pollingTimer.value) {
      clearInterval(pollingTimer.value)
      pollingTimer.value = null
    }
  }, 3000)
}

function stopPolling() {
  if (pollingTimer.value) {
    clearInterval(pollingTimer.value)
    pollingTimer.value = null
  }
}

function handleContentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (target?.classList.contains('rp-cite')) {
    const ref = target.getAttribute('data-ref')
    if (ref) onCiteClick(ref)
  }
}

/** 渲染报告段落：将 [cite:xxx] 转为可点击角标 */
function renderSection(
  content: string,
  _report: { citations: Array<{ cite_id: number; ref: string; summary: string }> },
): string {
  if (!content) return '<span class="rp-empty">（本段无内容）</span>'
  let html = content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  html = html.replace(
    /\[cite:([^\[\]]+)\]/g,
    (_, ref) =>
      `<span class="rp-cite" data-ref="${ref}">[${ref}]</span>`,
  )
  html = html.replace(/\n/g, '<br>')
  return html
}

async function copyMarkdown() {
  if (!selectedReport.value) return
  try {
    await navigator.clipboard.writeText(
      selectedReport.value.content_md)
    message.success('已复制 Markdown 到剪贴板')
  } catch {
    message.error('复制失败，请手动选择文本')
  }
}

async function downloadMd() {
  if (!selectedReport.value) return
  try {
    const blob = await canvasApi.exportReportMd(
      props.caseId, props.clueId, selectedReport.value.report_id)
    triggerDownload(blob, `研判报告_v${selectedReport.value.version_no}.md`)
  } catch (e: any) {
    message.error(e?.message || 'Markdown 导出失败')
  }
}

async function downloadDocx() {
  if (!selectedReport.value) return
  try {
    const blob = await canvasApi.exportReportDocx(
      props.caseId, props.clueId, selectedReport.value.report_id)
    triggerDownload(
      blob, `研判报告_v${selectedReport.value.version_no}.docx`)
  } catch (e: any) {
    message.error(e?.message || 'Word 导出失败，请改用 Markdown 复制')
  }
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function onCiteClick(ref: string) {
  emit('cite-click', ref)
}

function close() {
  stopPolling()
  emit('close')
}

onMounted(() => {
  loadReports()
})

onUnmounted(() => {
  stopPolling()
})

watch(() => props.clueId, () => {
  selectedReport.value = null
  reports.value = []
  loadReports()
})
</script>

<template>
  <div class="report-panel" data-testid="report-panel">
    <div class="rp-header">
      <span class="rp-title">
        <NIcon :component="DocumentOutline" />
        研判报告
      </span>
      <NButton size="small" quaternary @click="close">
        <NIcon :component="CloseOutline" />
      </NButton>
    </div>

    <div class="rp-actions">
      <NButton
        size="small"
        type="primary"
        :loading="generating"
        data-testid="btn-generate-report"
        @click="showExtra = true"
      >
        <NIcon :component="DocumentOutline" />
        生成新版本
      </NButton>
      <NButton
        size="small"
        quaternary
        :loading="loading"
        @click="loadReports"
      >
        <NIcon :component="RefreshOutline" />
        刷新
      </NButton>
    </div>

    <!-- 版本列表 -->
    <div class="rp-list" v-if="reports.length > 0">
      <div
        v-for="r in reports"
        :key="r.report_id"
        class="rp-list-item"
        :class="{ active: selectedReport?.report_id === r.report_id }"
        @click="selectReport(r.report_id)"
      >
        <div class="rp-item-head">
          <span class="rp-ver">v{{ r.version_no }}</span>
          <NTag
            size="small"
            :type="r.status === 'ready' ? 'success'
              : r.status === 'generating' ? 'info'
              : r.status === 'failed' ? 'error' : 'default'"
          >
            {{ r.status }}
          </NTag>
          <span class="rp-date">{{ r.created_at?.slice(0, 16) }}</span>
        </div>
        <div class="rp-item-meta" v-if="r.status === 'ready'">
          <span v-if="r.warning_count > 0" class="rp-warn">
            {{ r.warning_count }} 条 warning
          </span>
          <span v-if="r.citation_count > 0">
            {{ r.citation_count }} 条引用
          </span>
        </div>
        <div class="rp-error" v-if="r.status === 'failed'">
          {{ r.error || '生成失败' }}
        </div>
      </div>
    </div>
    <NEmpty
      v-else-if="!loading"
      description="暂无报告，点击「生成新版本」创建第一份研判报告"
      style="padding: 40px 0"
    />
    <div v-else class="rp-loading">
      <NSpin size="small" />
    </div>

    <!-- 报告正文 -->
    <div class="rp-body" v-if="selectedReport">
      <div v-if="reportLoading" class="rp-loading">
        <NSpin size="small" />
      </div>
      <template v-else>
        <div
          v-for="(title, key) in SECTION_TITLES"
          :key="key"
          class="rp-section"
        >
          <h4 class="rp-section-title">{{ title }}</h4>
          <div
            class="rp-section-content"
            v-html="renderSection(selectedReport.sections[key] || '', selectedReport)"
            @click="handleContentClick"
          />
        </div>

        <!-- 导出按钮 -->
        <div class="rp-export" v-if="selectedReport.status === 'ready'">
          <NButton size="small" @click="copyMarkdown">
            <NIcon :component="CopyOutline" />
            复制 Markdown
          </NButton>
          <NButton size="small" @click="downloadMd">
            <NIcon :component="DownloadOutline" />
            下载 MD
          </NButton>
          <NButton size="small" @click="downloadDocx">
            <NIcon :component="DownloadOutline" />
            导出 Word
          </NButton>
        </div>
      </template>
    </div>

    <!-- 补充要求弹窗 -->
    <NModal
      v-model:show="showExtra"
      preset="dialog"
      title="生成研判报告"
      :show-icon="false"
      style="max-width: 500px"
    >
      <div style="padding: 12px 0">
        <p style="margin-bottom: 8px; color: var(--sun-text-secondary)">
          可选：补充要求（≤300字），将透传给报告生成模型
        </p>
        <NInput
          v-model:value="extraRequest"
          type="textarea"
          :rows="3"
          placeholder="如：重点关注资金流向异常"
          :maxlength="300"
        />
      </div>
      <template #action>
        <NButton size="small" @click="showExtra = false">取消</NButton>
        <NButton
          size="small"
          type="primary"
          :loading="generating"
          @click="generate"
        >
          生成
        </NButton>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.report-panel {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 480px;
  max-height: calc(100% - 16px);
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border-light);
  border-radius: 8px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  z-index: 200;
}

.rp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--sun-border-light);
}

.rp-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 15px;
}

.rp-actions {
  display: flex;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--sun-border-light);
}

.rp-list {
  max-height: 200px;
  overflow-y: auto;
  border-bottom: 1px solid var(--sun-border-light);
}

.rp-list-item {
  padding: 8px 16px;
  cursor: pointer;
  border-bottom: 1px solid var(--sun-border-lighter);
  transition: background 0.15s;
}

.rp-list-item:hover {
  background: var(--sun-bg-hover);
}

.rp-list-item.active {
  background: var(--sun-bg-active);
}

.rp-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.rp-ver {
  font-weight: 600;
}

.rp-date {
  color: var(--sun-text-tertiary);
  font-size: 12px;
  margin-left: auto;
}

.rp-item-meta {
  display: flex;
  gap: 12px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}

.rp-warn {
  color: var(--sun-warning);
}

.rp-error {
  color: var(--sun-error);
  font-size: 12px;
  margin-top: 4px;
}

.rp-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

.rp-loading {
  display: flex;
  justify-content: center;
  padding: 24px;
}

.rp-section {
  margin-bottom: 16px;
}

.rp-section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 6px 0;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--sun-border-lighter);
}

.rp-section-content {
  font-size: 13px;
  line-height: 1.6;
  color: var(--sun-text-primary);
}

.rp-empty {
  color: var(--sun-text-tertiary);
  font-style: italic;
}

.rp-export {
  display: flex;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--sun-border-light);
}

:deep(.rp-cite) {
  display: inline-flex;
  align-items: center;
  padding: 0 4px;
  font-size: 11px;
  background: var(--sun-tag-bg);
  border-radius: 3px;
  cursor: pointer;
  color: var(--sun-primary);
}

:deep(.rp-cite:hover) {
  background: var(--sun-primary);
  color: white;
}
</style>
