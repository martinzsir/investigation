# 六项解耦 · Server 端与 Frontend 端调整评估

> 版本 **v1.1** ｜ 初版 2026-09-10 ｜ 二次核对修订 2026-09-10
> 范围：对照《六项解耦_综合实施方案》15 个需求，逐需求列出 server/frontend 需改动的**具体文件、改动类型、风险点**
> 代码基线：`server/app/`、`frontend/src/`、`core/`、`ontology/default/`（2026-09-10 逐文件核对）
>
> **v1.1 修订缘由**：v1.0 写于 core 层施工前，按"全部待实施"列清单。二次核对确认 R1–R8/R11–R14 的
> **core 层已落地大半**，且 v1.0 存在 4 处方案性误判（两套"五间"词汇混淆、声明里不存在 color/tone/sla_days 字段、
> gold tone、密级标尺直接比较）。本版以【现状】标注每条的实际状态，并修正方案。**结论以 v1.1 为准。**

---

## 0. 二次核对总结论（v1.1 新增，先读本节）

### 0.1 已落地（v1.0 清单中已不需要再做的部分）

| 项 | 实际位置（已核对） | 状态 |
|---|---|---|
| R1 RuntimeContext + ReadOnlyStore | `core/runtime_context.py`；测试组 `rtcontext` 已注册（run_tests.py:205） | ✅ 完成 |
| R2 load_pack 缓存 | `core/ontology_loader.py:99` mtime 指纹缓存 | ✅ 完成 |
| R3 FunctionSpec.requires + loader 校验 | 测试组 `fnrequires`（run_tests.py:206） | ✅ 完成 |
| R4 py 函数表名参数化 | `core/functions.py` ctx.table/link 化 | ✅ 完成 |
| R5 core 加载器 | `load_jians` loader:186、`load_cross_levels` loader:218；`functions.py:253 _jian_order`、`:263 _cross_level_name` 已读声明；objects/links/rules 装载期 jian 校验已接线（loader:114-118, 132-134） | ✅ core 完成；**server/frontend 未接** |
| R6 core 状态机 | `load_states` loader:250；`registry.py:51-100` `ClueStatus.from_pack/terminal_states/human_only_states`、`ClueStatusMachine.transitions_for/can_transition/validate` 全部读声明；actions 装载 `allowed_states` 已动态（loader:122-126 调用、:1411-1422 实现；**v1.0 写的 ":1130" 行号已漂移**）；`action_executor.py:133` `requires_role=="human"` 校验已在 | ✅ core 主链完成；**audit/case_library/server/frontend 未接** |
| R7/R12 计分声明读取 | `core/lineage.py:211-223` prioritize_clues 已读 scoring.json + jians 权重 | ✅ 完成 |
| R8 core 派生属性 | `load_derived_properties` loader:362（含 AC5/缓存策略校验）；`core/derived.py:85 register_from_decl` | ✅ core 完成；**端点与前端未建** |
| R13 score_basis 生产 | `core/lineage.py:251-266` 产出 `score_basis{raw,weight,contrib}` + `score_formula` + `score_source="scoring.json@v2"` | ✅ 生产侧完成 |
| R13 server 部分透传 | `clues_view.py:76-77`（basis+formula）、`hypotheses_view.py:101`（仅 basis）、`disposal_board.py:140`（仅 basis） | 🟡 部分完成 |

### 0.2 半拉子工程（函数已建但未接线，最容易误判为"已完成"）

1. **R9 源独立性只写了函数、没有调用方**：`core/functions.py:276 count_independent()`（related_pairs 并查集）
   全库 grep 无任何调用；`jian_cross_level` 升格仍按 `n = len(hits)` **命中间类数**计算（functions.py:346-348），
   未读 `jians.json` 的 `source_independence.related_pairs`（当前声明为空数组），返回体
   （rows/命中间类/交叉等级/规则）**不含** `independent_source_count`。
2. **R13 server 透传不对称**：`score_source` 在线索产物里有，但三个 view 一个都没透；
   `score_formula` 只有 clues 列表/详情透，候补池与看板卡没透。
3. **R13 MCP 未接**：`scripts/mcp_server.py:503-513` tool_clue_list 仍只输出 priority_score。
4. **R13 前端零落地**：全 `frontend/src` grep 不到 score_basis/score_formula；
   `ClueListItem`（clues.ts:24-45）无对应字段；KanbanCard:42 仅原生静态 title；
   ClueListView / ClueDetailView 无任何分数展示。

### 0.3 v1.0 的四处方案性误判（本版已修正）

