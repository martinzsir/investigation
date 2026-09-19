# 线索研判画布 UX 优化（P0）实施计划

> 对应线索 `clue_4c680cc3`（demoW）体验反馈五痛点：方块无释义、展开入口深、
> 配色连线不清、布局无追溯感、明细行全量铺开。
> 范围 = 已确认的 P0 六项（方案 1–6）。**本批零后端契约变更、零数据迁移**，
> 全部在前端渲染/视图层解决；P1（barycenter 布局、显式钉住、小地图）与
> P2（维度泳道、汇报模式）见文末附录，不在本批实施。

## 一、仓库调研结论（Repository Research）

### 1. 实证数据（该线索持久化画布，cases/demoW/state.sqlite）

- 43 节点 / 48 边，画布区固定高 580px；构成：规则 1、事实 12、实体 12、
  数据行 12、数据源 1、待核实 5。
- **12 个数据行节点 label 全部是「招投标档案」**（`canvas_seed._row_label`
  只取数据源名），节点之间零区分性。
- 22/43 节点 `pinned=true`（拖动即隐式钉住，用户无感知）。
- 边关系 5 种语义（命中 12/来源行 15/中标参与 12/所属文件 6/涉及 3），
  当前全部同色同形 1.2px cubic 线。

### 2. 现状架构关键事实

- 成图：`server/app/canvas_seed.py` 种子生成 rule/fact/source_row/
  verify_item/evidence；`server/app/canvas_expand.py` 懒加载 object/
  source_file，**expand 结果经 `merge_expansion` 持久化进 doc**。
- 折叠：`frontend/src/domain/canvas-collapse.ts` 的可见性模型只隐藏
  "非种子 + expand 引入"的图层；**刷新后 expand 节点全部变成种子节点，
  再也折叠不回去**（这 12 个实体节点即如此固化）。种子行层从设计上就不可折叠。
- 渲染：`ResearchCanvas.vue` 用 G6 **v5.1.1**（`@antv/g6 ^5.1.1`），
  内置 rect 节点 168×42 纯填色；只监听 `node:click`（开抽屉）与
  `node:dragend`；无图例、无列头、无 hover 联动、无小地图。
- 配色：`KIND_COLORS` 10 色硬编码在组件内，违反 FE-D-011
  （tokens 单一色板）；系统边 `#4a6b82`/1.2px 在画布底色 `#051522`
  上对比度最低——主干溯源链反而最看不清。
- 维度：系统声明的是**五维**（`ontology/default/dimensions.json`：
  资金/通讯/行为/关系/时间），`design/tokens.ts` 已有五维色板
  （`--sun-jian-*`）；规则维度可经既有 `canvasApi.ruleAudit()` 取得
  （`RuleAudit.dimension`），无需新端点。
- 遮蔽纪律（RC-104）：种子 doc 是 system 旁路预生成、无 per-user 身份，
  **节点上不允许新增持久化的明文字段摘要**；行字段明文只能走既有
  expand→`source_row_dto` 遮蔽 DTO（抽屉/TraceabilityPanel 路径）。
- 测试面：`canvas-expand.spec.ts` 直接测 `hiddenSets/visibleDoc` 并
  mount ResearchCanvas；`research-canvas.spec.ts`、`canvas-m3-ui.spec.ts`、
  `canvas-m4-ui.spec.ts` mount 组件；`canvas-layout.spec.ts` 测布局纯函数
  （本批不动 doc 坐标，不应受影响）。
- 前端 `node_modules` 当前未安装，验证前需在 WSL 内 `pnpm install`。

### 3. 核心设计决策

**把"明细折叠"做成前端渲染层的视图投影，而不是改 doc/种子契约。**
doc 仍完整持有全部节点与坐标（自动保存、快照、回滚语义全部不变），
新增一个纯函数视图模型决定"此刻渲染哪些节点"。由此：

- 老画布（含本线索已固化的 12 个实体节点）立即受益，无需 reseed/迁移；
- 不触碰 `validate_patch`、审计链、快照、RC-104 遮蔽任何一条既有纪律；
- 默认视图从 43 节点降到约 18 个骨干节点（规则+事实+待核实+书证+假设）。

## 二、交互设计（评审重点）

### 1. 两档视图

| 视图 | 可见内容 | 切换 |
|------|----------|------|
| **简洁视图（默认）** | 骨干层：规则/事实/待核实/书证/假设/备注/查询结果；object/source_row/source_file 三类明细默认隐藏 | 图例区开关；偏好按 `canvas-view:{caseId}:{clueId}` 存 localStorage |
| **完整视图** | 等同今天的全量成图 | 同一开关，一键返回 |

