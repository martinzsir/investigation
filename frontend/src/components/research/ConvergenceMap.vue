<script setup lang="ts">
/**
 * 三维交汇地图视图（离线优先）。
 *
 * 授权纪律（PLAN-GEO-001 P4 §6.3）
 * --------------------------------
 * 默认 offline：**不注入任何外联瓦片/脚本**。本图是纯 SVG 散点图，
 * 展示锚点的**相对位置**与聚集形态，不提供在线底图——涉密环境下这是常态，
 * 且"看相对位置"足以回答"聚集在哪个片区"。授权态由 useMapConsent 统一管，
 * 本组件不自行判断网络出口。
 *
 * 两条红线（与列表面板、G6 图同口径）
 * ------------------------------------
 * 1. **质心点必须空心**：区划质心（coord_degraded）在地图上和门牌级长得
 *    一模一样，若都画实心，等于把"区级推算"伪装成精确位置。地图天生看上去
 *    精确，误导性比列表更强。
 * 2. **无坐标锚点不消失**：单列到下方，写明原因——它们照样是交汇结果，
 *    只是落不到地图上。
 */
import { computed, ref } from 'vue'
import { NAlert, NButton, NEmpty, NTag, NTooltip } from 'naive-ui'
import { useMapConsent } from '../../composables/useMapConsent'
import {
  MAP_LEGEND,
  buildConvergenceGeo,
  describeConvPointBrief,
  projectConvergencePoints,
} from '../../domain/convergence-geo'
import type { ConvProjectedPoint } from '../../domain/convergence-geo'
import type { ConvergenceItem } from '../../api/endpoints/convergence'
import ConvergenceAmap from '../geo/ConvergenceAmap.vue'
import ConvergenceLeaflet from '../geo/ConvergenceLeaflet.vue'

const props = withDefaults(defineProps<{
  items: ConvergenceItem[]
  coords?: { status?: string; detail?: string; effect?: string } | null
  width?: number
  height?: number
}>(), { width: 760, height: 420 })

const emit = defineEmits<{ (e: 'select', key: string): void }>()

// 授权闸口与既有地理画像视图同一套：未授权零外联，授权可撤销
const consent = useMapConsent()
// useMapConsent 返回 { engine, isOnline, ... }，没有 current——
// 解构 current 会让 mapEngine 恒为 undefined，授权后在线引擎永远不渲染。
const isOnline = consent.isOnline
const mapEngine = computed(() => consent.engine.value)

const model = computed(() => buildConvergenceGeo(props.items, { coords: props.coords }))
const projected = computed<ConvProjectedPoint[]>(() => projectConvergencePoints(
  model.value.points, model.value.bounds, props.width, props.height))

const hovered = ref<ConvProjectedPoint | null>(null)
const selectedId = ref<string | null>(null)
const mapError = ref('')

/** 在线引擎下零可打点几何时的遮罩提示（离线 SVG 自带空态，不重复） */
const showNoGeomOverlay = computed(
  () => isOnline.value && model.value.points.length === 0,
)

function onPick(p: ConvProjectedPoint) {
  selectedId.value = p.id
  if (p.convKeys.length) emit('select', p.convKeys[0])
}

function grantEngine(engine: 'amap' | 'leaflet'): void {
  consent.grant(engine)
  mapError.value = ''
}

function onFailed(msg: string): void {
  mapError.value = msg
}

/** 点的可读写摘要——含精度自陈，避免"看着精确" */
function describe(p: ConvProjectedPoint): string {
  return describeConvPointBrief(p)
}
</script>

