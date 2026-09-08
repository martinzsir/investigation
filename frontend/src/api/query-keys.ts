// FE-I-014 前置骨架：data_version 登记表。MVP-1 引入 TanStack Query 后由 query-keys
// 工厂消费（[cid, data_version] 缓存键 + SSE terminal 触发该案件全量失效）。
// MVP-0 仅登记版本号，不缓存任何业务数据。

const versions = new Map<string, number>()

export function noteDataVersion(caseId: string | null, version?: number): void {
  if (!caseId || typeof version !== 'number') return
  versions.set(caseId, version)
}

export function getDataVersion(caseId: string): number | undefined {
  return versions.get(caseId)
}

/** 切案件清该案件缓存；登出/全清不传参 */
export function clearCaseCache(caseId?: string): void {
  if (caseId) versions.delete(caseId)
  else versions.clear()
}
