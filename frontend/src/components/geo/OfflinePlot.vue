<script setup lang="ts">
// 离线相对示意（PLAN-GEO-001 P4 §6.3 默认隔离态）。
//
// 零外联：纯 SVG + 等距矩形局部投影（经度乘 cos(纬度)），不加载任何底图
// 资源。只表达「点位/概率格之间的相对关系」，不承载真实地理形状——
// 水印与角注明确告知「非地图投影」，避免被当成定址图使用。
import { computed } from 'vue'
import type { GeoLayerModel, GeoSite } from '../../domain/geoMap'
import { boundsOf, probabilityBand } from '../../domain/geoMap'
import { geoMapTokens } from '../../design/tokens'

const props = defineProps<{
  model: GeoLayerModel
  layersVisible: { cells: boolean; sites: boolean; top: boolean }
}>()
const emit = defineEmits<{ selectSite: [site: GeoSite] }>()

const W = 1000
const H = 680
const PAD = 56

interface Projection {
  project: (lng: number, lat: number) => [number, number]
  /** 每像素对应米数（纬度方向，比例尺用） */
  metersPerPixel: number
}

const projection = computed<Projection | null>(() => {
  const b = boundsOf(props.model)
  if (!b) return null
  const midLat = ((b.minLat + b.maxLat) / 2) * (Math.PI / 180)
  const kx = Math.max(Math.abs(Math.cos(midLat)), 0.01)
  const dx = Math.max((b.maxLng - b.minLng) * kx, 1e-9)
  const dy = Math.max(b.maxLat - b.minLat, 1e-9)
  const scale = Math.min((W - 2 * PAD) / dx, (H - 2 * PAD) / dy)
  const cx = (b.minLng + b.maxLng) / 2
  const cy = (b.minLat + b.maxLat) / 2
  return {
    project: (lng, lat) => [
      W / 2 + (lng - cx) * kx * scale,
      H / 2 - (lat - cy) * scale,
    ],
    metersPerPixel: 111_320 / scale,
  }
})

/** 视野底边大约跨度（比例尺注记） */
const spanLabel = computed<string>(() => {
  const p = projection.value
  if (!p) return ''
  const km = (p.metersPerPixel * (W - 2 * PAD)) / 1000
  return km >= 1 ? `图示横宽约 ${km.toFixed(km >= 10 ? 0 : 1)} km` : '局部放大'
})

const cellPolys = computed(() => {
  const p = projection.value
  if (!p || !props.layersVisible.cells) return []
  return props.model.cells.map((c) => {
    const b = probabilityBand(c.probability)
    const points = c.ring.map(([lng, lat]) => p.project(lng, lat).join(',')).join(' ')
    return { points, band: b, key: `${b}:${points.slice(0, 32)}`, probability: c.probability }
  })
})

const siteDots = computed(() => {
  const p = projection.value
  if (!p || !props.layersVisible.sites) return []
  return props.model.sites
    .filter((s) => s.lat !== null && s.lng !== null)
    .map((s) => {
      const [x, y] = p.project(s.lng as number, s.lat as number)
      return { s, x, y }
    })
})

const topMark = computed(() => {
  const p = projection.value
  const z = props.model.topZone
  if (!p || !z || !props.layersVisible.top) return null
  const [x, y] = p.project(z.lng, z.lat)
  const d = 12
  return { x, y, points: `${x},${y - d} ${x + d},${y} ${x},${y + d} ${x - d},${y}`, z }
})

const bands = geoMapTokens.band
</script>

<template>
  <div class="off-host">
    <svg
      v-if="projection"
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="xMidYMid meet"
      class="off-svg"
    >
      <!-- 概率面格子 -->
      <polygon
        v-for="c in cellPolys"
        :key="c.key"
        :points="c.points"
        :fill="bands[c.band].fill"
        :stroke="bands[c.band].stroke"
        stroke-width="1"
      />
      <!-- 落脚点 -->
      <g
        v-for="({ s, x, y }) in siteDots"
        :key="s.locationId ?? s.stdAddress"
        class="off-site"
        @click="emit('selectSite', s)"
      >
        <circle
          :cx="x"
          :cy="y"
          :r="s.coordDegraded ? 5 : 7"
          :fill="s.coordDegraded ? geoMapTokens.siteDegraded.fill : geoMapTokens.site.fill"
          :stroke="s.coordDegraded ? geoMapTokens.siteDegraded.stroke : geoMapTokens.site.stroke"
          :stroke-width="s.coordDegraded ? 1 : 1.5"
          :fill-opacity="s.coordDegraded ? 0.6 : 1"
        />
        <title>{{ s.stdAddress }}｜到访 {{ s.visits }} 次</title>
      </g>
      <!-- 顶格排查区 -->
      <polygon
        v-if="topMark"
        :points="topMark.points"
        :fill="geoMapTokens.topZone.fill"
        :stroke="geoMapTokens.topZone.stroke"
        stroke-width="2"
      >
        <title>顶格排查网格 #{{ topMark.z.rank }}｜概率 {{ (topMark.z.probability * 100).toFixed(1) }}%</title>
      </polygon>
    </svg>

    <div v-else class="off-empty">
      当前观察无任何可绘制坐标（落脚点全部未编码或事件不足 5 起）。
      可在右侧坐标表层查看文本地址，或运行地理编码脚本补坐标后重跑镜头。
    </div>

    <!-- 角注与图例 -->
    <div class="off-hud">
      <div class="off-watermark">离线相对示意 · 非地图投影 · 零外联</div>
      <div v-if="spanLabel" class="off-scale">{{ spanLabel }}</div>
    </div>
    <div v-if="projection" class="off-legend">
      <span class="off-legend-title">概率档</span>
      <span v-for="(b, i) in bands" :key="i" class="off-legend-item">
        <i :style="{ background: b.fill, borderColor: b.stroke }" />
        {{ `${i * 20}-${(i + 1) * 20}%` }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.off-host {
  position: relative;
  width: 100%;
  height: 100%;
  background:
    repeating-linear-gradient(0deg, transparent 0 39px, rgba(16, 49, 74, 0.25) 39px 40px),
    repeating-linear-gradient(90deg, transparent 0 39px, rgba(16, 49, 74, 0.25) 39px 40px),
    var(--sun-bg-base);
  overflow: hidden;
}
.off-svg {
  width: 100%;
  height: 100%;
  display: block;
}
.off-site {
  cursor: pointer;
}
.off-site:hover circle {
  stroke-width: 2.5;
}
.off-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
  text-align: center;
  color: var(--sun-text-tertiary);
  font-size: 13px;
  line-height: 1.8;
}
.off-hud {
  position: absolute;
  top: 10px;
  left: 12px;
  pointer-events: none;
}
.off-watermark {
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--sun-text-tertiary);
}
.off-scale {
  margin-top: 2px;
  font-size: 11px;
  color: var(--sun-text-secondary);
  font-family: var(--sun-font-mono);
}
.off-legend {
  position: absolute;
  bottom: 10px;
  left: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  background: rgba(3, 10, 20, 0.78);
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  font-size: 11px;
  color: var(--sun-text-secondary);
  pointer-events: none;
}
.off-legend-title {
  color: var(--sun-text-tertiary);
}
.off-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--sun-font-mono);
}
.off-legend-item i {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 1px solid;
  border-radius: 2px;
}
</style>
