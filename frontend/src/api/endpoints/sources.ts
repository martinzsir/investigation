import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { TaskRow } from '../../domain/task'
import type { AnalyzeResult, SourceColumn } from '../../domain/mapping'

// 数据接入端点（server/app/routers/ingest.py 契约）。
// 上传 multipart（timeout=upload 120s）；导入指纹幂等（同文件名+SHA256+行数 → 409）；
// 导入需 clearance≥2（后端 403）；导入返回 task_dto（200，链式 BUILD）。

/** SQLite 库内表（upload 时枚举，供向导选择读哪张表） */
export interface SqliteTableInfo {
  name: string
  rows: number
  columns: string[]
}

export interface UploadResult {
  upload_id: string
  filename: string
  format: string
  fingerprint: string
  rows: number
  columns: SourceColumn[]
  /** 塌缩预警：1 行 1 列且单元格疑似 JSON（嵌套包裹/分隔符不匹配），无预警为 null */
  warning?: string | null
  /** SQLite 库内全部表（仅 format=sqlite 返回；其余格式为 []） */
  sqlite_tables?: SqliteTableInfo[]
  /** declared_source_tables()：{表名: [声明原始列...]} */
  declared_tables: Record<string, string[]>
  status: string
}

export interface SourceItem {
  upload_id: string
  filename: string
  format: string
  fingerprint: string
  rows: number
  status: string
  table_name: string
  mapping_json: Record<string, unknown>
  created_by: string
  created_at: string
}

export interface ImportBody {
  target_table: string
  /** {源列: 声明列}（rename 视角） */
  column_map: Record<string, string>
  clean?: string[]
  /** SQLite：读取库内哪张表（缺省后端取首表） */
  sqlite_table?: string
}

export const sourcesApi = {
  /** POST /cases/{cid}/sources/upload（multipart；返回 upload_id + 指纹 + 行数 + 列画像） */
  async upload(caseId: string, file: File): Promise<UploadResult> {
    const fd = new FormData()
    fd.append('file', file)
    const res = await api.post<UploadResult>(
      `/cases/${encodeURIComponent(caseId)}/sources/upload`,
      fd,
      { timeoutKind: 'upload', idempotencyAction: `source-upload:${file.name}:${file.size}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/sources —— 数据源注册表（staged/queued/imported） */
  async list(caseId: string): Promise<{ items: SourceItem[]; total: number }> {
    const res = await api.get<{ items: SourceItem[]; total: number }>(
      `/cases/${encodeURIComponent(caseId)}/sources`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/sources/{uid}/analyze —— 同步只读列分析（不产任务） */
  async analyze(
    caseId: string,
    uploadId: string,
    targetTable?: string,
    sqliteTable?: string,
  ): Promise<AnalyzeResult> {
    const body: Record<string, string> = {}
    if (targetTable) body.target_table = targetTable
    if (sqliteTable) body.sqlite_table = sqliteTable
    const res = await api.post<AnalyzeResult>(
      `/cases/${encodeURIComponent(caseId)}/sources/${encodeURIComponent(uploadId)}/analyze`,
      body,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/sources/{uid} —— 映射草稿保存（仅 staged/queued 可改） */
  async saveMapping(
    caseId: string,
    uploadId: string,
    body: {
      target_table: string
      mapping: Record<string, string>
      notes?: string
      sqlite_table?: string
    },
  ): Promise<SourceItem> {
    const res = await api.put<SourceItem>(
      `/cases/${encodeURIComponent(caseId)}/sources/${encodeURIComponent(uploadId)}`,
      body,
      { idempotencyAction: `source-mapping:${uploadId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * POST /cases/{cid}/sources/{uid}/import 🔒⚡
   * 指纹幂等（重复 409 CONFLICT）→ 入队 TASK_IMPORT；返回任务（200）。
   */
  async import(caseId: string, uploadId: string, body: ImportBody): Promise<TaskRow> {
    const res = await api.post<TaskRow>(
      `/cases/${encodeURIComponent(caseId)}/sources/${encodeURIComponent(uploadId)}/import`,
      {
        target_table: body.target_table,
        column_map: body.column_map,
        clean: body.clean ?? [],
        ...(body.sqlite_table ? { sqlite_table: body.sqlite_table } : {}),
      },
      { idempotencyAction: `source-import:${uploadId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