<template>
  <div class="conv-map">
    <NAlert v-if="model.coordsStatus !== 'ok'" type="warning" :show-icon="false" class="mb8">
      坐标不可用：{{ model.coordsNote || '语义层不可达' }}
      <template v-if="coords?.effect">——{{ coords.effect }}</template>
    </NAlert>

    <!-- 授权闸口：未授权零外联；撤销即销毁地图实例回纯离线 SVG -->
    <div
      class="engine-bar"
      :class="isOnline ? 'engine-bar--on' : 'engine-bar--off'"
    >
      <template v-if="!isOnline">
        <span class="engine-bar__icon" aria-hidden="true">◌</span>
        <div class="engine-bar__text">
          <b>离线模式</b>：未加载任何外部地图资源，下方为按经纬度绘制的相对示意图。
          授权在线引擎后由浏览器直连高德服务器加载底图（坐标仅在你的浏览器内渲染，
          不经内核网络出口）。
        </div>
        <NTooltip v-if="!consent.amapKeyConfigured.value">
          <template #trigger>
            <NButton size="small" disabled>高德地图（需配置 Key）</NButton>
          </template>
          需在 frontend/.env.local 配置高德 JS API Key（VITE_AMAP_JS_KEY）后重新构建；
          当前可改用 Leaflet 在线引擎（高德栅格瓦片，免 Key）或保持离线。
        </NTooltip>
        <NButton
          v-else
          size="small"
          type="primary"
          @click="grantEngine('amap')"
        >
          授权并加载高德地图
        </NButton>
        <NButton size="small" @click="grantEngine('leaflet')">
          在线地图（免 Key 回退）
        </NButton>
      </template>
      <template v-else>
        <span class="engine-bar__icon" aria-hidden="true">◉</span>
        <div class="engine-bar__text">
          <b>在线模式 · {{ mapEngine === 'amap' ? '高德 JS API' : 'Leaflet + 高德瓦片' }}</b>
          ：底图请求由你的浏览器直连高德服务器；全线坐标 GCJ-02 直叠无偏移。
          实心点 = 门牌级坐标，空心点 = 区划质心（区级推算，不代表实际位置）。
        </div>
        <NButton
          size="small"
          :type="mapEngine === 'amap' ? 'primary' : 'default'"
          :disabled="!consent.amapKeyConfigured.value"
          @click="grantEngine('amap')"
        >
          高德
        </NButton>
        <NButton
          size="small"
          :type="mapEngine === 'leaflet' ? 'primary' : 'default'"
          @click="grantEngine('leaflet')"
        >
          Leaflet
        </NButton>
        <NButton size="small" @click="consent.revoke()">撤销授权</NButton>
      </template>
    </div>

    <div class="map-canvas">
      <!-- 在线引擎：授权后才挂载，未授权时其 JS 根本不进运行时 -->
      <ConvergenceAmap
        v-if="mapEngine === 'amap'"
        :model="model"
        @select="(k: string) => emit('select', k)"
        @failed="onFailed"
      />
      <ConvergenceLeaflet
        v-else-if="mapEngine === 'leaflet'"
        :model="model"
        @select="(k: string) => emit('select', k)"
        @failed="onFailed"
      />
      <template v-else>
        <svg
          v-if="projected.length"
          :viewBox="`0 0 ${width} ${height}`"
          class="map-svg"
          role="img"
          aria-label="三维交汇锚点地图"
        >
          <!-- 质心点：空心（hollow）；门牌级：实心 -->
          <g v-for="p in projected" :key="p.id">
            <circle
              v-if="p.degraded"
              :cx="p.x" :cy="p.y" :r="p.r"
              class="pt hollow"
              :class="{ amb: p.ambiguous }"
              @click="onPick(p)"
              @mouseenter="hovered = p"
              @mouseleave="hovered = null"
            />
            <circle
              v-else
              :cx="p.x" :cy="p.y" :r="p.r"
              class="pt solid"
              :class="{ amb: p.ambiguous }"
              @click="onPick(p)"
              @mouseenter="hovered = p"
              @mouseleave="hovered = null"
            />
            <title>{{ describe(p) }}</title>
            <text v-if="p.maxDimHit >= 3" :x="p.x" :y="p.y - p.r - 4" class="lbl">
              {{ p.address.slice(0, 6) }}
            </text>
          </g>
        </svg>
        <NEmpty v-else description="无可用坐标，无法绘制地图（见下方未落点清单）" />
      </template>

      <div v-if="showNoGeomOverlay" class="map-nogeom">
        当前筛选结果无任何可打点坐标（锚点均无地点实体或语义层不可达）。
        可见下方未落点清单，或回离线模式查看相对示意。
      </div>
      <div v-if="mapError" class="map-error">
        {{ mapError }}
        <NButton size="tiny" quaternary @click="grantEngine('leaflet')">
          改用 Leaflet
        </NButton>
        <NButton size="tiny" quaternary @click="consent.revoke()">
          回离线示意
        </NButton>
      </div>
    </div>

    <div v-if="hovered && !isOnline" class="tip">{{ describe(hovered) }}</div>

    <div class="legend">
      <span v-for="l in MAP_LEGEND" :key="l.text" class="lg">
        <i :class="['sw', l.kind]" />{{ l.text }}
      </span>
    </div>

    <div v-if="model.degradedCount" class="warn">
      其中 {{ model.degradedCount }} 个点为区划质心坐标（空心），
      仅表示"大概在这个区"，不代表实际位置。
    </div>

    <div v-if="model.unmapped.length" class="unmapped">
      <div class="h">无坐标、未落点的锚点（{{ model.unmapped.length }}）</div>
      <div v-for="u in model.unmapped" :key="u.key" class="row">
        <NTag size="small">{{ u.date }}</NTag>
        <span class="who">{{ u.personLabel }}</span>
        <span class="dim">{{ u.address }}——{{ u.reason }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.conv-map { display: flex; flex-direction: column; }
.mb8 { margin-bottom: 8px; }
/* 授权横幅：与 GeoMapView 同口径，配色走主题变量 */
.engine-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  margin-bottom: 8px;
}
.engine-bar--off {
  background: var(--sun-warn-bg);
  border-color: var(--sun-warn-border);
  color: var(--sun-warn-text);
}
.engine-bar--on {
  background: var(--sun-info-bg);
  border-color: var(--sun-info-border);
  color: var(--sun-info-text);
}
.engine-bar__icon { font-size: 16px; }
.engine-bar__text { flex: 1; color: var(--sun-text-secondary); }
.engine-bar__text b { color: var(--sun-text-primary); }