明细节点的归属：沿系统边（`命中/涉及/来源行/所属文件` 及 links.title
类系统边如「中标参与」）从每个 fact 做有界 BFS，得到
`{objects, rows, files}` 分组。多事实共享节点取**生产者并集**
（延续 canvas-collapse 既有纪律：所有归属事实都折叠时才隐藏；
任一事实展开即显示）。

### 2. 节点上的直接操作（不再必须开抽屉）

| 动作 | 行为 |
|------|------|
| 单击节点体 | 打开详情抽屉（现状不变；为避免与双击冲突，延迟 250ms，双击到达则取消） |
| **双击节点体** | 展开/折叠该事实的明细分组（fact=实体+行+文件；object=邻居；row=文件） |
| **hover 节点右上 +/− 徽标** | + 表示存在未展开明细、− 表示已展开；单击徽标等同双击；叶子节点（文件/无明细）不显示 |
| **单击事实节点的「N 行」「N 实体」计数胶囊** | 弹预览层（不开抽屉）：分组清单，行显示"数据源名 · 行 N"序号与缺失/未登记/表级汇总状态；点单条 → 打开该行抽屉（走既有懒加载+遮蔽 DTO）；实体/文件项点击 → 定位该节点并开抽屉 |
| hover 任意节点 | 高亮其一跳上下游路径（focus chain），其余元素降透明度 0.25 |
| 单击节点（选中态） | 持续高亮路径；点空白/Esc 取消选中 |

展开分组但 doc 中尚无对应节点时（老画布以外的情况），复用现有
`doExpand(nodeId, direction)` 懒加载链路，四态机（expanding/失败重试）
不变；节点已存在时只切可见性，不发请求。

### 3. 节点视觉（卡片化，深色主题）

- 统一卡片：深色卡面（取 tokens 卡片底）+ 左侧类型图标圆底（中文单字：
  规/实/体/行/档/核/证/假/备/查，图标底色=类型色）+ 主标题 + 副标题
  + 右侧状态点；建议虚节点保留虚线描边、失效节点保留 0.4 透明并加「失效」角标。
- 副标题一律使用 doc 内既有 props 派生（**不引入明文字段**）：
  fact="N 条来源行 · M 个实体"；object=`props.type_title`；
  source_row="数据源名 · 行 N（组内确定序）"+缺失/未登记/表级汇总标记；
  verify_item=`props.status`；evidence=类型+上传日期；
  hypothesis=内容首句；function_result=函数业务名；rule=维度名（懒取 ruleAudit）。
- 规则/事实节点左上角加**维度色点**（五维色板；多规则多色），
  直观看出"哪几维交叉命中"；维度未取到时不显示，不阻塞渲染。
- 连线：系统溯源边改高对比青灰实线 1.5px+箭头，人工边暖色虚线
  （维持现有语义区分）；关系标签默认隐藏（含义进图例），进入 focus
  路径或缩放 ≥0.9 时显示，带深色文字底衬保证可读。
- 追溯方向感：G6 数据注入层（非 doc、不落库）追加 5 个**列头泳道标签**
  （规则→事实/待核实→实体/研判→数据行→数据源，id 前缀 `lane:`，
  不参与任何交互/查找/findNode）与极淡列底色带。
- 画布左上/右上常驻 **HTML 图例层**：类型图标+名称、维度色点含义、
  两种线型说明；图例同时是层开关（实体/数据行/数据源三个独立开关 +
  简洁/完整视图切换）。

### 4. G6 技术选型与兜底

- 首选：`G6.registerNode('research-card', …)` 注册自定义卡片节点
  （图标圆/双行文本/状态点/+-徽标均为具名子图形，事件经现有
  `ev.target.id` 委托识别，与当前 node:click 解析方式一致）。
- **步骤 0 先做技术验证（spike）**：WSL 装依赖后，最小 demo 验证
  ① registerNode 自定义节点 ② `node:dblclick` 事件 ③ 子图形点击的
  target 命中 ④ 元素 state 驱动 opacity。任一不成立则降级为：
  内置 rect + `iconText`（中文单字图标）+ 单 badge（+/−）+
  副标题进 hover tooltip；focus chain、图例、列头等其余方案不受影响。

## 三、文件与模块变更（Files and Modules）

