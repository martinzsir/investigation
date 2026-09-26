<script setup lang="ts">
// 地理画像地图视图（PLAN-GEO-001 P4）。
//
// 三层职责：
//   ① 授权闸口——未授权零外联，只渲染纯 SVG 相对示意；授权后才挂载
//      高德 JS API 或 Leaflet（高德栅格瓦片，GCJ-02），授权可撤销；
//   ② 数据——读既有「观察档案」端点（geo_site_profile/geo_serial_profile
//      两个镜头的 detail），不新增后端端点；serial 观察自动并轨同主体
//      site 观察的落脚点坐标；
//   ③ 图层——概率面五档热力格 / 落脚点 / 顶格排查区，右侧坐标表与覆盖率。
// 红线：图上一切措辞只给「优先排查区域」，不作定址/定性结论。
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton,
  NCheckbox,
  NSpin,
  NTag,
  NTooltip,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import {
  observationsApi,
  type ObservationItem,
} from '../api/endpoints/observations'
import { presentError } from '../api/errors'
import { useMapConsent, type MapEngine } from '../composables/useMapConsent'
import {
  boundsOf,
  GEO_SKILL_SERIAL,
  GEO_SKILL_SITE,
  geocodeSourceLabel,
  mergeSites,
  parseGeoObservation,
  type GeoLayerModel,
  type GeoSite,
} from '../domain/geoMap'
import EmptyState from '../components/common/EmptyState.vue'
import AmapMap from '../components/geo/AmapMap.vue'
import LeafletMap from '../components/geo/LeafletMap.vue'
import OfflinePlot from '../components/geo/OfflinePlot.vue'

const cs = useCaseStore()
const router = useRouter()
const consent = useMapConsent()

const listLoading = ref(false)
const listError = ref('')
const items = ref<ObservationItem[]>([])
const selectedId = ref<string>('')
const model = ref<GeoLayerModel | null>(null)
const detailLoading = ref(false)
const mapError = ref('')
const selectedSiteKey = ref('')

const layersVisible = ref({ cells: true, sites: true, top: true })

const serialItems = computed(() => items.value.filter((i) => i.skill_id === GEO_SKILL_SERIAL))
const siteItems = computed(() => items.value.filter((i) => i.skill_id === GEO_SKILL_SITE))

/** 覆盖率口径随镜头类型切换文案 */
const coverageText = computed<string>(() => {
  const m = model.value
  if (!m?.coordCoverage) return '—'
  const unit = m.kind === 'serial' ? '起坐标事件' : '个落脚点'
  return `${m.coordCoverage.withCoords}/${m.coordCoverage.total} ${unit}已编码`
})

const coveragePercent = computed<number | null>(() => {
  const c = model.value?.coordCoverage
  if (!c || c.total <= 0) return null
  return Math.round((c.withCoords / c.total) * 100)
})

const topZones = computed(() => model.value?.zones.slice(0, 10) ?? [])

/** 在线引擎下，选中观察但零几何（全部未编码/事件不足）时给出遮罩提示；
 *  离线 SVG 组件内部已有同文案空态，不重复遮罩 */
const showNoGeomOverlay = computed(
  () =>
    !!model.value &&
    consent.engine.value !== 'offline' &&
    boundsOf(model.value) === null,
)

const coordSites = computed(() =>
  (model.value?.sites ?? []).filter((s) => !s.coordDegraded))
const degradedSites = computed(() =>
  (model.value?.sites ?? []).filter((s) => s.coordDegraded))

async function loadList(): Promise<void> {
  if (!cs.currentCaseId) {
    items.value = []
    return
  }
  listLoading.value = true
  listError.value = ''
  try {
    const cid = cs.currentCaseId
    const [rSerial, rSites] = await Promise.all([
      observationsApi.list(cid, { skill: GEO_SKILL_SERIAL, page: 1, page_size: 200 }),
      observationsApi.list(cid, { skill: GEO_SKILL_SITE, page: 1, page_size: 200 }),
    ])
    items.value = [...rSerial.observations, ...rSites.observations]
    // 默认展示最近一次系列画像；没有则回落落脚点画像
    const preferred = serialItems.value[0] ?? siteItems.value[0]
    if (preferred) await selectItem(preferred)
    else {
      selectedId.value = ''
      model.value = null
    }
  } catch (e) {
    listError.value = presentError(e).title
    items.value = []
  } finally {
    listLoading.value = false
  }
}

