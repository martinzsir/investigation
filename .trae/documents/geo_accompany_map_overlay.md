# 地图视图叠加时空伴随连线 — 实施方案

## 背景与问题

时空伴随观察（`geo_accompany`）已在 `cases/testF/artifacts/directed_observations.json` 中产出，包含张卫国 × 李志强 3 次反复同框数据。但当前地图视图（`c/geo`）**只展示落脚点画像和系列画像**，不展示时空伴随。用户要求在地图上以连线方式呈现时空伴随关系。

## 数据基础

时空伴随观察 `detail.companion` 字段结构：
```json
{
  "person_a": "张卫国",
  "person_b": "李志强",
  "meet_count": 3,
  "repeated": true,
  "first_date": "2020-01-07",
  "last_date": "2021-02-21",
  "locations": ["浙江省/杭州市/西湖区/..."],
  "location_count": 1,
  "spatial_level": "same_road",
  "spatial_note": "同一区县同一路段（门牌可能不同）"
}
```

**关键限制**：`locations` 是文本地址数组，**无坐标**。坐标需从同主体的 `geo_site_profile` 观察中按 `std_address` 匹配获取。

## 实施方案

### 1. 扩展 geoMap.ts 数据模型

新增 `GeoLink` 接口与 `GeoLayerKind` 扩展：

```typescript
export type GeoLayerKind = 'site' | 'serial' | 'accompany'

export interface GeoLink {
  personA: string
  personB: string
  meetCount: number
  repeated: boolean
  firstDate: string | null
  lastDate: string | null
  spanDays: number | null
  spatialNote: string
  /** 连线端点坐标（从 site 观察匹配；缺坐标端点为 null 时跳过绘制） */
  coordA: { lat: number; lng: number } | null
  coordB: { lat: number; lng: number } | null
}
```

`GeoLayerModel` 新增 `links: GeoLink[]` 字段。

### 2. 扩展 parseGeoObservation 支持 accompany 类型

- 识别 `skill_id === 'geo_accompany'`，`kind = 'accompany'`
- 从 `detail.companion` 提取 personA/personB、meetCount 等
- **不解析坐标**（accompany 观察本身不含坐标），坐标由视图层从同主体 site 观察匹配补入

### 3. GeoMapView.vue 加载与匹配逻辑

`loadList()` 增加拉取 `geo_accompany` 观察：
```typescript
const [rSerial, rSites, rAccompany] = await Promise.all([
  observationsApi.list(cid, { skill: GEO_SKILL_SERIAL, ... }),
  observationsApi.list(cid, { skill: GEO_SKILL_SITE, ... }),
  observationsApi.list(cid, { skill: GEO_SKILL_ACCOMPANY, ... }),
])
```

左栏新增「时空伴随」分组，显示 `person_a × person_b` 标题。

`selectItem()` 中，当选中 accompany 观察时：
1. 从 `items` 中找到 personA 和 personB 各自的 `geo_site_profile` 观察
2. 加载它们的详情，提取 sites
3. 按 `detail.companion.locations` 中的文本地址，在两侧 sites 中匹配 `stdAddress`
4. 将匹配到的坐标填入 `GeoLink.coordA` / `coordB`
5. 若任一端点无坐标，该 link 不绘制（但保留在列表中显示文本信息）

### 4. 地图组件渲染连线

三个引擎各自实现连线绘制：

- **AmapMap.vue**：使用 `AMap.Polyline`，虚线样式（`strokeStyle: 'dashed'`），颜色用 `geoMapTokens.accompany`（新增 token，建议琥珀色 #F2B54D 或青色系）
- **LeafletMap.vue**：使用 `L.polyline`，`dashArray: '6,4'`
- **OfflinePlot.vue**：SVG `<line>` 元素，虚线 `stroke-dasharray`

连线 zIndex 低于落脚点 marker、高于概率面格子。

### 5. 右栏信息展示

当选中 accompany 观察时，右栏显示：
- 主体对：personA × personB
- 同框次数、时间跨度、空间判据
- 涉及地点列表（文本）
- 坐标覆盖提示（如「2/2 个端点已定位」）

### 6. 图层开关

`layersVisible` 新增 `links: boolean` 字段，工具栏增加「伴随连线」开关。

## 关键文件

| 文件 | 改动 |
|------|------|
| `frontend/src/domain/geoMap.ts` | 新增 GeoLink、GeoLayerKind 扩展、parseAccompany、匹配辅助函数 |
| `frontend/src/views/GeoMapView.vue` | 加载 accompany 观察、左栏分组、selectItem 匹配逻辑、右栏信息、layersVisible.links |
| `frontend/src/components/geo/AmapMap.vue` | 新增 Polyline 绘制 |
| `frontend/src/components/geo/LeafletMap.vue` | 新增 polyline 绘制 |
| `frontend/src/components/geo/OfflinePlot.vue` | 新增 SVG line 绘制 |
| `frontend/src/design/tokens.ts` | 新增 accompany 连线 token |

## 验证方式

1. 刷新 `c/geo` 页面，左栏应出现「时空伴随」分组（1 条：张卫国 × 李志强）
2. 点击该条目，地图应显示虚线连接两个主体的高频落脚点（文三路区域）
3. 切换「伴随连线」开关，连线应显隐正常
4. 离线/Leaflet/高德三引擎下连线均正常显示
5. 运行 `python run_tests.py --only geo` 确保无回归

## 风险与降级

- **地址匹配失败**：`companion.locations` 文本与 site 观察的 `stdAddress` 格式可能不完全一致（如前缀差异）。匹配失败时该端点坐标为 null，连线不绘制，右栏显示「端点未定位」提示。
- **多地点伴随**：当前数据只涉及 1 个地点（文三路）。若未来涉及多个地点，只连线 meetCount 最高的那个地点对。
- **坐标缺失**：若某主体完全没有 site 观察（未跑过落脚点画像），则无法定位，连线跳过。
