/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API 基址（D8：默认相对路径 /api/v1，发布即冻结） */
  readonly VITE_API_BASE?: string
  /** '1' 时启用 MSW 兜底（契约先行演示/无后端联调；真实联调不开启） */
  readonly VITE_USE_MSW?: string
  /**
   * 高德 JS API Web 端 Key（PLAN-GEO-001 P4）。
   * 仅在用户显式授权后由浏览器动态加载 webapi.amap.com；不配时地理画像页
   * 回落 Leaflet + 高德栅格瓦片（同样需授权）或纯离线 SVG 示意。
   * 放 frontend/.env.local（*.local 已 gitignore，不进库）。
   * 优先读取；仅配了 VITE_AMAP_KEY 的旧部署回退使用它。
   */
  readonly VITE_AMAP_JS_KEY?: string
  /**
   * 高德 Web 服务 Key（REST，如地理编码）。与 JS API Key 是两个平台、
   * 不可混用；前端当前不直接消费，保留给脚本/后端读取。
   */
  readonly VITE_AMAP_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
