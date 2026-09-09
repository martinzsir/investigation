import { api } from '../client'

// 案件包（server/app/routers/package.py + worker/package.py 契约）。
// - export：POST 无 body → {task_id}（EXPORT 任务，走任务中心 SSE）；
// - download：GET zip 二进制流（FileResponse，非 JSON 信封）→ Blob；
// - verify：multipart 单字段 file → 七步清单 + chain_ok + 敏感文件名单（A1）；
// - import：multipart file + 表单字段 case_id/name → 预校验失败 422/重号 409 → {task_id}。

export interface VerifyStep {
  key: 'format' | 'manifest' | 'hash' | 'declarations' | 'schema' | 'chain' | 'duckdb'
  label: string
  /** pass/warn/fail；warn（chain 不完整等）不阻断 */
  status: 'pass' | 'warn' | 'fail'
  detail?: string
}

export interface VerifyResult {
  ok: boolean
  errors: string[]
  /** 审计链完整性（false=橙色告警，不阻断） */
  chain_ok: boolean
  file_count: number
  steps: VerifyStep[]
  /** 敏感文件名单（case_knowledge.json 等，导入侧红框） */
  sensitive_files: string[]
}

export interface TaskIdResult {
  task_id: string
}

export const packageApi = {
  /** POST /cases/{cid}/package/export（无 body；导出人=会话 operator） */
  async exportCase(caseId: string): Promise<string> {
    const { data } = await api.post<TaskIdResult>(
      `/cases/${encodeURIComponent(caseId)}/package/export`,
      undefined,
      { idempotencyAction: `package-export:${caseId}` },
    )
    return data.task_id
  },

  /** GET /packages/{taskId}/download —— zip 二进制流（Blob；失败回退 JSON 错误信封） */
  async download(taskId: string): Promise<Blob> {
    return api.getBlob(`/packages/${encodeURIComponent(taskId)}/download`, {
      timeoutKind: 'upload',
    })
  },

  /** POST /packages/verify（multipart；同步校验，不入队） */
  async verify(file: File): Promise<VerifyResult> {
    const form = new FormData()
    form.append('file', file)
    const { data } = await api.post<VerifyResult>('/packages/verify', form, {
      timeoutKind: 'upload',
    })
    return data
  },

  /**
   * POST /packages/import（multipart file + 表单字段 case_id/name）。
   * 预校验失败 422 / case_id 重号 409（ApiError 带文案）；通过 → {task_id}。
   */
  async importPackage(file: File, caseId: string, name: string): Promise<string> {
    const form = new FormData()
    form.append('file', file)
    form.append('case_id', caseId)
    form.append('name', name)
    const { data } = await api.post<TaskIdResult>('/packages/import', form, {
      timeoutKind: 'upload',
      idempotencyAction: `package-import:${caseId}`,
    })
    return data.task_id
  },
}
