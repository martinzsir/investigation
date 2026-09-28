<script setup lang="ts">
/**
 * 三维交汇地图 · Leaflet 适配器（在线回退引擎）。
 *
 * 与既有 geo/LeafletMap.vue 同套纪律：
 *   - 底图用高德栅格瓦片（webrd，GCJ-02），**不叠 OSM**——全线坐标已是
 *     GCJ-02，叠 WGS-84 底图会产生 300-600m 偏移；
 *   - 瓦片属外联资源，故与高德 JS API 同一授权闸口，未授权时本组件根本
 *     不会被挂载（父级 v-if），连 leaflet 的 JS 都不进运行时。
 *
 * 与 geo/LeafletMap.vue 的差异（故不复用而新建）
 * -----------------------------------------------
 * 那个画的是「概率面 + 落脚点 + 顶格区」（GeoLayerModel），本图画的是
 * 「人-时-地锚点」（ConvGeoModel）——语义完全不同：没有概率面，取而代之
 * 的是**命中维数**（半径）与**坐标精度档**（实心/空心）。硬塞进同一个
 * 组件会让两边都变成大杂烩。
 *
 * 红线：质心点必须空心 + 无坐标锚点不得静默消失（后者由父级单列）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import type { ConvGeoModel } from '../../domain/convergence-geo'
import {
  MAP_SYMBOL,
  describeConvPoint,
} from '../../domain/convergence-geo'

const props = defineProps<{ model: ConvGeoModel }>()
const emit = defineEmits<{
  select: [key: string]
  failed: [message: string]
}>()

const elRef = ref<HTMLDivElement | null>(null)
const loading = ref(true)
const errorMsg = ref('')

let L: typeof import('leaflet') | null = null
let map: import('leaflet').Map | null = null
let layerGroup: import('leaflet').LayerGroup | null = null

function draw(): void {
  if (!L || !map || !layerGroup) return
  layerGroup.clearLayers()

  for (const p of props.model.points) {
    const ambiguous = p.ambiguous
    const marker = L.circleMarker([p.lat, p.lng], {
      radius: MAP_SYMBOL.radius(p.maxDimHit),
      // 质心不填充（空心），门牌级实心——视觉分档是红线不是偏好
      color: ambiguous ? MAP_SYMBOL.strokeAmbiguous : MAP_SYMBOL.stroke,
      weight: ambiguous ? MAP_SYMBOL.strokeWidthAmbiguous : MAP_SYMBOL.strokeWidth,
      fillColor: MAP_SYMBOL.fillPrecise,
      fillOpacity: p.degraded
        ? MAP_SYMBOL.fillOpacityDegraded
        : MAP_SYMBOL.fillOpacityPrecise,
      dashArray: ambiguous ? '4 3' : undefined,
    })
    marker.bindTooltip(
      describeConvPoint(p).replace(/\n/g, '<br/>'),
      { direction: 'top', offset: [0, -6], className: 'geo-tooltip' },
    )
    marker.on('click', () => {
      if (p.convKeys.length) emit('select', p.convKeys[0])
    })
    marker.addTo(layerGroup)
  }

  const b = props.model.bounds
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
      // 无几何时 fitBounds 不触发，Leaflet 未设初始视野则不请求瓦片（灰屏）
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

watch(() => props.model, () => draw(), { deep: true })

onBeforeUnmount(() => {
  map?.remove()
  map = null
  layerGroup = null
  L = null
})
</script>

<template>
  <div class="clf-host">
    <div ref="elRef" class="clf-canvas" />
    <div v-if="loading" class="clf-mask">
      <NSpin size="small" /> 正在加载在线地图…
    </div>
    <div v-else-if="errorMsg" class="clf-mask clf-mask--error">
      {{ errorMsg }}
    </div>
  </div>
</template>

<style scoped>
.clf-host {
  position: relative;
  width: 100%;
  height: 420px;
  background: var(--sun-bg-base);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
}
.clf-canvas {
  width: 100%;
  height: 100%;
}
.clf-mask {
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
.clf-mask--error {
  color: var(--sun-error-text);
}
</style>
