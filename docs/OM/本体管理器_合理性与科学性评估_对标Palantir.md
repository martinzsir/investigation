# 本体管理器（对象管理器）合理性与科学性评估

> 对标基准：Palantir Foundry Ontology
> 评估对象：`OntologyManagerView.vue` + `model_designer.py` + `ontology_generic.py` + 八段声明体系
> 评估方式：静态代码走查 + PRD 与实现一致性比对（未运行系统）
> 日期：2026-09-15

---

## 零、结论摘要

**总体判断：架构理念正确且领先，治理能力扎实，但"治理"装错了门，"管理"管错了对象。**

| 维度 | 评分 | 说明 |
|---|---|---|
| 分层合理性 | ★★★★★ | 类型层/管道层/规则层分离，比多数自研本体系统彻底 |
| 声明-实现分离 | ★★★★★ | 声明是数据、实现是代码，切换案件包不改代码 |
| 变更治理 | ★★★☆☆ | 提案/影响面机制科学，但只覆盖 7 个低风险文件 |
| 版本与举证 | ★★★★★ | 只追加不覆盖 + 完整快照引用，取证导向明确 |
| 权限与遮蔽 | ★★★★★ | fail-closed + 对象级/属性级双层 |
| 管理界面组织 | ★★☆☆☆ | 管的是 19 个 JSON 文件，不是"对象" |
| 文档一致性 | ★★★☆☆ | 存在已确认的文档漂移 |

**一句话**：底层本体（Ontology）做得像 Palantir，上面的管理器（Manager）还停留在"配置文件浏览器"。

---

## 一、站得住的部分（对标 Palantir 逐项）

### 1.1 类型层与管道层彻底分离 —— 这是最正确的一处

`objects.json` 只声明"对象是什么"（pk / kind / name_property / 带值类型的属性），
**完全不含数据来源**；数据从哪来、怎么清洗、怎么建链接全在 `bindings.json`。

这正是 Palantir "Ontology 是决策层抽象，不是数据模型"的核心主张。
多数自研系统会把这两者混在一张表里，导致换数据源就要改对象定义。
本项目换数据源只需加 binding，检测器代码不动。

> 证据：`ontology/default/objects.json`（无任何 source 字段）、`ontology/default/bindings.json`

### 1.2 Action 唯一写路径 + Function 强制只读

`functions.json` 声明只读计算并强制 SQL 白名单 SELECT/WITH + `{{param}}` 模板参数（string 走 enum 白名单防注入）；
`actions.json` 声明唯一写路径，由 `ActionExecutor` 执行角色/参数/状态机校验。

对应 Palantir 的 Functions vs Actions 二分，且落到了**代码级强制**而非架构约定——比 Palantir 更硬。

### 1.3 三层继承 + 固定合并优先级

`_shared`（全域）→ `_industry/{金融,医疗}` → 案件快照，合并优先级**固定不可配置**（案件 > 行业 > 全域）。

"不可配置"这条是刻意的：若可配置，同一份数据在不同案件会解析出不同结果，直接破坏取证结论的可复现性。
这是对场景的清醒认知，Palantir 在通用场景反而不需要这么硬。

### 1.4 版本沿革：只追加 + 完整快照引用

`commit_ontology_version()` 重算指纹 → 归档完整快照 → 回写 `case_pack_snapshots` → 追加 `pack_snapshot_history`。
指纹未变不产生历史行。

关键设计：`reason` 必填 + 存完整快照引用，而非只存元信息。
PRD 原话："只存元数据等于'知道改过但拿不出改前是什么'，回溯性存疑。"
这是取证系统的正确取舍。

### 1.5 版本收口做了单点漏斗（容易被做错，这里做对了）

`record_config_audit()` 内部统一调 `commit_ontology_write()`，
所有配置写路由只需调它一个，不会漏掉版本收口。

实测覆盖率：`access_config` / `etl` / `governance` / `knowledge` / `model_designer` / `ontology_generic` / `rule_workshop` / `views` **全部接入**。

> 证据：`server/app/snapshot_config.py:170-188`（docstring 明写"所有配置写路由的统一漏斗"）

### 1.6 影响面分析的三条红线（科学性最强的一处）

`core/impact.py`：
- **R1 宁可多报不可漏报**：能确定的记 `certain`，动态 SQL 里只能词法命中的一律记 `uncertain` 并保留，绝不静默丢弃
- **R2 已生成线索单独成类**，且据 `state.sqlite` 标出「已固证/已立案」
- **D10 计算失败 ≠ 无影响**：每类独立 try，失败记 `failures` 并置 `unavailable`，全失败时 `status=failed`，禁止显示"无影响"