async function selectItem(o: ObservationItem): Promise<void> {
  if (!cs.currentCaseId) return
  selectedId.value = o.observation_id
  detailLoading.value = true
  model.value = null
  selectedSiteKey.value = ''
  mapError.value = ''
  try {
    const cid = cs.currentCaseId
    const detail = await observationsApi.detail(cid, o.observation_id)
    let m = parseGeoObservation(detail)
    if (!m) return
    // serial 观察只带事件引用：并轨同主体 site 观察的落脚点坐标图层
    if (m.kind === 'serial' && m.sites.length === 0) {
      const peer = items.value.find(
        (x) => x.skill_id === GEO_SKILL_SITE && x.subject === o.subject,
      )
      if (peer) {
        const pd = await observationsApi.detail(cid, peer.observation_id)
        const pm = parseGeoObservation(pd)
        if (pm) m = mergeSites(m, pm.sites)
      }
    }
    model.value = m
  } catch (e) {
    listError.value = presentError(e).title
  } finally {
    detailLoading.value = false
  }
}

function grantEngine(engine: Exclude<MapEngine, 'offline'>): void {
  consent.grant(engine)
  mapError.value = ''
}

function onSelectSite(s: GeoSite): void {
  selectedSiteKey.value = s.locationId ?? s.stdAddress
}

function openObservation(): void {
  if (selectedId.value) {
    void router.push(`/c/observations/${selectedId.value}`)
  }
}

function lensLabel(o: ObservationItem): string {
  return o.skill_id === GEO_SKILL_SERIAL ? '系列画像' : '落脚点'
}

function fmtCoord(v: number | null): string {
  return v === null ? '—' : v.toFixed(6)
}

const noCase = computed(() => !cs.currentCaseId)

watch(() => cs.currentCaseId, () => void loadList())
onMounted(() => void loadList())
</script>

