<script setup lang="ts">
/**
 * 地图窗口（WIN-04）：地点节点的贴附窗口内的地图。
 *
 * 默认离线 SVG，不注入任何外联瓦片/脚本（与 ConvergenceMap 同一授权纪律）。
 * 授权后才动态 import 在线引擎组件——**未授权时引擎 JS 根本不进运行时**，
 * 不是"加载了但不用"（WIN-08）。
 *
 * 单实例（WIN-07）：在线引擎占用全局唯一槽位；槽位被占时本窗口退回离线
 * SVG 并写明原因，绝不排队或强行创建。
 *
 * 红线：区划质心必须空心（fillOpacity=0）。窗口地图比大图更容易被误读成
 * 精确位置——它就贴在"莫干山路"这个门牌名旁边。
 */
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NTag } from 'naive-ui'
import { useMapConsent } from '../../composables/useMapConsent'
import {
  buildMapWindow,
  toSinglePointGeoModel,
  toTrackGeoModel,
  type WindowNode,
} from '../../domain/canvas-window'
import { pointSymbol } from '../../domain/canvas-symbol'
import { acquireEngineSlot, releaseEngineSlot } from '../../domain/map-engine-slot'

const props = withDefaults(
  defineProps<{ node: WindowNode; width?: number; height?: number }>(),
  { width: 316, height: 168 },
)

const consent = useMapConsent()
// 注意：useMapConsent 返回的是 { engine, isOnline, ... }，**没有 current**。
// 解构 current 会得到 undefined，进而让 `mapEngine === 'amap'/'leaflet'`
// 恒为 false——授权成功也永远不渲染在线引擎（ConvergenceMap.vue 曾踩此坑）。
const isOnline = consent.isOnline
const mapEngine = computed(() => consent.engine.value)

const model = computed(() => buildMapWindow(props.node))
const slotId = `nwin-${props.node.id}`
const slotOwned = ref(false)
const engineError = ref('')

// 在线引擎组件：仅在授权后才被真正请求（动态 import 的求值时机在渲染时）
const AmapWin = defineAsyncComponent(() => import('../geo/ConvergenceAmap.vue'))
const LeafletWin = defineAsyncComponent(() => import('../geo/ConvergenceLeaflet.vue'))

