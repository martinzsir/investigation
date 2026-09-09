// 分页与 URL query 同步（FE-C-003 DataTable：?page= 可分享、跳页输入框、page_size=50）。
// 纯函数：页码解析/钳制/跳页校验/query 序列化，组件只做薄编排。

/** MVP-2 验收：默认每页 50 条（非虚拟滚动，分页器 + 跳页） */
export const PAGE_SIZE_DEFAULT = 50

/** 总页数（total=0 时为 1，便于页码钳制） */
export function totalPages(total: number, pageSize: number = PAGE_SIZE_DEFAULT): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / pageSize))
}

/** 页码钳制到 [1, totalPages] */
export function clampPage(page: number, total: number, pageSize: number = PAGE_SIZE_DEFAULT): number {
  const pages = totalPages(total, pageSize)
  if (!Number.isFinite(page) || page < 1) return 1
  return Math.min(Math.floor(page), pages)
}

/**
 * 从 route.query 值解析页码：非法/缺省回落 1。
 * 接受 string | string[] | null | undefined（vue-router query 类型）。
 */
export function parsePageQuery(raw: unknown): number {
  const v = Array.isArray(raw) ? raw[0] : raw
  if (v === undefined || v === null || v === '') return 1
  const n = Number.parseInt(String(v), 10)
  return Number.isFinite(n) && n >= 1 ? n : 1
}

/** 跳页输入框校验：返回错误文案（'' = 合法） */
export function jumpPageError(input: string, total: number, pageSize: number = PAGE_SIZE_DEFAULT): string {
  const t = input.trim()
  if (!t) return '请输入页码'
  if (!/^\d+$/.test(t)) return '页码须为正整数'
  const n = Number.parseInt(t, 10)
  if (n < 1) return '页码须 ≥ 1'
  if (n > totalPages(total, pageSize)) return `超出总页数（共 ${totalPages(total, pageSize)} 页）`
  return ''
}

/** 生成可分享 URL query：page=1 时省略（保持链接干净） */
export function pageQuery(page: number, pageSize: number = PAGE_SIZE_DEFAULT): Record<string, string> {
  const q: Record<string, string> = { page_size: String(pageSize) }
  if (page > 1) q.page = String(page)
  return q
}