<template>
  <div class="geo-page">
    <!-- ① 授权横幅 -->
    <div
      class="geo-consent"
      :class="consent.engine.value === 'offline' ? 'geo-consent--off' : 'geo-consent--on'"
    >
      <template v-if="consent.engine.value === 'offline'">
        <span class="geo-consent__icon" aria-hidden="true">◌</span>
        <div class="geo-consent__text">
          <b>离线模式</b>：未加载任何外部地图资源，下方为按经纬度绘制的相对示意图。
          授权在线引擎后将连接高德服务器加载底图（坐标仅在你的浏览器内渲染，不经内核网络出口）。
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
        <span class="geo-consent__icon" aria-hidden="true">◉</span>
        <div class="geo-consent__text">
          <b>在线模式 · {{ consent.engine.value === 'amap' ? '高德 JS API' : 'Leaflet + 高德瓦片' }}</b>
          ：底图请求由你的浏览器直连高德服务器；全线坐标 GCJ-02 直叠无偏移。
        </div>
        <NButton
          size="small"
          :type="consent.engine.value === 'amap' ? 'primary' : 'default'"
          :disabled="!consent.amapKeyConfigured.value"
          @click="grantEngine('amap')"
        >
          高德
        </NButton>
        <NButton
          size="small"
          :type="consent.engine.value === 'leaflet' ? 'primary' : 'default'"
          @click="grantEngine('leaflet')"
        >
          Leaflet
        </NButton>
        <NButton size="small" @click="consent.revoke()">撤销授权</NButton>
      </template>
    </div>

    <div class="geo-body">
      <!-- ② 左栏：观察选择 -->
      <aside class="geo-rail geo-rail--left">
        <div class="geo-rail__head">地理画像观察</div>
        <NSpin v-if="listLoading" size="small" class="geo-rail__spin" />
        <div v-else-if="listError" class="geo-rail__err">{{ listError }}</div>
        <div v-else-if="!items.length" class="geo-rail__empty">
          本案件暂无地理画像观察。<br />
          可在研判画布对主体发起「落脚点画像 / 系列案件地理画像」镜头。
        </div>
        <div v-else class="geo-obs-scroll">
          <div v-if="serialItems.length" class="geo-rail__group">系列案件地理画像</div>
          <button
            v-for="o in serialItems"
            :key="o.observation_id"
            type="button"
            class="geo-obs"
            :class="{ 'geo-obs--active': o.observation_id === selectedId }"
            @click="void selectItem(o)"
          >
            <div class="geo-obs__line">
              <span class="geo-obs__subject">{{ o.subject || '—' }}</span>
              <NTag size="tiny" :bordered="false" type="warning">{{ lensLabel(o) }}</NTag>
            </div>
            <div class="geo-obs__meta">
              {{ o.created_at?.slice(0, 16) ?? '' }}
              <NTooltip v-if="o.directed">
                <template #trigger>
                  <NTag size="tiny" :bordered="false" type="info">定向</NTag>
                </template>
                由正兵从线索画布定向发起{{ o.origin?.clue_id ? `（${o.origin.clue_id}）` : '' }}
                ，案件级保留、不随重扫失效；另一条无此标记的是建案/重扫自动批量产出。
              </NTooltip>
              <NTag v-if="o.degraded" size="tiny" :bordered="false" type="error">降级</NTag>
            </div>
          </button>

          <div v-if="siteItems.length" class="geo-rail__group">落脚点画像</div>
          <button
            v-for="o in siteItems"
            :key="o.observation_id"
            type="button"
            class="geo-obs"
            :class="{ 'geo-obs--active': o.observation_id === selectedId }"
            @click="void selectItem(o)"
          >
            <div class="geo-obs__line">
              <span class="geo-obs__subject">{{ o.subject || '—' }}</span>
              <NTag size="tiny" :bordered="false">{{ lensLabel(o) }}</NTag>
            </div>
            <div class="geo-obs__meta">
              {{ o.created_at?.slice(0, 16) ?? '' }}
              <NTooltip v-if="o.directed">
                <template #trigger>
                  <NTag size="tiny" :bordered="false" type="info">定向</NTag>
                </template>
                由正兵从线索画布定向发起{{ o.origin?.clue_id ? `（${o.origin.clue_id}）` : '' }}
                ，案件级保留、不随重扫失效；另一条无此标记的是建案/重扫自动批量产出。
              </NTooltip>
              <NTag v-if="o.degraded" size="tiny" :bordered="false" type="error">降级</NTag>
            </div>
          </button>
        </div>
      </aside>

      <!-- ③ 中部：地图 -->
      <section class="geo-map-wrap">
        <EmptyState
          v-if="noCase"
          type="empty"
          title="未选择案件"
          desc="请先在顶部案件下拉中选择当前案件，地理画像按案件读取观察档案。"
        />
        <template v-else>
          <div class="geo-map-toolbar">
            <NCheckbox v-model:checked="layersVisible.cells">概率面</NCheckbox>
            <NCheckbox v-model:checked="layersVisible.sites">落脚点</NCheckbox>
            <NCheckbox v-model:checked="layersVisible.top">顶格排查区</NCheckbox>
            <span class="geo-map-toolbar__hint">
              产出为优先排查区域，非定址结论
            </span>
            <NButton size="tiny" quaternary @click="openObservation">
              查看观察详情/溯源 →
            </NButton>
          </div>
          <div class="geo-map-canvas">
            <NSpin v-if="detailLoading" size="small" class="geo-map-loading" />
            <template v-else-if="model">
              <AmapMap
                v-if="consent.engine.value === 'amap'"
                :key="`amap:${model.observationId}`"
                :model="model"
                :layers-visible="layersVisible"
                @select-site="onSelectSite"
                @failed="(msg: string) => (mapError = msg)"
              />
              <LeafletMap
                v-else-if="consent.engine.value === 'leaflet'"
                :key="`leaflet:${model.observationId}`"
                :model="model"
                :layers-visible="layersVisible"
                @select-site="onSelectSite"
                @failed="(msg: string) => (mapError = msg)"
              />
              <OfflinePlot
                v-else
                :model="model"
                :layers-visible="layersVisible"
                @select-site="onSelectSite"
              />
              <div v-if="showNoGeomOverlay" class="geo-map-nogeom">
                当前观察无任何可绘制坐标（落脚点全部未编码或事件不足 5 起）。
                可在右侧坐标表查看文本地址，或运行地理编码脚本补全坐标后重跑镜头。
              </div>
              <div v-if="mapError" class="geo-map-error">
                {{ mapError }}
                <NButton size="tiny" quaternary @click="grantEngine('leaflet')">
                  改用 Leaflet
                </NButton>
                <NButton size="tiny" quaternary @click="consent.revoke()">
                  回离线示意
                </NButton>
              </div>
            </template>
            <EmptyState
              v-else
              type="empty"
              title="暂无可绘制的地理画像"
              desc="从左侧选择一条观察；若该观察因事件不足或坐标缺口降级，地图将不产出概率面。"
            />
          </div>
        </template>
      </section>

      <!-- ④ 右栏：覆盖率 + 优先区 + 坐标表 -->
      <aside class="geo-rail geo-rail--right">
        <template v-if="model">
          <div class="geo-rail__head">
            {{ model.kind === 'serial' ? '系列地理画像' : '落脚点画像' }}
            <span class="geo-rail__sub">{{ model.subject }}</span>
          </div>

          <div v-if="model.degraded && model.degradedReason" class="geo-degrade">
            {{ model.degradedReason }}
          </div>

          <div class="geo-card">
            <div class="geo-card__title">坐标覆盖</div>
            <div class="geo-cov">
              <span class="geo-cov__num">{{ coverageText }}</span>
              <span v-if="coveragePercent !== null" class="geo-cov__pct">
                {{ coveragePercent }}%
              </span>
            </div>
            <div v-if="model.kind === 'serial'" class="geo-cov__lines">
              <span>纳入概率面 {{ model.eventsUsed ?? '—' }} 起</span>
              <span>无坐标丢弃 {{ model.eventsDroppedNoCoord ?? 0 }} 起</span>
              <span>网格 {{ model.grid.cols ?? '—' }}×{{ model.grid.rows ?? '—' }}
                （{{ model.gridMeters ?? '—' }}m）</span>
            </div>
            <div v-if="degradedSites.length" class="geo-cov__hint">
              {{ degradedSites.length }} 个落脚点无坐标：可运行
              <code>scripts/geocode_locations.py</code> 编码后重跑镜头。
            </div>
          </div>

          <div v-if="topZones.length" class="geo-card">
            <div class="geo-card__title">优先排查区域 Top {{ topZones.length }}</div>
            <table class="geo-table">
              <thead>
                <tr><th>#</th><th>纬度</th><th>经度</th><th>概率</th></tr>
              </thead>
              <tbody>
                <tr v-for="z in topZones" :key="z.rank">
                  <td>{{ z.rank }}</td>
                  <td class="geo-mono">{{ z.lat.toFixed(5) }}</td>
                  <td class="geo-mono">{{ z.lng.toFixed(5) }}</td>
                  <td class="geo-mono">{{ (z.probability * 100).toFixed(1) }}%</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="geo-card geo-card--grow">
            <div class="geo-card__title">
              落脚点坐标表
              <span class="geo-card__count">{{ coordSites.length }} 有坐标
                · {{ degradedSites.length }} 无坐标</span>
            </div>
            <div class="geo-sites-scroll">
              <table class="geo-table geo-table--sites">
                <thead>
                  <tr><th>地址 / 区划</th><th>到访</th><th>坐标与来源</th></tr>
                </thead>
                <tbody>
                  <tr
                    v-for="s in model.sites"
                    :key="s.locationId ?? s.stdAddress"
                    :class="{
                      'geo-row--active': (s.locationId ?? s.stdAddress) === selectedSiteKey,
                      'geo-row--degraded': s.coordDegraded,
                    }"
                    @click="onSelectSite(s)"
                  >
                    <td>
                      <div class="geo-addr">{{ s.stdAddress || '（空地址）' }}</div>
                      <div class="geo-admin">{{ s.adminPath || '无区划' }}</div>
                      <div class="geo-dates">{{ s.firstDate ?? '—' }} ~ {{ s.lastDate ?? '—' }}</div>
                    </td>
                    <td class="geo-visits">{{ s.visits }}</td>
                    <td>
                      <template v-if="!s.coordDegraded">
                        <div class="geo-mono">{{ fmtCoord(s.lat) }}, {{ fmtCoord(s.lng) }}</div>
                        <div class="geo-source">
                          {{ geocodeSourceLabel(s.geocodeSource) }}
                          <span v-if="s.geocodeConfidence !== null">
                            · {{ (s.geocodeConfidence * 100).toFixed(0) }}%
                          </span>
                        </div>
                      </template>
                      <span v-else class="geo-nocoord">无坐标</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>
        <EmptyState
          v-else-if="!detailLoading && !listLoading && items.length"
          type="empty"
          title="未选择观察"
          desc="从左侧选择一条地理画像观察。"
        />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.geo-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 56px - 24px);
  min-height: 520px;
  gap: 8px;
}

