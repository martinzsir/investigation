<script setup lang="ts">
// Leaflet 适配器（PLAN-GEO-001 P4）：高德 JS API Key 未配置时的在线回退。
//
// 关键决策：底图选用高德栅格瓦片（webrd，GCJ-02 加密坐标系）而非 OSM
// （WGS-84）——全线坐标已是 GCJ-02，叠 OSM 会产生 300-600m 偏移；
// 栅格瓦片免 Key，但仍属外联资源，故与高德 JS API 同一授权闸口。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import type { GeoLayerModel, GeoSite } from '../../domain/geoMap'
import { boundsOf, probabilityBand } from '../../domain/geoMap'
import { geoMapTokens } from '../../design/tokens'

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

// leaflet 动态加载：未授权在线引擎时连它的 JS 都不进运行时
let L: typeof import('leaflet') | null = null
let map: import('leaflet').Map | null = null
let layerGroup: import('leaflet').LayerGroup | null = null
const bandOpacity = [0.1, 0.22, 0.3, 0.38, 0.52]

function draw(): void {
  if (!L || !map || !layerGroup) return
  layerGroup.clearLayers()

  if (props.layersVisible.cells) {
    for (const c of props.model.cells) {
      const b = probabilityBand(c.probability)
      const tok = geoMapTokens.band[b]
      // ring: [[lng, lat], ...] → leaflet [lat, lng]
      const latlngs = c.ring.map(([lng, lat]) => [lat, lng] as [number, number])
      L.polygon(latlngs, {
        color: tok.stroke,
        weight: 1,
        opacity: 0.9,
        fillColor: tok.stroke,
        fillOpacity: bandOpacity[b],
        interactive: false, // 不挡落脚点点击
      }).addTo(layerGroup)
    }
  }

  if (props.layersVisible.sites) {
    for (const s of props.model.sites) {
      if (s.lat === null || s.lng === null) continue
      const tok = s.coordDegraded ? geoMapTokens.siteDegraded : geoMapTokens.site
      const marker = L.circleMarker([s.lat, s.lng], {
        radius: s.coordDegraded ? 5 : 7,
        color: tok.stroke,
        weight: 1,
        fillColor: tok.fill,
        fillOpacity: s.coordDegraded ? 0.4 : 1,
      })
      const lines = [
        s.stdAddress,
        `到访 ${s.visits} 次｜${s.firstDate ?? '—'} ~ ${s.lastDate ?? '—'}`,
      ]
      if (s.coordDegraded) lines.push('无坐标（文本降级点）')
      marker.bindTooltip(lines.join('<br/>'), {
        direction: 'top',
        offset: [0, -6],
        className: 'geo-tooltip',
      })
      marker.on('click', () => emit('selectSite', s))
      marker.addTo(layerGroup)
    }
  }

  if (props.layersVisible.top && props.model.topZone) {
    const z = props.model.topZone
    const tok = geoMapTokens.topZone
    L.circleMarker([z.lat, z.lng], {
      radius: 11,
      color: tok.stroke,
      weight: 2,
      fillColor: tok.stroke,
      fillOpacity: 0.25,
      interactive: false,
    }).addTo(layerGroup)
  }

  const b = boundsOf(props.model)
  if (b) {
    map.fitBounds(
      L.latLngBounds([b.minLat, b.minLng], [b.maxLat, b.maxLng]).pad(0.18),
      { maxZoom: 16 },
    )
  }
}

onMounted(async () => {
  try {
    L = await import('leaflet')
    await import('leaflet/dist/leaflet.css')
    if (!elRef.value || !L) return
    map = L.map(elRef.value, {
      zoomControl: true,
      attributionControl: false,
      preferCanvas: true,
      // 无几何（全部未编码/事件不足）时 fitBounds 不会触发：
      // Leaflet 未设初始视野则不请求任何瓦片（灰屏），先给全国视野兜底
      center: [35.0, 107.5],
      zoom: 5,
      minZoom: 3,
    })
    // 高德路网栅格（GCJ-02，与语义层坐标同系）
    L.tileLayer(
      'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
      { subdomains: ['1', '2', '3', '4'], maxZoom: 18, minZoom: 3 },
    ).addTo(map)
    layerGroup = L.layerGroup().addTo(map)
    // 容器在 flex 布局中首帧尺寸可能为 0，延迟校正一次
    window.setTimeout(() => map?.invalidateSize(), 60)
    draw()
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : 'Leaflet 地图加载失败'
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
  map?.remove()
  map = null
  layerGroup = null
  L = null
})
</script>

<template>
  <div class="lf-host">
    <div ref="elRef" class="lf-canvas" />
    <div v-if="loading" class="lf-mask">
      <NSpin size="small" /> 正在加载在线地图…
    </div>
    <div v-else-if="errorMsg" class="lf-mask lf-mask--error">
      {{ errorMsg }}
    </div>
  </div>
</template>

<style scoped>
.lf-host {
  position: relative;
  width: 100%;
  height: 100%;
  background: var(--sun-bg-base);
}
.lf-canvas {
  width: 100%;
  height: 100%;
}
.lf-mask {
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
.lf-mask--error {
  color: var(--sun-error-text);
}
</style>