watch(
  () => isOnline.value,
  (online) => {
    if (online) {
      if (acquireEngineSlot(slotId)) {
        slotOwned.value = true
        engineError.value = ''
      } else {
        // 槽位被占：退回离线，不排队、不抢占
        slotOwned.value = false
        engineError.value = '已有地图窗口占用在线引擎，本窗口改用离线示意'
      }
    } else {
      releaseEngineSlot(slotId)
      slotOwned.value = false
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => releaseEngineSlot(slotId))

/** 单点投影：以该点为心，按固定跨度画邻域框，只表达相对位置 */
const view = computed(() => {
  const p = model.value.point
  if (!p) return null
  const sym = pointSymbol({
    precise: p.precise,
    maxDimHit: p.maxDimHit,
    ambiguous: p.ambiguous,
  })
  const cx = props.width / 2
  const cy = props.height / 2
  return { p, sym, cx, cy }
})

/**
 * 多点轨迹投影（WIN-04 扩展）：把轨迹点按自身 bounds 归一化到窗口内。
 *
 * 符号口径仍走 pointSymbol（与交汇地图同源）：不可判定的点画成空心小点，
 * 避免"精度不足"与"确实在此"长得一样。
 * 所有点重合时 span 取 0.01 兜底——除零会让坐标变成 NaN 而整块不渲染。
 */
const trackView = computed(() => {
  const ts = model.value.track ?? []
  if (!ts.length) return null
  const lats = ts.map((t) => t.lat)
  const lngs = ts.map((t) => t.lng)
  const minLat = Math.min(...lats)
  const maxLat = Math.max(...lats)
  const minLng = Math.min(...lngs)
  const maxLng = Math.max(...lngs)
  const pad = 20
  const w = Math.max(props.width - pad * 2, 1)
  const h = Math.max(props.height - pad * 2 - 16, 1)
  const spanLat = maxLat - minLat || 0.01
  const spanLng = maxLng - minLng || 0.01
  return ts.map((t) => ({
    x: pad + ((t.lng - minLng) / spanLng) * w,
    y: pad + (1 - (t.lat - minLat) / spanLat) * h,
    sym: pointSymbol({
      precise: t.precise,
      maxDimHit: 0,
      ambiguous: false,
    }),
    determinable: t.determinable,
    at: t.at,
  }))
})

/** 在线引擎消费的模型：有轨迹时走多点，避免"授权后轨迹反而消失" */
const geoModel = computed(() => {
  const tk = toTrackGeoModel(model.value.point, model.value.track ?? [])
  if (tk) return tk
  return model.value.point ? toSinglePointGeoModel(model.value.point) : null
})

function grant(engine: 'amap' | 'leaflet'): void {
  consent.grant(engine)
}
</script>

<template>
  <div class="wmap">
    <div v-if="!view && !trackView" class="wmap-empty">
      {{ model.unmappable || '该地点无可用坐标' }}
    </div>

    <template v-else>
      <!-- 授权闸口 -->
      <div class="wmap-bar">
        <NTag size="small" :bordered="false">
          {{ isOnline ? `在线引擎：${mapEngine}` : '离线示意' }}
        </NTag>
        <span v-if="model.coordsNote" class="wmap-note">{{ model.coordsNote }}</span>
        <template v-if="!isOnline">
          <NButton size="tiny" @click="grant('leaflet')">在线地图</NButton>
        </template>
        <NButton v-else size="tiny" @click="consent.revoke()">撤销</NButton>
      </div>

      <div v-if="engineError" class="wmap-warn">{{ engineError }}</div>

      <!-- 在线：单实例；槽位被占时已在上面提示并回落 -->
      <div v-if="isOnline && slotOwned && geoModel" class="wmap-engine" :style="{ height: height + 'px' }">
        <AmapWin v-if="mapEngine === 'amap'" :model="geoModel" />
        <LeafletWin v-else :model="geoModel" />
      </div>

      <!-- 离线 SVG：单点 + 邻域框，符号口径与交汇地图同源 -->
      <svg
        v-else
        class="wmap-svg"
        :width="width"
        :height="height"
        :viewBox="`0 0 ${width} ${height}`"
        role="img"
        :aria-label="`${view?.p?.address ?? '轨迹'}｜${view ? (view.p.precise ? '门牌级坐标' : '区划质心') : '仅轨迹点'}`"
      >
        <rect x="1" y="1" :width="width - 2" :height="height - 2" fill="#08182a" stroke="#17324a" />
        <line :x1="width / 2" y1="8" :x2="width / 2" :y2="height - 8" stroke="#17324a" stroke-dasharray="3 3" />
        <line x1="8" :y1="height / 2" :x2="width - 8" :y2="height / 2" stroke="#17324a" stroke-dasharray="3 3" />
        <circle
          v-if="view"
          :cx="view.cx"
          :cy="view.cy"
          :r="view.sym.radius + 6"
          fill="none"
          stroke="#17324a"
        />
        <circle
          v-if="view"
          :cx="view.cx"
          :cy="view.cy"
          :r="view.sym.radius"
          :fill="view.sym.fill"
          :fill-opacity="view.sym.fillOpacity"
          :stroke="view.sym.stroke"
          :stroke-width="view.sym.strokeWidth"
          :stroke-dasharray="view.sym.strokeDash.join(' ') || undefined"
        />
        <!-- 轨迹点：判定不了的点画空心，与"确实在此"视觉可辨 -->
        <circle
          v-for="(t, i) in trackView ?? []"
          :key="`tk${i}`"
          :cx="t.x"
          :cy="t.y"
          :r="t.determinable ? 4 : 3"
          :fill="t.determinable ? t.sym.stroke : 'none'"
          :fill-opacity="t.determinable ? t.sym.fillOpacity : 0"
          :stroke="t.sym.stroke"
          :stroke-width="1.5"
        />
        <text :x="width / 2" :y="height - 10" text-anchor="middle" fill="#7d93a8" font-size="10">
          <tspan v-if="view">{{ view.p.precise ? '门牌级坐标' : '区划质心（区级推算）' }}</tspan>
          <tspan v-else>仅轨迹点</tspan>
          <tspan v-if="trackView"> ｜ 空心点＝精度不足，不可据此判时段</tspan>
        </text>
      </svg>

      <div v-if="view" class="wmap-addr">{{ view.p.address }}</div>
      <div v-if="model.trackNote" class="wmap-note">{{ model.trackNote }}</div>
    </template>
  </div>
</template>

<style scoped>
.wmap {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wmap-empty {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  padding: 8px 0;
}
.wmap-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.wmap-note {
  font-size: 10px;
  color: var(--sun-warn-text);
  flex: 1;
}
.wmap-warn {
  font-size: 10px;
  color: var(--sun-warn-text);
}
.wmap-svg {
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: #08182a;
}
.wmap-engine {
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  overflow: hidden;
}
.wmap-addr {
  font-size: 11px;
  color: var(--sun-text-secondary, #9fb3c8);
}
</style>