/* 授权横幅 */
.geo-consent {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
}
.geo-consent--off {
  background: var(--sun-warn-bg);
  border-color: var(--sun-warn-border);
  color: var(--sun-warn-text);
}
.geo-consent--on {
  background: var(--sun-info-bg);
  border-color: var(--sun-info-border);
  color: var(--sun-info-text);
}
.geo-consent__icon {
  font-size: 16px;
}
.geo-consent__text {
  flex: 1;
  color: var(--sun-text-secondary);
}
.geo-consent__text b {
  color: var(--sun-text-primary);
}

.geo-body {
  flex: 1;
  display: flex;
  gap: 8px;
  min-height: 0;
}

/* 左右栏通用 */
.geo-rail {
  width: 264px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
}
.geo-rail--right {
  width: 372px;
}
.geo-rail__head {
  padding: 10px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--sun-text-primary);
  border-bottom: 1px solid var(--sun-border);
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.geo-rail__sub {
  font-size: 11px;
  font-weight: 400;
  color: var(--sun-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.geo-rail__spin,
.geo-rail__err,
.geo-rail__empty {
  padding: 16px 12px;
  font-size: 12px;
  color: var(--sun-text-secondary);
  line-height: 1.8;
}
.geo-rail__err {
  color: var(--sun-error-text);
}
.geo-rail__group {
  padding: 10px 12px 4px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  letter-spacing: 0.05em;
}
.geo-obs-scroll {
  flex: 1;
  overflow-y: auto;
  padding-bottom: 8px;
}
.geo-obs {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 12px;
  border: 0;
  border-left: 2px solid transparent;
  background: transparent;
  color: var(--sun-text-secondary);
  cursor: pointer;
  font: inherit;
}
.geo-obs:hover {
  background: var(--sun-bg-card-hover);
}
.geo-obs--active {
  background: var(--sun-bg-card-hover);
  border-left-color: var(--sun-border-active);
}
.geo-obs__line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.geo-obs__subject {
  font-size: 12px;
  color: var(--sun-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.geo-obs__meta {
  margin-top: 3px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--sun-text-tertiary);
  font-family: var(--sun-font-mono);
}

/* 地图区 */
.geo-map-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.geo-map-toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 6px 12px;
  background: var(--sun-bg-card);
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.geo-map-toolbar__hint {
  flex: 1;
  color: var(--sun-text-tertiary);
}
.geo-map-canvas {
  position: relative;
  flex: 1;
  min-height: 0;
  border: 1px solid var(--sun-border);
  border-radius: 6px;
  overflow: hidden;
  background: var(--sun-bg-base);
}
.geo-map-loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--sun-bg-base);
  z-index: 5;
}
.geo-map-nogeom {
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
.geo-map-error {
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
  z-index: 10;
}

/* 右栏卡片 */
.geo-degrade {
  margin: 8px 12px 0;
  padding: 6px 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--sun-warn-text);
  background: var(--sun-warn-bg);
  border: 1px solid var(--sun-warn-border);
  border-radius: 4px;
}
.geo-card {
  margin: 8px 12px 0;
  padding: 8px 10px;
  border: 1px solid var(--sun-border);
  border-radius: 4px;
  background: var(--sun-input-bg);
}
.geo-card--grow {
  flex: 1;
  min-height: 120px;
  display: flex;
  flex-direction: column;
  margin-bottom: 12px;
}
.geo-card__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--sun-text-primary);
  margin-bottom: 6px;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.geo-card__count {
  font-weight: 400;
  font-size: 11px;
  color: var(--sun-text-tertiary);
}
.geo-cov {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.geo-cov__num {
  font-size: 13px;
  color: var(--sun-text-primary);
}
.geo-cov__pct {
  font-size: 18px;
  font-weight: 700;
  color: var(--sun-border-active);
  font-family: var(--sun-font-mono);
}
.geo-cov__lines {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: var(--sun-text-secondary);
}
.geo-cov__hint {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--sun-warn-text);
}
.geo-cov__hint code {
  font-family: var(--sun-font-mono);
  color: var(--sun-warn-text);
}

