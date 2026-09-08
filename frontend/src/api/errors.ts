// 错误码映射：前端唯一事实源（FE-I-005，决策 13 按 envelope.py 实际 7 码 + 客户端网络态）。
// 任何组件不得自行解读 httpStatus，一律 import 本模块的映射函数。
export type ErrorCode =
  | 'UNAUTHORIZED'
  | 'FORBIDDEN'
  | 'NOT_FOUND'
  | 'VALIDATION'
  | 'CONFLICT'
  | 'DEGRADED_WRITE_REJECTED'
  | 'INTERNAL'
  | 'NETWORK'

/** 红线八：登录失败统一文案——不区分账号/密码/双因子失败，不提示账号是否存在 */
export const LOGIN_FAIL_TEXT = '账号或密码错误'

/** 404 统一文案（跨租户探测同样 404，防存在性泄漏） */
export const NOT_FOUND_TEXT = '不存在或无权访问'

/** FE-I-015：服务端不可达全局异常态 */
export const NETWORK_DOWN_TEXT = '无法连接服务端'

export class ApiError extends Error {
  readonly code: ErrorCode
  readonly httpStatus: number
  readonly detail?: unknown

  constructor(code: ErrorCode, message: string, httpStatus: number, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.httpStatus = httpStatus
    this.detail = detail
  }
}

export type ErrorKind =
  | 'auth'
  | 'permission'
  | 'notfound'
  | 'validation'
  | 'conflict'
  | 'degraded'
  | 'internal'
  | 'network'

export interface ErrorPresentation {
  kind: ErrorKind
  title: string
  /** UNAUTHORIZED 业务态：清会话跳 /login（来源路径由调用方保留） */
  redirectToLogin: boolean
  /** 降级写拒绝 / 网络异常：禁用导出/立案入口（FE-T-010） */
  degradeWrites: boolean
  /** 幂等键命中（409）：跳转既有任务，不重复提交 */
  reuseTask: boolean
}

const PRESENTATIONS: Record<ErrorCode, ErrorPresentation> = {
  UNAUTHORIZED: {
    kind: 'auth',
    title: '登录状态已失效，请重新登录',
    redirectToLogin: true,
    degradeWrites: false,
    reuseTask: false,
  },
  FORBIDDEN: {
    kind: 'permission',
    title: '无权访问',
    redirectToLogin: false,
    degradeWrites: false,
    reuseTask: false,
  },
  NOT_FOUND: {
    kind: 'notfound',
    title: NOT_FOUND_TEXT,
    redirectToLogin: false,
    degradeWrites: false,
    reuseTask: false,
  },
  VALIDATION: {
    kind: 'validation',
    title: '参数校验失败',
    redirectToLogin: false,
    degradeWrites: false,
    reuseTask: false,
  },
  CONFLICT: {
    kind: 'conflict',
    title: '任务冲突',
    redirectToLogin: false,
    degradeWrites: false,
    reuseTask: true,
  },
  DEGRADED_WRITE_REJECTED: {
    kind: 'degraded',
    title: '降级运行中，该操作已被拒绝',
    redirectToLogin: false,
    degradeWrites: true,
    reuseTask: false,
  },
  INTERNAL: {
    kind: 'internal',
    title: '系统内部错误，请重试',
    redirectToLogin: false,
    degradeWrites: false,
    reuseTask: false,
  },
  NETWORK: {
    kind: 'network',
    title: NETWORK_DOWN_TEXT,
    redirectToLogin: false,
    degradeWrites: true,
    reuseTask: false,
  },
}

/** 唯一映射入口：组件只消费本函数结果，不自行判断状态码 */
export function presentError(err: unknown): ErrorPresentation {
  if (err instanceof ApiError) return PRESENTATIONS[err.code]
  return PRESENTATIONS.NETWORK
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError
}
