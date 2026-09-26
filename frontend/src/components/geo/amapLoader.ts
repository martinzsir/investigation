// 高德 JS API 动态注入器（PLAN-GEO-001 P4 §6.3）。
//
// 唯一外联入口：只有用户在授权横幅显式选择高德引擎后，AmapMap 才会调用
// loadAmap()；未授权时本文件不会被 import 到（AmapMap 自身也在 v-if 之后）。
// 单例 + callback 幂等，重复调用/并发调用只注入一次 script。
/* eslint-disable @typescript-eslint/no-explicit-any */

declare global {
  interface Window {
    AMap?: any
    __sunziAmapInit?: () => void
  }
}

let loader: Promise<any> | null = null

export function isAmapLoaded(): boolean {
  return typeof window !== 'undefined' && !!window.AMap
}

export function loadAmap(key: string): Promise<any> {
  if (typeof window === 'undefined') {
    return Promise.reject(new Error('高德地图只能在浏览器环境加载'))
  }
  if (window.AMap) return Promise.resolve(window.AMap)
  if (loader) return loader

  loader = new Promise((resolve, reject) => {
    const cbName = '__sunziAmapInit'
    const timer = window.setTimeout(() => {
      cleanup()
      loader = null
      reject(new Error('高德 JS API 加载超时（网络不可达或 Key 无效）'))
    }, 15000)

    function cleanup(): void {
      window.clearTimeout(timer)
      try {
        delete (window as any)[cbName]
      } catch {
        (window as any)[cbName] = undefined
      }
    }

    window[cbName] = () => {
      if (window.AMap) {
        cleanup()
        resolve(window.AMap)
      } else {
        cleanup()
        loader = null
        reject(new Error('高德 JS API 回调未提供 AMap 对象'))
      }
    }

    const script = document.createElement('script')
    script.async = true
    script.src =
      'https://webapi.amap.com/maps?v=2.0'
      + `&key=${encodeURIComponent(key)}&callback=${cbName}`
    script.onerror = () => {
      cleanup()
      loader = null
      reject(new Error('高德 JS API 脚本加载失败（检查网络/Key 域名白名单）'))
      script.remove()
    }
    document.head.appendChild(script)
  })
  return loader
}
