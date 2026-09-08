/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API 基址（D8：默认相对路径 /api/v1，发布即冻结） */
  readonly VITE_API_BASE?: string
  /** '1' 时启用 MSW 兜底（契约先行演示/无后端联调；真实联调不开启） */
  readonly VITE_USE_MSW?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