/* 地图区：三个渲染器占位同一块，在线引擎才有高度 */
.map-canvas {
  position: relative;
  min-height: 0;
}
.map-nogeom {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  max-width: 440px;
  padding: 10px 14px;
  font-size: 12px;
  line-height: 1.8;
  text-align: center;
  color: var(--sun-warn-text);
  background: rgba(20, 26, 12, 0.82);
  border: 1px solid var(--sun-warn-border);
  border-radius: 6px;
  pointer-events: none;
  /* Leaflet 内建 pane z-index 200~700，必须高于瓦片层 */
  z-index: 1000;
}
.map-error {
  position: absolute;
  left: 50%;
  bottom: 12px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--sun-error-text);
  background: rgba(43, 15, 18, 0.92);
  border: 1px solid var(--sun-error-border);
  border-radius: 4px;
  z-index: 1000;
}
.dim { color: #999; }
.map-svg { width: 100%; height: auto; background: #fafbfc; border: 1px solid #e5e7eb; border-radius: 6px; }
.pt { cursor: pointer; stroke: #33475b; stroke-width: 1.5; }
.pt.solid { fill: #2f7ed8; }
.pt.hollow { fill: #fff; }
.pt.amb { stroke-dasharray: 3 2; stroke: #b4781a; }
.pt:hover { stroke-width: 2.5; }
.lbl { font-size: 10px; fill: #555; text-anchor: middle; }
.tip { margin-top: 6px; font-size: 12px; color: #333; background: #f3f4f6; padding: 4px 8px; border-radius: 4px; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; font-size: 12px; color: #555; }
.lg { display: inline-flex; align-items: center; gap: 4px; }
.sw { width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid #33475b; display: inline-block; }
.sw.solid { background: #2f7ed8; }
.sw.hollow { background: #fff; }
.sw.ring { border-style: dashed; border-color: #b4781a; background: #fff; }
.sw.size { border: none; background: linear-gradient(90deg, #999 40%, #2f7ed8 40%); border-radius: 2px; }
.warn { margin-top: 8px; font-size: 12px; color: #b4781a; }
.unmapped { margin-top: 10px; border-top: 1px dashed #e5e7eb; padding-top: 8px; }
.unmapped .h { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.unmapped .row { font-size: 12px; display: flex; gap: 6px; align-items: center; padding: 2px 0; }
.who { color: #333; }
</style>
