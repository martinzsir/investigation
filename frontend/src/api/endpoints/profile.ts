import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import { adaptProfile, type ProfileData, type ProfileResponse } from '../../domain/profile'

// FE-P-007 数据画像：后端 GET /cases/{cid}/profiles 已实现（profiles_view.assemble_profiles）。
// 返回六层报告结构（l1_l2/l3/l4/l5），前端 adaptProfile 适配为 UI 数据。
// 案件未 BUILD → available:false + 空态「尚未接入数据源」。

export const profileApi = {
  async get(caseId: string): Promise<ProfileData> {
    const res = await api.get<ProfileResponse>(
      `/cases/${encodeURIComponent(caseId)}/profiles`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return adaptProfile(res.data)
  },
}