1. **两套"五间"词汇被混为一谈（影响最大）**
   - **兵法五间**＝因间/内间/反间/死间/生间：来自 `jians.json`，线索字段 `jian_types`，
     消费方是 HeatGrid（后端 heatmap.jians 下发单字"因/内/反/死/生"）与内间权限过滤。
   - **侦查五维（数据通道）**＝资金/通讯/行为/关系/时间：来自 **`dimensions.json`**（不是 jians.json！），
     线索字段 `detail.dimension`，消费方是 JianRadar（DashboardView:78-84 按 dimension 聚合）、
     StatusBadge 的 room 色板、crosslevel.ts 的 KNOWN_ROOMS、KanbanCard 房间标签。
   - v1.0 让 `JIAN_ROOMS`/`JianRadar`/`KNOWN_ROOMS` 改读 `jianConfig` 是错的——会把"因间"贴到资金雷达轴上。
     **正确做法：ontology-config 分别下发 `jians` 与 `dimensions` 两组，各归各的消费方。**

2. **声明里不存在 v1.0 假设的字段**
   - `jians.json` 无 `color`；`cross_levels` 无 `tone`；`dimensions.json` 无 `color`；
     `states.json` 无 `sla_days`；`requires_role` 非 human 时取值是 **`"any"` 不是 `null`**；
     transitions 在文件里是**数组** `[{from,to:[]}]`，loader 转成 dict（loader:281-295）。
   - states 的 tone 实际取值是 `warning/info/muted/success/danger`，**已立案 tone=danger，不存在 "gold"**。
     前端金橙样式来自 STATUS_META（金边 #D4AF37）与 CSS 变量 `--sun-filed-*`，不是 tone。

