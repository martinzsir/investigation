<script setup lang="ts">
/**
 * 三维交汇地图 · 高德 JS API 适配器。
 *
 * 与既有 geo/AmapMap.vue 同套纪律：
 *   - 仅在用户授权高德引擎后由父级 v-if 挂载，挂载时才动态注入
 *     webapi.amap.com 脚本（见 amapLoader）；未授权时本文件不进运行时；
 *   - GCJ-02 坐标直叠，不做任何坐标转换。
 *
 * 与 geo/AmapMap.vue 的差异：画的是「人-时-地锚点」而非「概率面」，
 * 半径=命中维数、实心/空心=坐标精度档、虚线=重名待裁决。
 * 红线：质心点 fillOpacity 必须为 0（空心），不得画成实心精确点。
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import type { ConvGeoModel } from '../../domain/convergence-geo'
import { MAP_SYMBOL } from '../../domain/convergence-geo'
import { loadAmap } from './amapLoader'

const props = defineProps<{ model: ConvGeoModel }>()
const emit = defineEmits<{
  select: [key: string]
  failed: [message: string]
}>()

const elRef = ref<HTMLDivElement | null>(null)
const loading = ref(true)
const errorMsg = ref('')

let map: any = null
let info: any = null
let overlays: any[] = []

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

function openPoint(p: ConvGeoModel['points'][number]): void {
  if (!info) return
  const prec = p.degraded
    ? '<span style="color:#F28B93">区划质心（区级推算，不代表实际位置）</span>'
    : '门牌级坐标'
  const amb = p.ambiguous
    ? '<div style="color:#F2B54D">重名待裁决，不可当作单一自然人</div>' : ''
  info.setContent(
    `<div style="min-width:200px;color:#E9F7FA;font-size:12px;line-height:1.7">
       <div style="font-weight:600;margin-bottom:2px">${esc(p.address)}</div>
       <div style="color:#9FB8C6">${prec}</div>
       ${amb}
       <div style="color:#9FB8C6">${esc(p.persons.join('/'))}
         ｜${esc(p.dates.join('、'))}</div>
       <div style="color:#9FB8C6">命中 <b style="color:#F2B54D">${p.maxDimHit}</b> 维
         ｜锚点 ${p.count} 条</div>
     </div>`,
  )
  info.open(map, [p.lng, p.lat])
  if (p.convKeys.length) emit('select', p.convKeys[0])
}

function draw(): void {
  const AMap = window.AMap
  if (!map || !AMap) return
  clearOverlays()
  const next: any[] = []

  for (const p of props.model.points) {
    const marker = new AMap.CircleMarker({
      center: [p.lng, p.lat],
      radius: MAP_SYMBOL.radius(p.maxDimHit),
      strokeColor: p.ambiguous ? MAP_SYMBOL.strokeAmbiguous : MAP_SYMBOL.stroke,
      strokeWeight: p.ambiguous
        ? MAP_SYMBOL.strokeWidthAmbiguous
        : MAP_SYMBOL.strokeWidth,
      // 质心 fillOpacity=0 → 空心；门牌级实心
      fillColor: MAP_SYMBOL.fillPrecise,
      fillOpacity: p.degraded
        ? MAP_SYMBOL.fillOpacityDegraded
        : MAP_SYMBOL.fillOpacityPrecise,
      zIndex: 50,
      cursor: 'pointer',
    })
    marker.on('click', () => openPoint(p))
    next.push(marker)
  }

  map.add(next)
  overlays = next

  const b = props.model.bounds
  if (b && next.length) {
    map.setFitView(next, false, [48, 48, 48, 48], 16)
  }
}

onMounted(async () => {
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

watch(() => props.model, () => draw(), { deep: true })

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
  <div class="cam-host">
    <div ref="elRef" class="cam-canvas" />
    <div v-if="loading" class="cam-mask">
      <NSpin size="small" /> 正在加载高德地图…
    </div>
    <div v-else-if="errorMsg" class="cam-mask cam-mask--error">
      {{ errorMsg }}
    </div>
  </div>
</template>

<style scoped>
.cam-host {
  position: relative;
  width: 100%;
  height: 420px;
  background: var(--sun-bg-base);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
}
.cam-canvas {
  width: 100%;
  height: 100%;
}
.cam-mask {
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
.cam-mask--error {
  color: var(--sun-error-text);
}
</style>
