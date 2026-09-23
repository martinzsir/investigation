# 观察图层时间轴面板（独立底部抽屉）

## Context

当前「观察图层」通过 `layoutObservationLayer` 算坐标后合并进 `graphDoc`，在同一个 G6 主画布渲染（[ResearchCanvas.vue#L938-L945](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L938-L945)）。这导致两个问题：
1. 观察层与主画布关系图共用坐标系，需独立 y 偏移避让，复杂且脆弱——用户反馈"和主图层在一起"；
2. G6 是关系图引擎，时间分档堆叠是伪时间轴，不支持原生缩放/平移，区间节点要靠自定义 rect + bandWidth 模拟。

用户决策：用 `vue-timeline-chart` 组件做**独立底部抽屉**的时间轴面板，**按节点时间字段自动判断**启用（有 event_time 或 start/end 就启用，无时间字段显示空态）。

## 方案概览

- **后端不变**：`build_observation_layer`（[canvas_seed.py#L1110](file:///d:/dev/inves_duckdb/server/app/canvas_seed.py#L1110)）仍产出 `observation_layer` payload，节点 props 保留 `event_time`/`start`/`end`/`interval_kind`/`created_at`/`obs_of`/`obs_layer`
- **前端改造**：观察层不再合并进 `graphDoc`，`observationLayerOn` 改为控制底部抽屉显示；新组件 `ObservationTimelinePanel.vue` 把 `observationLayerRaw.nodes` 转成 timeline items + groups 渲染
- **数据复用**：复用 [canvas-layout-time.ts](file:///d:/dev/inves_duckdb/frontend/src/domain/canvas-layout-time.ts) 的 `nodeTimestamp`/`intervalRange`/`buildTimeAxis` 做时间字段提取（只复用数据层，不用 G6 坐标层）

## 步骤

### 1. 安装依赖

```
cd d:\dev\inves_duckdb\frontend && pnpm add vue-timeline-chart
```

### 2. 新建 `frontend/src/components/research/ObservationTimelinePanel.vue`

**props/emits**：
```ts
defineProps<{
  show: boolean
  nodes: CanvasNode[]
  timeMode: TimeMode               // = effectiveTimeMode
  observationCount?: number
}>()
const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'pick-observation', observationId: string): void
}>()
```

**数据转换**（复用 `nodeTimestamp`/`intervalRange`/`buildTimeAxis`）：
- 事件点节点（`props.event_time`）→ `{ type: 'point', start: Date.parse(event_time) }`
- 区间节点（`props.start/end/interval_kind`）→ `{ type: 'range', start, end, cssVariables: { '--item-background': bandColor(kind) } }`
- 普通对象节点（subject/project，无时间字段）→ 不发 item
- 按 `props.obs_of` 分组 → `groups: [{ id: obs_of, label: obs_skill_id }]`
- `hasTimeData = timed > 0`（自动启用条件）；`false` 时渲染 `<NEmpty>` 提示"当前口径下无时间字段，尝试切回 process 口径"

**视口**：`viewportMin/Max = axis.from/to`（`buildTimeAxis` 算出），`initialViewportStart/End` = 全范围；无时间时回落 `Date.now()-1d ~ Date.now()`。

**模板骨架**：
```vue
<NDrawer :show="show" placement="bottom" :height="340" :z-index="920" @update:show="emit('update:show', $event)">
  <NDrawerContent title="观察图层时间轴" closable>
    <NEmpty v-if="!hasTimeData" description="..." />
    <Timeline v-else :items :groups :viewport-min :viewport-max @changeViewport="...">
      <template #item="{ item }">
        <div @click="emit('pick-observation', item.group)" :title="...">...</div>
      </template>
    </Timeline>
  </NDrawerContent>
</NDrawer>
```

**bandColor**：burst → 橙色（与现有 `canvasTokens.band.burst.stroke` 对齐），collision_window → 蓝色。

### 3. 改造 `ResearchCanvas.vue`

| 位置 | 操作 |
|---|---|
| [L731-L750](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L731-L750) `observationLayerDoc` computed | **删除**（不再算 G6 坐标） |
| [L938-L945](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L938-L945) `graphDoc` 内 obs 层合并块 | **删除**，`graphDoc` 简化为仅处理 `collapsedChain` |
| [L3017](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L3017) `watch(observationLayerOn, () => pushGraphData)` | **删除**（不再喂 G6） |
| [L2742-L2744](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L2742-L2744) `d.data?.observationLayer === true` 描边分支 | **删除**（obs 节点不上 G6） |
| [L425-L427](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L425-L427) `toG6Data` 里 `observationLayer` 字段赋值 | **删除**（dead code） |
| [L2650-L2657](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L2650-L2657) `onToggleObservationLayer` 文案 | 改为"已展开观察时间轴（底部抽屉）" |
| [L176-L193](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L176-L193) `observationLayerRaw/On/hasObservationLayer`、L2667 `load()`、L3129-3132 CanvasToolbar 绑定 | **保留**（按钮仍是开关） |
| 模板新增 | 在 `</CanvasToolbar>` 后挂 `<ObservationTimelinePanel :show="observationLayerOn" :nodes="observationLayerRaw.nodes ?? []" :time-mode="effectiveTimeMode" :observation-count="observationCount" @update:show="observationLayerOn = $event" @pick-observation="openOriginLensObservation" />` |
| 抽屉开闭时触发 G6 resize | `@update:show` 内 `nextTick(() => inst?.resize?.())` |

### 4. 改造 `frontend/src/domain/canvas-layout-time.ts`

- **保留**：`buildTimeAxis`/`nodeTimestamp`/`intervalRange`/`layoutByTime`/`layoutTimeBands`（主画布 time 视角 + 新面板都在用）
- **删除**：`layoutObservationLayer`、`observationLayerHeight`、`OBS_LAYER_GAP`（改造后无引用）

### 5. 改造 `CanvasToolbar.vue`

[L347-L368](file:///d:/dev/inves_duckdb/frontend/src/components/research/CanvasToolbar.vue#L347-L368) tooltip 文案改为"展开观察时间轴（底部抽屉）"，状态/事件不变。

## 风险点

1. **边数据无法表达**：vue-timeline-chart 只表达时间 item，无法画 obsedge 关系边。当前 `observationLayerRaw.edges` 为 0 条，暂无影响；若未来后端产生 obs 边，需在 `#item` slot 内手绘 SVG 或忽略。
2. **抽屉挤压主画布视口**：底部抽屉 `:height=340` 会让 G6 容器可视高度收缩，需在 `@update:show` 内触发 `inst?.resize?.()` + `applyInitialViewport()` 重排。
3. **event 口径下部分节点无时间**：事件点/区间有 `event_time`/`start/end`，但 subject/project 普通对象节点无时间字段 → 不出现在 timeline，这是预期行为（业务时间只对业务事件有意义）。
4. **observationCount 与 items 数维度不同**：按钮 tooltip 文案保持"X 条观察 / Y 个时间 item"区分。

## 验证（demoC/clue_a84c326b）

1. `cd d:\dev\inves_duckdb\frontend && pnpm dev`，浏览器打开 demoC/clue_a84c326b 画布
2. 主画布**不再出现** obs:: 前缀节点（橙色描边特殊色块消失）
3. 点 CanvasToolbar「观察图层」按钮 → 底部抽屉滑出，时间轴显示 point/range 类型 items，按 `obs_of` 分泳道
4. 切换 `timeMode`：process ↔ event；process 下所有 obs 节点都参与（created_at），event 下事件点+区间参与、普通对象不参与
5. 点 timeline item → 触发 `openOriginLensObservation`（[L159](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue#L159)），镜头观察抽屉正确打开
6. 关闭抽屉 → G6 容器触发 resize/refit，主画布视口恢复正常
7. 跑 `pnpm test`，关注 ResearchCanvas 相关快照断言（G6 数据中不再含 obs 节点，快照需更新）