3. **前端看板列序与后端本来就不一致**（v1.0 未发现）
   - server `disposal_board.py:34` COLUMNS＝待查/查证中/**已排除/已固证/已立案**（＝states.json 数组序）；
   - frontend `board.ts:7` BOARD_COLUMNS＝待查/查证中/**已固证/已立案/已排除**（产品有意把已排除作旁路灰列置末）。
   - 且前端看板**不消费** `/cases/{cid}/disposal/board`：BoardView:38-39 走 `/clues` 列表 + `toBoardCard` 前端自行分列。
     声明化时前端若直接吃 states 数组序，已排除列会从第 3 列跳到第 5 列——必须保留"旁路态置末"的展示排序规则。

4. **密级与角色秩级是两把尺子，不能直接比大小**
   - `ROLE_RANK`（core/access.py:25，前后端同值）：见习0/正兵1/偏将2/主办3/human4/system99；
   - `jians.default_clearance`：内间=3。`_can_see_neijian` 现状判定是 rank≥偏将(2)。
   - v1.0 说"基于 default_clearance 与角色秩级比较"——直接比会得到 偏将2 < 内间3 → 偏将反而看不见内间 的荒谬结果。
     需要一张"角色秩级 → 可见密级"的对照（或独立 ROLE_CLEARANCE 映射），**不是**两个整数直接比较。

### 0.4 仍保持硬编码、确认待改的位置（v1.0 这部分判断准确）

| 文件:行 | 硬编码 | 关联 |
|---|---|---|
| `server/app/hypotheses_view.py:29-31` | `_NEIJIAN` / `_JIANS=["因","内","反","死","生"]`（**单字**，用 `j[0]` 匹配）/ `_LEVELS`（startswith 匹配） | R5/R9 |
| `server/app/clues_view.py:28` | `_NEIJIAN = "内间"` | R5/R9 |
| `server/app/disposal_board.py:34-35` | COLUMNS 五态顺序 | R6 |
| `server/app/worker/dispose.py:30` | `DISPOSE_ACTIONS` 白名单 | R6 |
| `core/audit.py:25` | `_DISPOSAL_STATUS` 五态元组 | R6 |
| `core/case_library.py:23` | `TERMINAL_MAP = {"已固证":"verified","已排除":"excluded"}` | R6 |
| `scripts/mcp_server.py:503-513` | clue_list 输出无 score_basis/formula/source | R13 |
| 前端 `domain/clue.ts:8-68, 89-95, 105-156` | 五态/STATUS_META/动作表/crossLevel/立案门禁 | R5/R6 |
| 前端 `domain/board.ts:7-23` | BOARD_COLUMNS + SLA_DAYS(3/5/7) | R6 |
| 前端 `domain/crosslevel.ts:18` | KNOWN_ROOMS（五维词） | R5（归属 dimensions，非 jians） |
| 前端 `components/common/StatusBadge.vue:15-34` | level--* 等级配色 + room 五维色板 | R5/R6 |
| 前端 `components/research/ClueStatusMachine.vue` | 动作文案/门禁/终态文案 | R6 |
| 前端 `components/board/KanbanCard.vue:35,42-44` | `status==='已立案'`；分数无依据明细 | R6/R13 |
| 前端 `views/DashboardView.vue:177-180` | 四张 MetricCard（待查/查证中/已固证/已立案，**本就无已排除**） | R6 |
| 前端 `components/audit/AuditTimeline.vue:34-36` | `isFiled = status_to === CLUE_STATUS.FILED`（样式走 `--sun-filed-*` CSS 变量，非硬编码色值） | R6 |
| **不存在** | ontology-config 端点（routers/ 下无）、`stores/ontologyConfig.ts`（stores/ 仅有 auth/health/case）、derived 端点 | 共性基建/R8 |

---

## 1. 逐需求调整清单（v1.1：每条含【现状】）

### R1 · RuntimeContext + ReadOnlyStore（py 路径红线）

【现状】✅ 已完成。`core/runtime_context.py` 已建，ReadOnlyStore 经 `__getattr__` 封死 execute/conn。
Server/Frontend 均无改动（server 仅经 worker 间接消费）。

**验证**：`run_tests.py --only rtcontext`

---

### R2 · load_pack 缓存

【现状】✅ 已完成（loader 内 mtime 指纹缓存）。Server/Frontend 无改动。

**验证**：`run_tests.py --only rtcontext`

---

### R3 · FunctionSpec.requires 声明 + loader 校验

【现状】✅ 已完成（`fnrequires` 组）。注意：维护纪律仍有效——server 侧 `etl.py`/`model_designer.py`
调 load_pack 时非法 requires 会装载即硬失败。

**验证**：`run_tests.py --only fnrequires`

---

### R4 · py 函数表名参数化

【现状】✅ 已完成。4 个 py 函数已 ctx.table/link 化，server 不直调。

**验证**：`run_tests.py --only fnrequires e2e`

---

### R5 · jians.json 声明化（五间解耦 A）⭐ Server/Frontend 均有改动

【现状】core 已完成；**server 两个 view 与前端均未接**。

**Server 端改动（剩余）**：

| 文件 | 改动（v1.1 修正） |
|---|---|
| `server/app/hypotheses_view.py:29-31,64-83` | `_JIANS` → `[j["name"] for j in load_jians(pack, base_dir)]`（**双字全名**）；`_LEVELS` → `[lv["name"] for lv in load_cross_levels(pack, base_dir)]`。**v1.0 漏报的匹配问题**：现 heatmap 用 `j[0]` 按单字首字符匹配（`j[0] in _JIANS`），声明名是"因间"双字——改为全名相等匹配，或要求 jians.json 增补 `short_name` 字段并同步扩 loader（推荐前者，产物 jian_types 落的就是全名）；`_level_idx()` 的 startswith 兼容逻辑改为精确匹配声明名（核验产物 `detail.级别` 已落"可立案依据候选"全名） |
| `server/app/clues_view.py:28,59-60,115` | `_NEIJIAN` 不再硬编码：从 jians 声明派生受护间（见下"内间派生"）；`_can_see_neijian()` 改为**角色→可见密级对照**后与 `default_clearance` 比较，**禁止直接拿 ROLE_RANK 整数比 clearance**（见 §4 风险表） |
| `server/app/disposal_board.py:30` | 同款 `_NEIJIAN` 硬编码，随 clues_view 一并改（可上移为共享 helper） |

**内间派生**：jians.json 当前没有"谁是内间"的标记字段，只有内间 `default_clearance=3` 为最高。
两种落法：① 按 `max(default_clearance)` 且来源含 tipoff 派生（零 schema 变更，推荐）；
② jians.json 增加显式标记字段并扩 loader 校验。择一，别两个都做。

**Frontend 端改动（v1.1 修正——区分两套词汇）**：

| 文件 | 改动 |
|---|---|
| `src/domain/clue.ts:89-95` | `CrossLevel` 联合类型放宽为 `string`；`crossLevel()` 的等级名来自配置 **cross_levels**（1/2/3 映射红线保留在前端纯函数里或直接只展示后端值） |
| `src/domain/clue.ts:72-81` `JIAN_ROOMS`/`JIAN_ROOM_VAR` | **改读 dimensions 配置（五维），不是 jians**；雷达/房间色板的轴名是资金/通讯/行为/关系/时间 |
| `src/domain/crosslevel.ts:18` | `KNOWN_ROOMS` 改读 dimensions 配置 |
| `src/components/common/StatusBadge.vue:16-34` | level 配色：cross_levels **无 tone 字段**，按配置数组**序号**映射现有 `level--watch/clue/candidate` 三个 class（样式留前端，不下沉声明）；room 色板：按 dimensions 名称映射现有 `--sun-jian-*` CSS 变量（dimensions 同样无 color 字段） |
| `src/components/research/JianRadar.vue:20,27` | 维度改读 dimensions 配置；**v1.0 漏报**：`angleOf()` 的 `/5` 与 `ring()` 的顶点数必须随维度数 N 参数化（现状是写死五边形），值域 v/3 阈值环文案"1间/2间/3间"随 cross_levels 名称 |
| `src/components/research/HeatGrid.vue` | **无需改**：已完全动态消费后端 `heatmap.jians/levels/counts`（已核对） |

**新增基础设施**：见 §2.1 ontology-config 端点（必须**同时**包含 jians 与 dimensions 两节）。

**风险点（v1.1 修正）**：
- heatmap 单字匹配 → 全名匹配（上见）；产物历史版本若落的是单字，需要兼容窗口或重跑产物。
- 等级名：确保 `jian_cross_level` 返回的 `交叉等级` 与 `cross_levels[].name` 逐字相等（现 functions.py:263-272 已读声明，server view 对齐即可）。

**验证**：`run_tests.py --only jiansdecl declconfig` + 前端雷达/热力/看板渲染回归

---

### R6 · states.json 声明化（领域模型解耦 A）⭐ 剩余改动仍不小

【现状】core 主链已完成（registry 双轨：类常量保留为默认包快捷方式，from_pack/transitions_for/validate 读声明；
actions 装载白名单已动态；action_executor human 校验已在）。**剩余是 2 个 core 文件 + 2 个 server 文件 + 前端 9 处。**

**Server/core 端剩余改动**：

| 文件:行 | 改动 |
|---|---|
| `core/audit.py:25` | `_DISPOSAL_STATUS` 改为按 pack 从 `load_states(pack)` 的 states 名派生（事件识别在无 pack 上下文处需把 pack 透传进来；若透传成本高，最低限度保留默认包常量并加注释声明其为"默认包快照"） |
| `core/case_library.py:23` | `TERMINAL_MAP` 硬编码"已固证→verified/已排除→excluded"。states.json **没有 outcome 字段**——二选一：① states 声明增加可选 `outcome` 字段并扩 loader（推荐，语义最直）；② case_library 保留默认映射、按状态 tone/旁路规则推导（脆弱，不推荐） |
| `server/app/disposal_board.py:34-35` | `COLUMNS` 改从 `load_states(pack, base_dir)` 按声明序生成（assemble_board 已有 pack 参；snapshot_base 即 ontology root，可直接作 base_dir） |
| `server/app/worker/dispose.py:30,45` | `DISPOSE_ACTIONS` 改从 actions 声明读取（pack 内动作全集过滤掉 review_* 实体裁决动作——以 `side_effects` 含 `set_clue_status` 为判据，而不是名字白名单） |

**Frontend 端改动（v1.1 修正）**：

| 文件 | 改动 |
|---|---|
| `src/domain/clue.ts:8-16` | `CLUE_STATUS` 保留为默认常量兼类型（编译期保护不丢）；运行时状态集可被 config 覆盖 |
| `src/domain/clue.ts:28-34` STATUS_META | 建 **tone → 样式令牌** 映射表消费配置：配置给的 tone 实际值域是 `warning/info/muted/success/danger`（**没有 gold**）；已立案的金橙视觉是产品特例，建议样式层规则＝`terminal && requires_role==='human'` 时叠 `--sun-filed-*` 令牌，danger 本身只决定基础色调 |
| `src/domain/clue.ts:40-68` 动作表 | 动作不再前端枚举：由配置的 **actions 节**（name/title/target_status/requires_role/parameters）与 **transitions** 现算——动作可见 ⇔ action.target_status ∈ transitions[current]。**注意现状差异需产品决策**（见下） |
| `src/domain/clue.ts:105-156` 门禁 | `fileGate()` 中 `status !== CONFIRMED` 的硬前置改为 transitions 推导（目标受控终态不在当前态出边里则置灰）；`canFileRole` 保留（角色秩级前端只做按钮渲染，真值后端）；终态判定＝config 中 `terminal===true && requires_role==='human'` |
| `src/domain/board.ts:7-13` | 列序＝**声明序 + 旁路置末规则**（保持现产品行为：已排除沉到已立案之后），不可直接用 states 数组序（见 §0.3-3） |
| `src/domain/board.ts:19-23` SLA_DAYS | **states.json 无 sla_days 字段**，见 §4 决策项 D2：要么扩 states schema，要么看板 SLA 继续走 thresholds（现状前端 3/5/7 与后端 14 天阈值本就是两套，且只有前端这套生效） |
| `src/components/common/StatusBadge.vue` | status 配色走 tone 令牌映射，不按状态名分支 |
| `src/components/research/ClueStatusMachine.vue:114,118-126` | 按钮/终态文案与显隐由 actions+states 配置派生；"立案（已立案）"这种含状态名的文案从配置 label 拼 |
| `src/components/board/KanbanCard.vue:35` | `=== '已立案'` → `isControlledTerminal(status)`（配置派生） |
| `src/views/DashboardView.vue:177-180` | MetricCards 由配置状态循环生成；**现状本就只有 4 张（无已排除），属有意设计**——配置化时用"是否上仪表盘"的过滤规则（如 tone=muted 旁路态不出卡），别简单循环全部状态导致多出一张已排除卡 |
| `src/components/audit/AuditTimeline.vue:34-36` | isFiled → `isControlledTerminal(status_to)`；光晕继续用 `--sun-filed-*` 变量（已经是变量，无需改色值） |

**现状差异（声明化会暴露，必须先决策）**：
- 前端动作表 `待查:[verify,exclude]`（**不能直接固证**），后端 states transitions 允许 `待查→已固证`。
  若按 actions+transitions 现算，待查态会凭空多出"固证"按钮。需产品确认：以前端更严为准（建议在 actions 或配置层增加显式可用约束），还是放开。
- actions.json 的 `title`（标记查证中／回退待查／排除线索／固证／立案）与前端 ACTION_LABEL
  （开始查证／退回待查／排除线索／固证／立案）有 **2 处文案不一致**。直接采用配置会改 UI 文案——
  实施前定一版权威文案（建议改 actions.json title 对齐现 UI，或产品确认新文案）。

**红线保留**：受控终态（terminal + requires_role=human）按钮仍受 canFileRole 渲染门控；
"已立案"human 专属不可因声明化而放开（后端 ActionExecutor 校验不变）。

**验证**：`run_tests.py --only statesdecl disposal disposeapi` + 前端看板/状态机/审计链回归

---

### R7 · scoring.json 声明化（计分权重外置）

【现状】✅ 已完成（lineage.py 读 load_scoring）。前端本项无改动；score_basis 类型支持在 R13。

**验证**：`run_tests.py --only scoringdecl`

---

### R8 · derived_properties.json 声明化（派生属性暴露）

【现状】core 完成（loader + register_from_decl）；**端点、前端均未建**。

**Server 端剩余**：

| 落点 | 说明 |
|---|---|
| 新增端点 | 建议独立 router（`routers/derived.py`）：`GET /cases/{cid}/objects/{obj_type}/{obj_id}/derived/{prop}`；经 AccessContext（五出口纪律），复用 `snapshot_ontology_root` 作 base_dir；**不要**塞进线索详情避免膨胀。注册位置参考 `routers/research.py:47-69` 的 pack/base_dir 取法 |
| 缓存 | cache_policy 已在 loader 校验（never/ttl/until_source_change/materialized）；端点响应带 `cache: hit/miss` 调试字段 |

**Frontend 端剩余**：
- `src/api/endpoints/` 新增 derived.ts（类型含 cache 字段）；
- 在 ProfileView 等对象详情处按需挂载展示（ClueDetailView 不建议直接绑，避免线索详情膨胀）。

**验证**：`run_tests.py --only derived versionanchor`

---

### R9 · 五间解耦 B/C（权限拆分 + 源独立性）

【现状】🟡 **半截**：`count_independent` 已建但无调用方，jian_cross_level 仍按命中间类数升格；
server 权限判定未接 default_clearance。

**Server 端剩余改动**：

| 文件 | 改动 |
|---|---|
| `core/functions.py:329-358` `jian_cross_level` | 升格输入由 `len(hits)` 改为独立源数：收集命中数据源标识 → 读 jians.json `source_independence.related_pairs` → 调本文件已有的 `count_independent(sources, related_pairs)`；返回体增加 `independent_source_count`（等级名仍走 `_cross_level_name`，1/2/3 映射硬红线不动） |
| `core/ontology_loader.py` | related_pairs 装载校验（当前只透传，需校验 a/b 非空、成对、引用的 source_object_types 存在） |
| `server/app/clues_view.py:59-60`、`hypotheses_view.py:116-126` | 见 R5：角色→密级对照后比较，不直接比整数 |

**Frontend 端（v1.1 修正）**：
- `src/domain/crosslevel.ts` 的 `countIndependentJians()` **与 R9 后端函数不是一回事，不能互删互替**：
  它解决的是 FE-T-007 **LLM 同源复读不升格**（按模型桶合并信号），后端 `count_independent` 解决的是
  **结构化数据源同源对合并**（related_pairs 并查集）。
- 等级展示口径以后端为准：凡能拿到后端线索/hypotheses 的地方直接消费后端 level，
  不自行从信号反推等级；前端函数仅保留在"信号录入、尚无后端结果"的本地预估场景。
- v1.0 提议后端在线索 detail 返回 `independent_source_count` 方向正确，字段名与 functions 返回对齐即可。

**验证**：`run_tests.py --only jiansdecl`

---

### R10 · 状态机接线（领域模型接线）

【现状】core 侧随 R6 已通；前端三处（StatusBadge/KanbanCard/AuditTimeline）待改，清单已并入 R6。

**验证**：`run_tests.py --only statesdecl disposal`

---

### R11 · DerivedProperty 运行时集成

【现状】RuntimeContext.derived() 已在 core；端点与前端依赖 R8，未建。

**验证**：`run_tests.py --only derived versionanchor`

---

### R12 · 计分权重读取接线

【现状】✅ 已完成（lineage.py）。前端无。

**验证**：`run_tests.py --only scoringdecl`

---

### R13 · score_basis 可解释输出 ⭐ 剩余：server 补齐 + MCP + 全部前端

【现状】生产侧（lineage）完成；server 透传半成品；MCP/前端未做。

**Server 端剩余**：

| 文件:行 | 改动 |
|---|---|
| `server/app/clues_view.py:76-77` | 已透 basis/formula；**补 `score_source: det.get("score_source")`** |
| `server/app/hypotheses_view.py:95-103` | candidates 补 formula/source（候补卡至少 basis 已有） |
| `server/app/disposal_board.py:134-146` | card 补 formula/source（basis 已透） |
| `scripts/mcp_server.py:503-513` | item 增加 `score_basis`/`score_formula`/`score_source`（optional 新字段，不破坏 69 项 MCP 契约） |

score_basis 实际结构（已核对 lineage.py:251-261，v1.0 类型猜测正确）：
```jsonc
{ "confidence": {"raw":0.9,"weight":0.4,"contrib":0.36},
  "jian_coverage": {"raw":0.6,"weight":0.35,"contrib":0.21},
  "data_strength": {"raw":0.4,"weight":0.25,"contrib":0.10} }
```

**Frontend 端剩余**：

| 文件 | 改动 |
|---|---|
| `src/api/endpoints/clues.ts:24-45` | ClueListItem 增 `score_basis?: Record<string,{raw:number;weight:number;contrib:number}>`、`score_formula?: string`、`score_source?: string`；research.ts 的 HypothesisCandidate 同步加 basis 可选字段 |
| `src/components/board/KanbanCard.vue:42-44` | 分数旁 popover（hover 展开三维 raw/weight/contrib + formula），替掉现在写死的静态 title |
| `src/views/ClueDetailView.vue` | 详情页完整依据面板（三维条形/公式/source） |
| `src/views/ClueListView.vue` | 现状**完全无分数列**（已核对），新增分列并沿用 priority_score 排序 |

**验证**：`run_tests.py --only scoringdecl cluesread` + 改后回归 `python -m scripts.mcp_client_test`

---

### R14 · 后端接线与前端展示

【现状】后端透传大部分随 R13 已做；看板卡/列表展示未做（并入 R13 剩余项）。MCP 见 R13。

**验证**：`run_tests.py --only cluesread`

---

### R15 · 全量回归

**Server 端**：`run_tests.py`（已注册新增组 rtcontext/fnrequires/jiansdecl/statesdecl/scoringdecl，
均在 run_tests.py:205-209 核实；cluesread 在 :181、derived 在 :96）+ `run_all.py --auto-review --no-cli`
+ `mcp_client_test`。
已知存量失败（与本次解耦无关，交接摘要记录）：disposeapi 2、pack 1 error、gateway 1、profiler 1、
auditview/dashboard 常驻失败、spec-AC5 超时——批次验收时需区分新增/存量。

**Frontend 端**：`frontend/tests/` e2e（mvp2/mvp5/mvp5-e2e）回归：看板列序（注意已排除仍在末列）、
状态机门禁（含待查态不应凭空多出固证按钮）、热力图、雷达五维、线索排序、已立案终态、分数 popover。

---

## 2. 跨需求共性基础设施（必须先建）

### 2.1 Ontology Config 端点（Server → Frontend 配置下发）

【现状】未建（routers 24 个文件中无 ontology_config；stores 无 ontologyConfig）。
落点与取数模式已验证可行：`routers/research.py:61-65` 已有
`pack=case.pack_id`、`base_dir=ctx.cases.snapshot_ontology_root(case_id)` 的现成范式。

**方案（v1.1 修正）**：新增 `GET /cases/{cid}/ontology-config`，**jians 与 dimensions 分节**：

```jsonc
{
  "pack": "default",
  "jians": [
    { "name": "因间", "default_clearance": 1, "weight": 3,
      "source_object_types": ["org", "bid_project"] }
  ],
  "cross_levels": [
    { "min_independent_sources": 1, "name": "观察" },
    { "min_independent_sources": 2, "name": "线索" },
    { "min_independent_sources": 3, "name": "可立案依据候选" }
  ],
  "dimensions": [
    { "name": "资金", "note": "银行流水/资金往来异常",
      "source_object_types": ["transaction"] }
  ],
  "states": [
    { "name": "待查", "label": "待查", "tone": "warning",
      "terminal": false, "requires_role": "any", "requires_basis": false }
  ],
  "transitions": { "待查": ["查证中", "已排除", "已固证"] },
  "actions": [
    { "name": "file", "title": "立案", "target_status": "已立案",
      "requires_role": "human", "terminal": true,
      "parameters": [ {"name":"legal_basis","required":true} ] }
  ],
  "scoring": { "dimensions": [], "assumption_confidence": {"_default": 0.7} }
}
```

与 v1.0 示例的差异（按真实声明文件修正）：
- 无 `color` 字段（jians/dimensions 都没有）；cross_levels 无 tone；
- `requires_role` 非 human 一律 `"any"`；
- transitions 服务端加载后是 dict（loader 已把文件内数组转好），端点直接下发 map；
- 增加顶层 `dimensions` 节（前端雷达/色板真正需要的是它）。

**权限**：配置含间类密级。低权限会话的过滤策略＝间类**照常返回名称**（前端要画热力/雷达骨架），
但受护间的命中数据仍走现有线索过滤；不建议按角色删配置节，否则前端维度数随角色跳动。

**前端消费**：新建 `src/stores/ontologyConfig.ts`（pinia，按 caseId 缓存，随 dataVersion 失效）；
`clue.ts`/`board.ts`/`crosslevel.ts` 从 store 读，**无配置时回落现有硬编码默认值**（双轨过渡，避免首屏阻塞）。

**优先级**：P0——R5/R6/R9 的前端改动全部依赖它。

### 2.2 状态配置驱动的前端领域模型重构

【现状】未开始。v1.0 方向正确，补充三条硬约束：
- 样式映射在前端：`tone(warning/info/muted/success/danger) → CSS 变量`集中到 `design/tokens.ts`；
  受控终态金橙＝`terminal && requires_role==='human'` 的叠加规则，不新增 gold tone；
- 列序保留产品语义：声明序为主，旁路态（建议以 tone=muted 为信号）置末；
- 动作表由 actions×transitions 现算，但"待查不可直接固证"等现状收紧项需显式配置承载（见 §4 D1）。

---

## 3. 调整量汇总（v1.1：按"剩余工作量"重算）

| 需求 | core 剩余 | Server 剩余 | Frontend 剩余 | 备注 |
|---|---|---|---|---|
| R1–R4 | 0 | 0 | 0 | ✅ 全完成 |
| R5 五间声明化 | 0 | 3（hypotheses_view/clues_view/disposal_board 共享 helper） | 5（clue.ts/crosslevel.ts/StatusBadge/JianRadar + config store） | HeatGrid 免改 |
| R6 状态声明化 | 2（audit/case_library） | 2（disposal_board/dispose worker） | 9 | 含两处产品决策（D1/D2） |
| R7/R12 计分 | 0 | 0 | 0 | ✅ 完成 |
| R8 派生属性 | 0 | 1 新 router | 2（api + 详情挂载） | core 已完成 |
| R9 五间 B/C | 1（jian_cross_level 接线 + loader 校验） | 随 R5 权限改造 | 1（消费口径修正，函数保留） | count_independent 待接线 |
| R10 状态接线 | 0 | 0 | 并入 R6 | — |
| R11 派生集成 | 0 | 并入 R8 | 并入 R8 | — |
| R13 score_basis | 0 | 4（3 view 补字段 + MCP） | 4（clues.ts/research.ts/KanbanCard/Detail + ListView） | 类型结构已核实 |
| **共性基建** | 0 | 1 新端点 | 1 store + tokens 映射 | P0 先行 |

**Server 端剩余总计**：约 7 个既有文件改动 + 2 个新端点（ontology-config / derived）
**Frontend 端剩余总计**：约 14 个文件（含 store 新建与 clue.ts 双轨重构）

---

## 4. 红线、风险与待决策项（v1.1 重写）

| 编号 | 事项 | 事实/风险 | 处置 |
|---|---|---|---|
| D1 | **待查→固证 前后端不一致** | 后端 transitions 允许；前端动作表不允许 | 实施前产品拍板；若保持前端收紧，需在 actions/配置层有显式表达，不能靠前端硬编码 |
| D2 | **SLA 字段落点** | states.json 无 sla_days；前端分态 3/5/7 天（唯一生效口径），后端 thresholds.json 单阈值 14 天（board 端点无前端消费方） | 二选一：states.json 加 `sla_days`（loader 加非负整数校验，推荐，可随包差异化）或维持 thresholds 单一阈值并让前端改读它 |
| D3 | **密级 vs 秩级两把尺** | ROLE_RANK 偏将=2，内间 default_clearance=3，直接比较出错 | 建角色→可见 clearance 对照（可放 access 模块或服务端 config 节下发），比较前先换算 |
| D4 | **动作文案不一致** | actions.json title 与前端 ACTION_LABEL 2 处不同 | 定一版权威文案再接线 |
| D5 | **已排除列序** | 前端旁路置末 vs states 声明序第三 | 前端保留置末规则（tone=muted 沉底），不直接吃数组序 |
| D6 | **单字/双字间名** | 产物与 view 用单字首字符，声明是双字全名 | 统一全名匹配；历史产物兼容窗口或重跑 |
| — | 前端 ClueStatus 类型放宽 | 联合类型→string 失去编译期检查 | CLUE_STATUS 常量保留为默认类型；运行时校验配置值必须在默认集或显式扩展集内 |
| — | ClueStatus 常量全库引用 | core 大量 `ClueStatus.PENDING` | 现双轨形态已满足：常量作默认包快捷方式，勿删 |
| — | 前后端等级名一致 | cross_levels 名称逐字相等 | functions.py 已读同一声明，server view 改完即可闭环 |
| — | config 端点权限 | 含 default_clearance | 名称全量下发、数据按角色过滤（§2.1） |
| — | 派生端点性能 | 查询时算 | until_source_change 缓存 + cache:hit/miss |
| — | MCP 契约 | 新增 optional 字段 | 旧调用方不受影响；改完必须跑 mcp_client_test |
| — | 存量测试失败 | disposeapi/pack/gateway/profiler 等与本次无关 | 验收按"新增失败=0"判定，不把存量算进批次 |

---

## 5. 实施顺序建议（v1.1 按剩余工作重排）

```
批次 1（P0 地基，纯增量）：
  Server: 新增 ontology-config 端点（jians/dimensions/states/actions/scoring 一节下发）
  Frontend: stores/ontologyConfig.ts + design/tokens.ts 色调映射
            （无配置回落硬编码默认，先不动任何消费组件）

批次 2（P0 声明化接线）：
  Server: R5 hypotheses_view/clues_view/disposal_board 读 jians/cross_levels
          + R6 disposal_board COLUMNS、dispose worker 动作白名单
          + R6 收尾 core/audit.py、core/case_library.py（含 D2 outcome 决策）
          + R9 jian_cross_level 接 count_independent + loader related_pairs 校验
  Frontend: clue.ts/board.ts/crosslevel.ts 双轨读 config
            StatusBadge/JianRadar(/5 参数化)/ClueStatusMachine/KanbanCard/
            DashboardView/AuditTimeline 适配
            （先落 D1/D3/D4/D5 决策再写代码）

批次 3（P1 能力补全）：
  Server: R8 derived 端点 + R13 三个 view 补 score_source/formula + MCP 透传
  Frontend: derived API + clues.ts/research.ts 类型 + KanbanCard popover
            + ClueDetail 依据面板 + ClueListView 分数列

批次 4（P2 收尾）：
  全量 run_tests.py（新增组必绿、存量失败不新增）+ mcp_client_test 69 项
  + 前端 e2e（重点：列序/门禁/雷达维度数/分数展示）
```

**关键里程碑**：批次 2 完成后，兵法五间、侦查五维、状态机在 server/frontend 双侧均无硬编码领域词汇，
且跨包换名不改代码——这是"跨领域可用"的临界点。