.geo-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
.geo-table th {
  text-align: left;
  font-weight: 500;
  color: var(--sun-text-tertiary);
  padding: 3px 6px;
  border-bottom: 1px solid var(--sun-border);
}
.geo-table td {
  padding: 4px 6px;
  color: var(--sun-text-secondary);
  vertical-align: top;
  border-bottom: 1px solid rgba(16, 49, 74, 0.5);
}
.geo-mono {
  font-family: var(--sun-font-mono);
  font-size: 10.5px;
  white-space: nowrap;
}
.geo-sites-scroll {
  flex: 1;
  overflow-y: auto;
  margin: 0 -10px -8px;
  padding: 0 10px 8px;
}
.geo-table--sites tbody tr {
  cursor: pointer;
}
.geo-table--sites tbody tr:hover td {
  background: var(--sun-bg-card-hover);
}
.geo-row--active td {
  background: rgba(110, 222, 233, 0.08);
}
.geo-row--degraded {
  opacity: 0.72;
}
.geo-addr {
  color: var(--sun-text-primary);
  max-width: 180px;
}
.geo-admin,
.geo-dates {
  margin-top: 1px;
  font-size: 10.5px;
  color: var(--sun-text-tertiary);
}
.geo-visits {
  text-align: center;
  color: var(--sun-gold);
  font-family: var(--sun-font-mono);
}
.geo-source {
  font-size: 10.5px;
  color: var(--sun-text-tertiary);
}
.geo-nocoord {
  color: var(--sun-error-text);
  font-size: 11px;
}
</style>

<style>
/* Leaflet tooltip 挂在 body，scoped 不生效，需全局样式对齐暗色主题 */
.geo-tooltip {
  background: rgba(5, 21, 34, 0.95) !important;
  border: 1px solid #10314a !important;
  border-radius: 4px !important;
  color: #e9f7fa !important;
  font-size: 11px !important;
  line-height: 1.6 !important;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.5) !important;
}
.geo-tooltip::before {
  border-top-color: #10314a !important;
}
.leaflet-bar a {
  background: #051522;
  color: #9fb8c6;
  border-color: #10314a;
}
.leaflet-bar a:hover {
  background: #072233;
  color: #6edee9;
}
</style>
