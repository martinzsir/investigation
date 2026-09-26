<script setup lang="ts">
// 高德 JS API 适配器（PLAN-GEO-001 P4）。
// 仅在用户授权高德引擎后由父级 v-if 挂载；挂载时才动态注入 webapi.amap.com
// 脚本（见 amapLoader）。GCJ-02 坐标直叠，不做任何坐标转换。
// 渲染：概率面 Polygon 五档填色 + 落脚点 CircleMarker + 顶格区高亮环。
/* eslint-disable @typescript-eslint/no-explicit-any */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import type { GeoLayerModel, GeoSite } from '../../domain/geoMap'
import { boundsOf, probabilityBand } from '../../domain/geoMap'
import { geoMapTokens } from '../../design/tokens'
import { loadAmap } from './amapLoader'

const props = defineProps<{
  model: GeoLayerModel
  layersVisible: { cells: boolean; sites: boolean; top: boolean }
}>()
const emit = defineEmits<{
  selectSite: [site: GeoSite]
  failed: [message: string]
}>()

const elRef = ref<HTMLDivElement | null>(null)
const loading = ref(true)
const errorMsg = ref('')

let map: any = null
let info: any = null
let overlays: any[] = []
// 与 geoMapTokens.band 同序的不透明度（AMap fillColor 只吃纯色 + fillOpacity）
const bandOpacity = [0.1, 0.22, 0.3, 0.38, 0.52]

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string
  ))
}

function clearOverlays(): void {
  if (map && overlays.length) {
    map.remove(overlays)
    overlays = []
  }
}

function openSite(s: GeoSite): void {
  if (s.lat === null || s.lng === null || !info) return
  const coord = s.coordDegraded
    ? '<span style="color:#F28B93">无坐标（文本降级点）</span>'
    : `${s.lat.toFixed(6)}, ${s.lng.toFixed(6)}`
  info.setContent(
    `<div style="min-width:180px;color:#E9F7FA;font-size:12px;line-height:1.7">
       <div style="font-weight:600;margin-bottom:2px">${esc(s.stdAddress)}</div>
       <div style="color:#9FB8C6">到访 <b style="color:#F2B54D">${s.visits}</b> 次
         ｜${s.firstDate ?? '—'} ~ ${s.lastDate ?? '—'}</div>
       <div style="color:#9FB8C6">${esc(s.adminPath || '无区划')}</div>
       <div style="color:#9FB8C6;font-family:monospace">${coord}</div>
     </div>`,
  )
  info.open(map, [s.lng, s.lat])
  emit('selectSite', s)
}

function draw(): void {
  const AMap = window.AMap
  if (!map || !AMap) return
  clearOverlays()
  const next: any[] = []

  if (props.layersVisible.cells) {
    for (const c of props.model.cells) {
      const b = probabilityBand(c.probability)
      const tok = geoMapTokens.band[b]
      next.push(new AMap.Polygon({
        path: c.ring.map(([lng, lat]) => [lng, lat]),
        strokeColor: tok.stroke,
        strokeWeight: 1,
        strokeOpacity: 0.9,
        fillColor: tok.stroke,
        fillOpacity: bandOpacity[b],
        bubble: true,
        clickable: false,
        zIndex: 20 + b,
      }))
    }
  }

  if (props.layersVisible.sites) {
    for (const s of props.model.sites) {
      if (s.lat === null || s.lng === null) continue
      const tok = s.coordDegraded ? geoMapTokens.siteDegraded : geoMapTokens.site
      const marker = new AMap.CircleMarker({
        center: [s.lng, s.lat],
        radius: s.coordDegraded ? 5 : 7,
        strokeColor: tok.stroke,
        strokeWeight: 1,
        fillColor: tok.fill,
        fillOpacity: s.coordDegraded ? 0.4 : 1,
        zIndex: 50,
        cursor: 'pointer',
      })
      marker.on('click', () => openSite(s))
      next.push(marker)
    }
  }

  if (props.layersVisible.top && props.model.topZone) {
    const z = props.model.topZone
    const tok = geoMapTokens.topZone
    next.push(new AMap.CircleMarker({
      center: [z.lng, z.lat],
      radius: 11,
      strokeColor: tok.stroke,
      strokeWeight: 2,
      fillColor: tok.stroke,
      fillOpacity: 0.25,
      zIndex: 60,
      bubble: true,
      clickable: false,
    }))
  }

  map.add(next)
  overlays = next
  if (boundsOf(props.model) && next.length) {
    // 右侧 360px 表层避让：[上, 右, 下, 左]
    map.setFitView(next, false, [48, 380, 48, 48], 16)
  }
}

onMounted(async () => {
  // JS API 必须用 Web 端(JS API) 平台 Key；优先 VITE_AMAP_JS_KEY，
  // 仅配 VITE_AMAP_KEY 的旧部署回退（注意：Web 服务型 Key 会鉴权失败）
  const key = (import.meta.env.VITE_AMAP_JS_KEY
    ?? import.meta.env.VITE_AMAP_KEY ?? '').trim()
  if (!key) {
    loading.value = false
    errorMsg.value = '未配置高德 JS API Key（VITE_AMAP_JS_KEY），请改用 Leaflet 在线引擎'
    emit('failed', errorMsg.value)
    return
  }
  try {
    const AMap = await loadAmap(key)
    if (!elRef.value) return
    map = new AMap.Map(elRef.value, {
      zoom: 12,
      viewMode: '2D',
      mapStyle: 'amap://styles/dark',
      features: ['bg', 'road', 'building'],
    })
    info = new AMap.InfoWindow({ offset: new AMap.Pixel(0, -12) })
    draw()
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '高德地图加载失败'
    emit('failed', errorMsg.value)
  } finally {
    loading.value = false
  }
})

watch(
  () => [props.model, props.layersVisible],
  () => draw(),
  { deep: true },
)

onBeforeUnmount(() => {
  try {
    map?.destroy()
  } catch {
    // 忽略销毁竞态
  }
  map = null
  overlays = []
})
</script>

<template>
  <div class="amap-host">
    <div ref="elRef" class="amap-canvas" />
    <div v-if="loading" class="amap-mask">
      <NSpin size="small" /> 正在加载高德地图…
    </div>
    <div v-else-if="errorMsg" class="amap-mask amap-mask--error">
      {{ errorMsg }}
    </div>
  </div>
</template>

<style scoped>
.amap-host {
  position: relative;
  width: 100%;
  height: 100%;
  background: var(--sun-bg-base);
}
.amap-canvas {
  width: 100%;
  height: 100%;
}
.amap-mask {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--sun-text-secondary);
  font-size: 12px;
  background: var(--sun-bg-base);
  pointer-events: none;
}
.amap-mask--error {
  color: var(--sun-error-text);
}
</style>