"失败不等于无影响"这条尤其关键——绝大多数影响面实现在这里会静默返回空集，属于安全方向的错误。

### 1.7 权限：fail-closed + 双层遮蔽

未声明策略的对象/链接一律拒绝；`policies.json` 支持对象级与属性级敏感列遮蔽。
`views.json`（REQ-046）按角色投影列子集，物化为 `v_*` VIEW，不复制数据、不复制权限事实（策略仍在读时执行）。

属性级遮蔽 + 角色视图分离，与 Palantir 的 Markings + Object Views 同构。

### 1.8 工程成熟度细节

- 局部降级：单文件缺失/解析失败只降级该项，不阻断整体（`ontology_overview.py` E1-3/E1-4）
- 逃生舱：挂载失败给"在 JSON 页打开"入口
- **修正假承诺**：7 个页面曾对 `dangerous=true` 一律显示"保存后自动触发 RESCAN"，实际只有 1 个真入队，PRD 明确禁止此类文案（S0 R3）
- 写前全量校验：`_validate_in_temp()` 在临时副本跑 `load_pack`，不合法不落盘

---

## 二、问题清单

### P0-1　objects / links 保存绕过影响面与提案（最严重）

**现象**：`ModelDesignerView` 保存走 `PUT /cases/{cid}/objects` → `_validate_in_temp` → 直接 `atomic_write_json` 落盘。
**没有影响面预览，没有提案，没有评审。**

**为什么严重**：
- 影响面引擎**本来支持**——`core/impact.py:57` 的 `ANALYZABLE_FILES` 明确包含 `objects` / `links`
- 但前端只有 `GenericConfigView.vue` 调用了 `ontologyGeneric` 的 impact/proposal API
- 也就是说：能力建成了，装在了低风险的 7 个通用文件上，**最危险的结构变更入口反而裸奔**

**后果**：改 `pk`、删属性、删对象类型会直接让 `obj_*` 物化表结构失效、已生成线索悬空，
而系统不会给出任何"将影响 N 张表 / N 条已固证线索"的前置警示。
这与 S5 R1「宁可多报不可漏报」的设计意图直接冲突。

**修复方向**：在 `model_designer.py` 的 save 前（或 ModelDesignerView 保存前）加一次
`POST /cases/{cid}/ontology/impact`，把结果以强制确认弹窗呈现；
`change_level == "structural"` 时要求填写变更理由并走提案。
`_diff_objects_level()` 已经算出了 structural/display 分级，接线成本不高。

---

### P0-2　views.json 前端保存链路断裂

**现象**：
- 后端有写路由：`server/app/routers/views.py:54` `PUT /cases/{cid}/views`
- API 层有方法：`frontend/src/api/endpoints/policies.ts:83` `saveViews()`
- 本体管理器标 `writable = true`
- **但没有任何组件调用 `saveViews`**

本体管理器把 `views` 挂到 `PolicyMaskingView`（`EDITOR_LOADERS.views`），
而该页面的 views tab 是**只读**表格，文案还写着"在 views.json 编辑器维护"——那个编辑器并不存在。

**后果**：用户看到「✅ 可写」却无处可写；Object Views 实际只能通过导包改 JSON 维护。
对照 Palantir，Object Views 是一等公民，这里退化成了死配置。

**修复方向**（二选一）：
1. 在 PolicyMaskingView 的 views tab 增加编辑态，接 `saveViews`（改动小）
2. 新建独立 `ViewsView.vue` 并在 `EDITOR_LOADERS` 改正挂载

---

### P1-3　管理器管的是"文件"，不是"对象"（根本定位问题）

**现象**：左栏按 19 个 JSON 文件组织（对象模型 / 数据 / 研判 / 治理 / 知识 / 计分六组）。
点开 `objects.json` 看到的是全部对象类型的数组。

**为什么这是根本问题**：
Palantir Ontology Manager 是**以 Object Type 为中心**的——点开 `person`，
看到的是它的属性、它参与的链接、数据从哪来、谁可见、哪些属性被遮蔽、有哪些角色视图、被哪些规则引用。

本项目中，一个 `person` 的信息被切散在：
`objects.json`（属性）+ `links.json`（关系）+ `bindings.json`（数据源）+ `policies.json`（权限）+ `views.json`（角色视图）+ `rules.json`（引用方）。
**没有任何一个界面能横向看全。**

