import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// 知识包（server/app/routers/knowledge.py 契约）。
// GET  /cases/{cid}/knowledge        断言列表 + 主体别名
// PUT  /cases/{cid}/knowledge    🔒  整体替换 relation_assertions（aliases 可选合并）
// POST /cases/{cid}/knowledge    🔒  追加断言（合并到现有列表）
// 注：敏感地点白名单后端暂无字段，MVP-4 不做。

export interface RelationAssertion {
  from: string
  to: string
  type: string
  source?: string
  /** ISO 日期或 null（null=长期有效；过期断言扫描自动排除） */
  valid_until: string | null
}

export interface KnowledgeDoc {
  relation_assertions: RelationAssertion[]
  /** 主体名 → 别名列表 */
  subject_aliases: Record<string, string[]>
  pack?: string
}

export interface KnowledgeSaveBody {
  relation_assertions: RelationAssertion[]
  subject_aliases?: Record<string, string[]>
  /** 变更理由（FE-T-012，落审计链 note） */
  reason?: string
}

export const knowledgeApi = {
  /** GET /cases/{cid}/knowledge */
  async list(caseId: string): Promise<KnowledgeDoc> {
    const res = await api.get<KnowledgeDoc>(
      `/cases/${encodeURIComponent(caseId)}/knowledge`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/knowledge 🔒 整体替换断言 */
  async save(caseId: string, body: KnowledgeSaveBody): Promise<{ saved: boolean; assertions: number }> {
    const res = await api.put<{ saved: boolean; assertions: number }>(
      `/cases/${encodeURIComponent(caseId)}/knowledge`,
      body,
      { idempotencyAction: 'knowledge-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST /cases/{cid}/knowledge 🔒 追加断言 */
  async add(caseId: string, body: KnowledgeSaveBody): Promise<{ added: number; total: number }> {
    const res = await api.post<{ added: number; total: number }>(
      `/cases/${encodeURIComponent(caseId)}/knowledge`,
      body,
      { idempotencyAction: 'knowledge-add' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
