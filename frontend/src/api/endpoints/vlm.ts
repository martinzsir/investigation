import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// P8 多模态图像研判（server/app/routers/vlm.py 契约）：
//  - POST /cases/{cid}/vlm/draft：人显式触发 VLM 分析，成功只产
//    image_draft 提案（模型直入生产=0）；降级/阻断 200 {ok:false,...}；
//  - GET  /cases/{cid}/vlm/drafts：列草案 + stale（TTL 派生，不改状态）；
//  - POST /cases/{cid}/vlm/drafts/{pid}/verify：人比对原件核验通过，
//    写 state.image_evidence（入图/入报告）+ 提案 approve。

export type VlmContentClass = 'invoice' | 'receipt' | 'document' | 'other'
export type VlmSubjectType = 'person' | 'org' | 'bid_project'
export type VlmSeverity = 'info' | 'warn'

/** POST draft：后端 draft_image_inspect 返回壳（透传，按档位渲染） */
export interface VlmDraftProposal {
  proposal_id: string
  title: string
  detail: string
  severity: VlmSeverity
  model_score: number | null
  stale: boolean
  model: string
  prompt_version: string
}

export interface VlmDraftResult {
  ok: boolean
  degraded?: boolean
  blocked?: boolean
  mode: string
  model: string | null
  log_id?: string | null
  reason?: string
  error?: string
  proposals: VlmDraftProposal[]
  dropped: { index: number; reason: string }[]
}

/** GET drafts：提案记录 + stale 派生 */
export interface VlmDraftRecord {
  proposal_id: string
  kind: 'image_draft'
  case_id: string
  status: string
  author: string
  created_at: string
  stale: boolean
  payload: {
    candidate: { title: string; detail: string; severity: string }
    input: {
      origin: string
      purpose: string
      mode: string
      model: string
      prompt_version: string
      image_uri: string
      content_class: string
      subject_type: string
      subject_id: string
      exif_stripped: boolean
      created_epoch: number
      ttl_s: number
    }
    _sort_hint?: { model: string; model_score: number }
  }
}

/** POST verify：核验回执 */
export interface VlmImageEvidence {
  image_evidence_id: string
  image_uri: string
  model: string
  prompt_version: string
  model_score: number | null
  verifier: string
  verify_conclusion: string
  subject_type: VlmSubjectType
  subject_id: string
  clue_id: string
  created_at: string
}

export interface VlmVerifyResult {
  proposal_id: string
  status: string
  image_evidence: VlmImageEvidence
}

export interface VlmDraftRequest {
  image_uri: string
  content_class: VlmContentClass
  instruction?: string
  prompt_version?: string
  subject_type?: VlmSubjectType
  subject_id?: string
}

/** GET findings：材料卡待核草案（pending 投影） */
export interface VlmFindingPending {
  proposal_id: string
  title: string
  detail: string
  severity: string
  image_uri: string
  model: string
  model_score: number | null
  stale: boolean
  created_at: string
}

/** GET findings：材料卡已人验证据（verified 投影） */
export interface VlmFindingVerified {
  image_evidence_id: string
  title: string
  detail: string
  severity: string
  image_uri: string
  model: string
  model_score: number | null
  verifier: string
  verify_conclusion: string
  subject_type: string
  subject_id: string
  clue_id: string
  created_at: string
}

export interface VlmMaterialFindings {
  pending: VlmFindingPending[]
  verified: VlmFindingVerified[]
}

/** GET /cases/{cid}/vlm/findings?clue_id= — 按 material_id 分组 */
export interface VlmFindingsPage {
  findings: Record<string, VlmMaterialFindings>
}

export const vlmApi = {
  /** POST .../vlm/draft —— 发起分析（同步；成功只落提案） */
  async draft(caseId: string, req: VlmDraftRequest): Promise<VlmDraftResult> {
    const res = await api.post<VlmDraftResult>(
      `/cases/${encodeURIComponent(caseId)}/vlm/draft`,
      req,
      { idempotencyAction: `vlm-draft:${req.image_uri}:${req.content_class}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET .../vlm/drafts —— 列草案（含 stale） */
  async list(caseId: string): Promise<VlmDraftRecord[]> {
    const res = await api.get<{ drafts: VlmDraftRecord[] }>(
      `/cases/${encodeURIComponent(caseId)}/vlm/drafts`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data.drafts
  },

  /**
   * GET .../vlm/findings?clue_id= —— 书证材料卡图像 findings
   * （按 material_id 分组：pending 待核草案 + verified 已人验证据）。
   * 线索归属由材料决定：clue_id 取该线索书证清单。
   */
  async findings(caseId: string, clueId: string): Promise<VlmFindingsPage> {
    const res = await api.request<VlmFindingsPage>({
      method: 'GET',
      path: `/cases/${encodeURIComponent(caseId)}/vlm/findings`,
      query: { clue_id: clueId },
    })
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** POST .../vlm/drafts/{pid}/verify —— 人验通过（入图/入报告） */
  async verify(
    caseId: string,
    proposalId: string,
    body: {
      verify_conclusion: string
      subject_type?: VlmSubjectType
      subject_id?: string
      clue_id?: string
    },
  ): Promise<VlmVerifyResult> {
    const res = await api.post<VlmVerifyResult>(
      `/cases/${encodeURIComponent(caseId)}/vlm/drafts/`
        + `${encodeURIComponent(proposalId)}/verify`,
      body,
      { idempotencyAction: `vlm-verify:${proposalId}` },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