| 文件 | 变更 |
|------|------|
| `frontend/src/design/tokens.ts` | 新增画布专用色板导出（10 类型图标色、系统/人工边色、列底带色、徽标色），全部在 tokens 取色；五维色复用现有 `jian.*` |
| `frontend/src/domain/canvas-view.ts` | **新增（纯函数，本批核心）**：明细分组 BFS、生产者并集、视图投影（visible 集合+每 fact 计数徽标+可展开判定）、focus chain 一跳集合 |
| `frontend/src/components/research/g6-card-node.ts` | **新增**：注册 `research-card` 自定义节点（spike 通过后落地；否则该文件不建，走降级样式） |
| `frontend/src/components/research/ResearchCanvas.vue` | 接入视图模型替换 `hiddenSets`；自定义节点/新边样式/state；双击与 250ms 单击延迟；+-徽标与计数胶囊事件；hover/选中 focus chain；泳道列头注入；规则维度懒取 |
| `frontend/src/components/research/CanvasLegend.vue` | **新增**：图例 + 三层开关 + 简洁/完整切换（纯展示，事件上抛） |
| `frontend/src/components/research/FactDetailPopover.vue` | **新增**：事实明细预览层（行/实体/文件三个分组清单；行序号与状态；点击跳抽屉），字段明文不进本组件 |
| `frontend/src/components/research/CanvasToolbar.vue` | 增加「展开全部明细/折叠全部明细」两个小按钮（与图例开关同源状态） |
| `frontend/src/components/research/CanvasNodeDrawer.vue` | 展开/折叠按钮改为调用新视图模型（事件名不变）；其余不动 |
| `frontend/src/domain/canvas-collapse.ts` | 被 canvas-view 取代后删除；`canvas-expand.spec.ts` 中其纯函数测试迁移为 canvas-view 分组/并集测试（语义等价保留） |
| `frontend/tests/canvas-view.spec.ts` | **新增**：分组 BFS、多事实共享并集、简洁/完整投影、计数徽标、focus chain、手动节点恒可见、`lane:` 伪节点不进投影 |
| `frontend/tests/research-canvas.spec.ts` | 更新：默认简洁视图下明细节点不渲染（断言喂给 G6 的 data）、计数胶囊存在、切完整视图后全量；骨架/错误/降级断言保留 |
| `frontend/tests/canvas-expand.spec.ts` | collapse 纯函数测试迁至 canvas-view；组件级展开/折叠/重试四态断言改用新交互（双击/徽标事件模拟） |
| `frontend/tests/canvas-m3-ui.spec.ts` / `canvas-m4-ui.spec.ts` | 适配选择器/视图默认态变化（人工节点、建议采纳、扩展查询的既有行为断言不变） |

**后端：无改动。** 契约、端点、`state.sqlite` 表结构、快照、审计、权限
全部不动；`CanvasDoc/CanvasNode/CanvasEdge` 类型不新增字段。

## 四、实施步骤（依赖顺序）

1. **Spike（G6 能力验证，约半天内）**：WSL `pnpm install`，最小页面验证
   registerNode/dblclick/子图形命中/state 四项；结论记录在本文件，
   决定走自定义节点还是降级样式。
2. tokens.ts 增加画布色板（无依赖、先立调色纪律）。
3. `domain/canvas-view.ts` 纯函数 + `tests/canvas-view.spec.ts`
   （分组、并集、投影、徽标计数、focus chain；不依赖 G6，先行可测）。
4. 迁移折叠接线：ResearchCanvas 以视图模型替换 canvas-collapse，
   双击/徽标/计数胶囊事件与 250ms 单击延迟；展开缺节点时仍走 doExpand；
   抽屉展开按钮改接新模型；删除 canvas-collapse.ts。
5. 视觉落地：按 spike 结论做卡片节点、边样式、state 联动、泳道列头注入。
6. CanvasLegend + FactDetailPopover + 工具栏全开/全合按钮；
   localStorage 视图偏好；规则维度懒取（ruleAudit 缓存）。
7. 前端测试更新补齐（见三、文件表），跑 vitest 相关规格 + `npm run build`
   （vue-tsc 类型检查）。
8. 浏览器人工验收 demoW 线索画布（见五、验收清单）。

## 五、验收清单（Validation）

功能/视觉：

1. 首次打开 clue_4c680cc3：默认简洁视图只剩骨干（规则 1+事实 12+
   待核实 5 约 18 节点），不再出现一屏 43 节点；每个事实显示
   「12 行」「N 实体」计数（实体仅在曾展开并落库时计数）。
2. 双击任一事实 / 点 + 徽标：该事实明细展开，− 出现；再双击收回；
   共享实体只在所有归属事实都折叠时隐藏。
3. 点「N 行」胶囊：弹出清单，12 行显示为 招投标档案 · 行 1…行 12，
   点单行开抽屉、字段仍走遮蔽口径（MaskedField 行为不变）。
4. hover/选中节点：一跳路径高亮、其余变暗；Esc/点空白取消。
5. 图例常驻且可点：三层开关、简洁/完整切换生效并持久化（刷新仍为简洁）。
6. 节点卡片显示类型图标+中文名+副标题；规则节点可见维度色点；
   建议虚线、失效置灰角标保留。
