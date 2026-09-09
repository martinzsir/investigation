import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type { ProfileData } from '../../domain/profile'

// FE-P-007 数据画像：后端 GET /cases/{cid}/profiles 待补（⛔），当前由 MSW mock 供演示；
// 真后端 404/空数据时页面走「尚未接入数据源」空态（非红线功能，允许 mock）。

export const profileApi = {
  async get(caseId: string): Promise<ProfileData> {
    const res = await api.get<ProfileData>(
      `/cases/${encodeURIComponent(caseId)}/profiles`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
