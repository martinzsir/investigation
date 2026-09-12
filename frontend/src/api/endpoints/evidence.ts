import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import { EVIDENCE_MAX_SIZE, type EvidenceMaterial } from '../../domain/verify'

// REQ-V-010 书证上传/下载/清单 + REQ-V-011 挂接/解除
//（server/app/routers/evidence.py、clues.py 契约）：
//  - 书证文件与 uploads/ 数据源物理分离：落 cases/<cid>/evidence/；
//  - upload/list/download 为同步端点（upload 落审计链、download 记平台审计）；
//  - link/unlink 为 202 入队 TASK_VERIFY（op=link/unlink），终态由父层等待后刷新；
//  - 书证是人的行为：agent:* 后端 403 兜底，前端不在请求体内传身份。

export interface EvidencePage {
  items: EvidenceMaterial[]
}

/** REQ-V-010 上传成功回执（行子集） */
export type EvidenceUploadResult = EvidenceMaterial

const base = (caseId: string, clueId: string) =>
  `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}/evidence`

/** 触发浏览器保存 Blob（与 PackageView zip 下载同款 DOM 副作用，两处复用） */
export function saveBlobAs(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export const evidenceApi = {
  /** GET .../evidence —— 本线索书证清单（元数据，无 state.sqlite 时为空清单） */
  async list(caseId: string, clueId: string): Promise<EvidencePage> {
    const res = await api.get<EvidencePage>(base(caseId, clueId))
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * POST .../evidence —— multipart 同步上传（file + material_type + note）。
   * 不传幂等键：同文件重复上传即新材料（后端按内容生成独立 material_id）。
   * 20MB 上限后端 413；前端先做同阈值拦截给即时反馈。
   */
  async upload(
    caseId: string,
    clueId: string,
    file: File,
    materialType: string,
    note: string,
  ): Promise<EvidenceUploadResult> {
    if (file.size > EVIDENCE_MAX_SIZE) {
      throw new Error(`文件超过 20MB 上限（实际 ${(file.size / 1024 / 1024).toFixed(1)}MB）`)
    }
    const fd = new FormData()
    fd.append('file', file)
    fd.append('material_type', materialType)
    fd.append('note', note)
    const res = await api.post<EvidenceUploadResult>(
      base(caseId, clueId),
      fd,
      { timeoutKind: 'upload' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET .../evidence/{materialId}/download —— 原文件字节流（原名保存） */
  async download(
    caseId: string,
    clueId: string,
    materialId: string,
  ): Promise<Blob> {
    return api.getBlob(
      `${base(caseId, clueId)}/${encodeURIComponent(materialId)}/download`,
      { timeoutKind: 'upload' },
    )
  },

  /**
   * REQ-V-011 POST .../verify-items/{itemId}/evidence —— 挂接（202）。
   * 返回任务回执，终态等待由视图层编排（同 transition/replay 纪律）。
   */
  async link(
    caseId: string,
    clueId: string,
    itemId: string,
    materialId: string,
  ): Promise<{ id: string; status: string; task_type: string }> {
    const res = await api.post<{ id: string; status: string; task_type: string }>(
      `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}`
        + `/verify-items/${encodeURIComponent(itemId)}/evidence`,
      { material_id: materialId },
      { idempotencyAction: `verify-link:${itemId}:${materialId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** REQ-V-011 DELETE .../evidence/{materialId}/link —— 解除挂接（202，行保留） */
  async unlink(
    caseId: string,
    clueId: string,
    materialId: string,
  ): Promise<{ id: string; status: string; task_type: string }> {
    const res = await api.delete<{ id: string; status: string; task_type: string }>(
      `${base(caseId, clueId)}/${encodeURIComponent(materialId)}/link`,
      { idempotencyAction: `verify-unlink:${materialId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