7. 泳道列头五段标签随平移移动；`lane:` 元素不响应点击、不开抽屉。
8. 完整视图与历史行为一致；快照回滚后视图模型对新 doc 立即正确分组；
   降级列表（RC-207）仍显示全量节点与关系。
9. 既有 M3/M4 行为不回归：人工假设/备注增删改、连线矩阵、
   建议采纳 202 链路、扩展查询、快照、自动保存坐标。

命令（按用户约定，**收到明确指示后再执行**，默认 WSL）：

```bash
# 前端单测（相关规格）
cd /mnt/d/dev/inves_duckdb/frontend && npx vitest run \
  tests/canvas-view.spec.ts tests/research-canvas.spec.ts \
  tests/canvas-expand.spec.ts tests/canvas-m3-ui.spec.ts \
  tests/canvas-m4-ui.spec.ts tests/canvas-relations.spec.ts \
  tests/canvas-layout.spec.ts
# 类型检查/构建
cd /mnt/d/dev/inves_duckdb/frontend && npm run build
```

后端无改动，无需跑 `run_tests.py`/`mcp_client_test`。

## 步骤 0 Spike 结论（2025-09 静态源码级，G6 5.1.1）

未跑运行时 demo，四项能力均由 `node_modules/@antv/g6/esm` 类型/源码直接证实，
**采用首选方案（自定义 research-card 节点），不启用兜底**：

1. **自定义节点**：`register(ExtensionCategory.NODE, 'research-card', Ctor)`
   （`esm/registry/register.d.ts`）；Ctor 继承 `elements/nodes/rect` 的 Rect
   （或 BaseNode），重写 `render(attributes, container)`：
   `this.upsert('chip', Circle, style, container)` / `upsert('chip-text', Text)` /
   `upsert('subtitle', Label)`，Label/Circle/Text 均可从 `@antv/g6` 根导出
   （`esm/shapes/index`）。
2. **双击事件**：`NodeEvent.DBLCLICK = "node:dblclick"` 存在
   （`constants/events/node.d.ts`），另有 `node:pointerover/pointerleave`。
3. **子图形命中**：`upsert(className, Ctor, …)` 以 `{className}` 构造图形
   （`elements/shapes/base-shape.js:60-90`），事件侧
   `ev.originalTarget` 可向上回溯 className；徽标经内置
   `getBadgesStyle` 命名为 `badge-0/badge-1/…`（位置
   `right-top/left-bottom`，pill 用 `backgroundRadius:8, padding:[2,6]`，
   缺省 `backgroundRadius:'50%'` 圆形）。
4. **state 驱动样式**：node/edge spec 支持
   `state: { [name]: style | (data)=>style }`
   （`spec/element/node.d.ts:32`）；`graph.setElementState(id, state[])`
   及批量重载 `graph.setElementState(Record<ID, State[]>)`
   （`runtime/graph.d.ts:1173-1182`）→ focus chain 用一次批量调用落
   dim/focus/selected，无需逐元素重设。

## 六、风险与处置（Risks）

- **G6 v5.1 自定义节点 API 与预期不符**：步骤 0 spike 前置验证；
  兜底内置 rect+iconText+单 badge，副标题移至 tooltip，其余范围不缩水。
- **双击误触两次单击开抽屉**：250ms 延迟+取消计时器；以组件测试
  模拟 click×2 断言抽屉不抖动、分组被切换。
- **多事实共享节点/边的隐藏错误**：生产者并集纯函数穷举测试
  （双归属、三归属、全折叠/半展开）；边随两端可见性过滤（沿用 visibleDoc）。
- **简洁视图"信息藏太深"反弹**：计数胶囊给明确预览入口；完整视图
  一键可达；降级列表恒为全量。
- **遮蔽纪律被破坏**：节点副标题禁止任何字段值，只许用 doc 既有
  业务 props 与组内序号；预览层不直出明文，看字段必须开抽屉走 DTO；
  代码评审以此为硬检查项。
- **旧画布兼容**：视图模型是纯派生，老 doc（含本线索 22 pinned、
  固化实体）直接生效；坐标/钉住/快照/回滚一律不触碰。
- **性能**：43 节点规模 BFS/投影可忽略；G6 state 批量设置在单跳集合上
  做（全量 dim/复位），避免逐元素闪烁。

## 附录：本批不做（P1/P2  backlog）

- P1：列内 barycenter/dagre 交叉最小化（扩 canvas-layout 纯函数）；
  钉住显式化（图钉按钮+「全部取消钉住」，可化解本线索 22 隐形 pin）；
  小地图（G6 插件能力随 spike 一并验证）。
- P2：维度泳道/维度过滤；「汇报模式」一键只剩骨干链；
  行预览批量 DTO 端点（一次取多行遮蔽字段，减少逐行打开）。