语义六组解决了"文件在哪"的痛点（PRD P1/P2），但没解决"某个对象长什么样"。
这是从"文件管理器"走向"对象管理器"必须跨过的一步。

**修复方向**：在左栏"对象模型"组下，把 objects/links 从"文件"展开为**对象类型列表**
（`person` / `org` / `transaction` …），点击单个对象类型进入"对象 360"面板，
聚合展示该对象的六类切面，并给每个切面对应文件的跳转。
这可复用现有 6 个编辑器的读接口，不需要重写写逻辑。

---

### P1-4　文档漂移（已确认 2 处）

| 位置 | 文档写的 | 实际 |
|---|---|---|
| `AGENTS.md` | 值类型仅 `string/integer/decimal/date/boolean` | `core/ontology.py:54` TYPE_SQL 已有 9 种，含 `timestamp`/`duration_days`/`enum`/`json`（REQ-041 扩展） |
| `S1 PRD §12 UC-S1-2` | 8 个可写、11 个只读 | 实际 17 可写、2 只读（`functions` / `llm_policy`） |

值类型这条尤其要注意：`AGENTS.md` 是给 AI/新人读的第一手约束，
写少 4 种类型会让人误以为 `objects.json` 里 `decision.decision_type: "enum"` 是非法声明。

---

### P2-5　`writable` 用静态映射表

`ontology_overview.py:48` 的 `_WRITABLE_FILES` 是硬编码 frozenset。
它已经漂移过一次（PRD 时期 8 个 → 现在 17 个）。新增写路由时若忘记同步，
界面会显示错误状态——而这种错误是"静默"的，不会报错。

**修复方向**：从 FastAPI `app.routes` 反射生成（启动时扫一遍 `PUT/POST` 路径映射到本体文件名），
或退一步加一条启动自检断言：静态表里的每个名字必须能找到对应写路由。

---

## 三、与 Palantir 的差异：哪些是"故意不学"，哪些是真差距

| 差异 | 判断 |
|---|---|
| 无 Ontology Branch（多分支并行编辑） | **故意不学**，正确。单案件线性演进足够，分支会破坏版本链的简单性 |
| 无本体级搜索/血缘图谱 UI | 差距但优先级低。`core/impact.py` 已算出引用关系，缺的是可视化 |
| 无对象实例浏览器（Object Explorer） | 差距。本项目有 `core/object_set.py` 查询构造器，但无对象实例的浏览界面 |
| 影响面不覆盖动态 SQL 未解析引用 | **已正确处理**：不是漏，是显式标 `uncertain` 并告警 |
| 提案不走多人会签 | 可接受。内部系统，`require_analyst` + 作者/管理员双控已够 |
| 编辑标准域需 `is_ontology_admin` + rank 双条件 | **比 Palantir 更严**，且已真实接入 `etl.py:258/279`（标准域数据元） |

---

## 四、建议优先级

| 优先级 | 事项 | 预计成本 |
|---|---|---|
| P0 | objects/links 保存前强制影响面确认，structural 级走提案 | 小（`_diff_objects_level` 已有，缺接线） |
| P0 | 修复 views.json 前端编辑入口 | 小（后端与 API 均已就绪） |
| P1 | 左栏从"文件"升级为"对象类型"视角 + 对象 360 面板 | 中 |
| P1 | 修正 `AGENTS.md` 值类型清单与 S1 PRD 可写计数 | 极小 |
| P2 | `writable` 静态表改为路由反射或启动自检 | 小 |

---

## 五、证据索引

| 结论 | 文件 |
|---|---|
| 类型层/管道层分离 | `ontology/default/objects.json`、`bindings.json` |
| 影响面红线 | `core/impact.py:1-58` |
| 提案状态机与人工发布 | `server/app/routers/ontology_generic.py:255-410` |
| 版本收口单点漏斗 | `server/app/snapshot_config.py:127-188` |
| objects/links 直落盘 | `server/app/routers/model_designer.py:169-197` |
| views 保存死代码 | `frontend/src/api/endpoints/policies.ts:83`、`views/PolicyMaskingView.vue:424-438` |
| 文件树挂载表 | `frontend/src/views/OntologyManagerView.vue:136-161` |
| 语义六组与可写表 | `server/app/routers/ontology_overview.py:36-55` |
| 值类型实际集合 | `core/ontology.py:54-65` |
| 标准域双条件门禁 | `server/app/snapshot_config.py:47-60`、`routers/etl.py:258,279` |
| PRD 红线 | `docs/OM/ext/S0_结构地基_PRD.md:245-260`、`S1_本体管理器壳与总览_PRD.md:226-234` |
