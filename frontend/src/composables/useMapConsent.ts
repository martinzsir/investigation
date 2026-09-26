// 在线地图授权闸口（PLAN-GEO-001 P4 §6.3）。
//
// 纪律：默认隔离——未授权时任何渲染器都不得注入 <script>/瓦片请求；
// 用户显式选择引擎后持久化到 localStorage，可随时撤销（撤销即销毁地图实例，
// 回纯离线 SVG）。本状态只管「浏览器渲染外联」这一件事，与内核
// SUNZI_NETWORK=isolated（服务端/LLM/编码脚本出口）解耦：瓦片是用户本人
// 的浏览行为，不经过内核。
import { computed, ref } from 'vue'

export type MapEngine = 'offline' | 'amap' | 'leaflet'

const STORAGE_KEY = 'sunzi.geo.map-engine'

function restore(): MapEngine {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'amap' || v === 'leaflet' ? v : 'offline'
  } catch {
    return 'offline'
  }
}

// 模块级单例：跨视图/路由切换保持授权态
const engine = ref<MapEngine>(restore())

export function useMapConsent() {
  /** 高德 JS API Key 已配置（构建期 env；未配置则高德引擎不可选） */
  const amapKeyConfigured = computed(() => {
    // 优先专用 JS Key；旧部署只配 VITE_AMAP_KEY 时回退
    const k = (import.meta.env.VITE_AMAP_JS_KEY
      ?? import.meta.env.VITE_AMAP_KEY ?? '').trim()
    return k.length > 0
  })

  const current = computed(() => engine.value)
  const isOnline = computed(() => engine.value !== 'offline')

  function grant(next: Exclude<MapEngine, 'offline'>): void {
    if (next === 'amap' && !amapKeyConfigured.value) return // 无 Key 硬不给切
    engine.value = next
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // 隐私模式等写入失败：仅本会话生效，不阻断
    }
  }

  function revoke(): void {
    engine.value = 'offline'
    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch {
      // 忽略
    }
  }

  return { engine: current, isOnline, amapKeyConfigured, grant, revoke }
}
