import { defineStore } from 'pinia'
import { casesApi, type CaseDto } from '../api/endpoints/cases'
import { clearCaseCache } from '../api/query-keys'

const KEY = 'sunzi.case'

function restoreCurrent(): string {
  try {
    return sessionStorage.getItem(KEY) ?? ''
  } catch {
    return ''
  }
}

export const useCaseStore = defineStore('case', {
  state: () => ({
    cases: [] as CaseDto[],
    currentCaseId: restoreCurrent(),
    loading: false,
  }),
  getters: {
    currentCase: (s) => s.cases.find((c) => c.id === s.currentCaseId) ?? null,
  },
  actions: {
    async loadCases(): Promise<void> {
      this.loading = true
      try {
        this.cases = await casesApi.list()
        // 当前选择已不在可见列表（会话/租户变化）→ 归零回"全部案件"
        if (this.currentCaseId && !this.cases.some((c) => c.id === this.currentCaseId)) {
          this.selectCase('')
        }
      } finally {
        this.loading = false
      }
    },

    /** '' = 全部案件（FE-C-025：含回 23b 案件门户语义）。切案件清案件缓存（FE-I-014） */
    selectCase(id: string): void {
      this.currentCaseId = id
      try {
        if (id) sessionStorage.setItem(KEY, id)
        else sessionStorage.removeItem(KEY)
      } catch {
        // 忽略
      }
      clearCaseCache(id || undefined)
    },

    /** 登出调用：案件列表与缓存全清 */
    clearAll(): void {
      this.cases = []
      this.selectCase('')
      clearCaseCache()
    },
  },
})
