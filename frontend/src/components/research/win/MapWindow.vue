<script setup lang="ts">
// P2 地图窗口（place → map，PRD 功能 3）。
// 离线 SVG 示意：网格底 + 本地点 + 画布内其他地点锚点（灰点），零外联——
// 在线底图（Leaflet/高德）不进窗口：涉密环境默认不外联，地图实例唯一性
// 验收（实例数=1）由「窗口内不创建任何在线实例」直接满足。
// 精度徽标复用 precisionSymbol 口径（门牌级=实心、区划质心=空心、无坐标=灰）。
import { computed } from 'vue'
import { KIND_LABELS, type CanvasDoc, type CanvasNode } from '../../../domain/canvas'

const props = defineProps<{
  node: CanvasNode
  doc: CanvasDoc
}>()

const W = 276
const H = 180
const PAD = 18

const lng = computed(() => {
  const v = props.node.props?.lng
  return typeof v === 'number' && v >= -180 && v <= 180 ? v : null
})
const lat = computed(() => {
  const v = props.node.props?.lat
  return typeof v === 'number' && v >= -90 && v <= 90 ? v : null
})
const hasCoord = computed(() => lng.value !== null && lat.value !== null)
const precision = computed(() => String(props.node.props?.coord_precision ?? ''))
const address = computed(
  () => String(props.node.props?.std_address ?? props.node.props?.location_id ?? ''),
)

/** 画布内其他带坐标地点（窗口锚点语境：本地点 + 周边同图地点） */
const peers = computed(() =>
  props.doc.nodes
    .filter(
      (n) =>
        n.id !== props.node.id &&
        n.kind === 'place' &&
        typeof n.props?.lng === 'number' &&
        typeof n.props?.lat === 'number',
    )
    .map((n) => ({
      id: n.id,
      label: n.label,
      kind: KIND_LABELS[n.kind] ?? n.kind,
      lng: n.props?.lng as number,
      lat: n.props?.lat as number,
    })),
)

/** 全部锚点（本地点 + peers）的经纬包围盒 → SVG 投影；单点时取默认跨度 */
const box = computed(() => {
  const pts: Array<{ lng: number; lat: number }> = []
  if (hasCoord.value) pts.push({ lng: lng.value as number, lat: lat.value as number })
  for (const p of peers.value) pts.push({ lng: p.lng, lat: p.lat })
  if (!pts.length) return null
  let minLng = Math.min(...pts.map((p) => p.lng))
  let maxLng = Math.max(...pts.map((p) => p.lng))
  let minLat = Math.min(...pts.map((p) => p.lat))
  let maxLat = Math.max(...pts.map((p) => p.lat))
  // 单点/重合：补 0.01° 视野，避免除零
  if (maxLng - minLng < 0.01) {
    const c = (maxLng + minLng) / 2
    minLng = c - 0.005
    maxLng = c + 0.005
  }
  if (maxLat - minLat < 0.01) {
    const c = (maxLat + minLat) / 2
    minLat = c - 0.005
    maxLat = c + 0.005
  }
  return { minLng, maxLng, minLat, maxLat }
})

function project(pLng: number, pLat: number): { x: number; y: number } | null {
  const b = box.value
  if (!b) return null
  const x = PAD + ((pLng - b.minLng) / (b.maxLng - b.minLng)) * (W - PAD * 2)
  const y = H - PAD - ((pLat - b.minLat) / (b.maxLat - b.minLat)) * (H - PAD * 2)
  return { x, y }
}

const selfPos = computed(() =>
  hasCoord.value ? project(lng.value as number, lat.value as number) : null)

const peerPos = computed(() =>
  peers.value
    .map((p) => ({ ...p, pos: project(p.lng, p.lat) }))
    .filter((p): p is typeof p & { pos: { x: number; y: number } } => p.pos !== null),
)
</script>

<template>
  <div class="mapw" data-testid="map-window">
    <template v-if="hasCoord && selfPos">
      <svg
        :width="W"
        :height="H"
        class="mapw-svg"
        data-testid="map-svg"
        role="img"
        aria-label="地点离线示意（非地图投影）"
      >
        <defs>
          <pattern id="mapw-grid" width="24" height="24" patternUnits="userSpaceOnUse">
            <path d="M 24 0 L 0 0 0 24" fill="none" stroke="#e2e8f2" stroke-width="1" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#mapw-grid)" />
        <g v-for="p in peerPos" :key="p.id">
          <circle :cx="p.pos.x" :cy="p.pos.y" r="4" fill="#b9c6dc" />
          <text :x="p.pos.x + 6" :y="p.pos.y + 3" font-size="9" fill="#7c8aa5">
            {{ p.label }}
          </text>
        </g>
        <g data-testid="map-self">
          <circle :cx="selfPos.x" :cy="selfPos.y" r="7" fill="#FF7043" opacity="0.25" />
          <circle :cx="selfPos.x" :cy="selfPos.y" r="4" fill="#FF7043" />
        </g>
      </svg>
      <div class="mapw-meta">
        <span v-if="precision" class="mapw-precision" :data-precision="precision">
          {{ precision }}{{ precision === '无坐标' ? '' : ' · 离线示意（非地图投影 · 零外联）' }}
        </span>
        <span v-else class="mapw-precision">离线示意（非地图投影 · 零外联）</span>
        <span v-if="address" class="mapw-addr" :title="address">{{ address }}</span>
        <span class="mapw-coord">{{ lng }}, {{ lat }}</span>
      </div>
    </template>
    <div v-else class="mapw-empty" data-testid="map-empty">
      <p>该地点暂无可用坐标</p>
      <p v-if="address" class="mapw-addr">区划/地址：{{ address }}</p>
      <p v-if="precision" class="mapw-precision">精度档：{{ precision }}</p>
    </div>
  </div>
</template>

<style scoped>
.mapw { display: flex; flex-direction: column; gap: 8px; }
.mapw-svg {
  width: 100%;
  height: auto;
  border: 1px solid var(--sun-border, #e2e8f2);
  border-radius: 8px;
  background: #fafbfd;
}
.mapw-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: var(--sun-text-secondary, #4a5a76);
}
.mapw-precision { color: var(--sun-text-tertiary, #7c8aa5); }
.mapw-addr {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mapw-coord { font-family: var(--sun-font-mono, monospace); }
.mapw-empty {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 100px;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--sun-text-tertiary, #7c8aa5);
  background: var(--sun-surface-2, #f6f8fb);
  border-radius: 8px;
}
.mapw-empty p { margin: 0; }
</style>
