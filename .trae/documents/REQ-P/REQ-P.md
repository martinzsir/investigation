# REQ-P 本体数据画像 + 数据地图 —— 需求清单（v2）

> 来源一：[ONTOLOGY_PROFILER.md](../profile/pr/ONTOLOGY_PROFILER.md)（外部沙盒产物，画像器 42 AC）
> 来源二：[DATA_MAP.md](../map/pr/DATA_MAP.md)（外部沙盒产物，数据地图 L0+L1 28 AC，基于本仓真实 bindings.json 分析）
> 一句话：**数据地图摸清"现在连得通吗"（L0 拓扑 + L1 血缘），本体画像回答"手里有什么、健康吗"（L1–L5）**——
> 前者是后者的结构前置，两者共同服务"画像检出→人工拆分→实体连接→归一落点明确"工作流。
> 核对基线：`ontology_v2 @ e53efad`（REQ-G-022 修复后）。v2 修订：并入 DATA_MAP 核对结论与 10 项新需求。

## 〇、核对结论（本仓库现状）

### 0.1 画像器与数据地图：全仓不存在

`EntityLinkExplorer` / `connectable_props` / `materialized_objects` / `value_profile` / `value_overlap` /
`tests.test_ontology_profiler` / `demo_profiler.py` / `tests.test_data_map` / `core.data_map` ——
全仓仅两份文档自身提及，**无任何实现**。文中"134 / 162 项验收全绿"是沙盒状态，不能视为本仓已完成。

### 0.2 可部分复用的既有设施

- [core/gateway.py](../../../core/gateway.py) OntologyReadGateway（materialization_state / objects / links /
  view / count / StaleOntology 防护）——取数通道现成，缺值画像方法；
- [entity_resolution.py](../../../entity_resolution.py) 人名 normalize / 拼音键 / 编辑距离 / 别名——变体规则轨半成品
  （单位是"人"非"对象.属性"，需适配）；
- [case_knowledge.json](../../../ontology/default/case_knowledge.json) subject_aliases——别名轨现成；
- [thresholds.json](../../../ontology/default/thresholds.json)（REQ-027）——时间窗参数治理落点。

### 0.3 DATA_MAP.md 对本仓代码的声称：逐条核实结果

| # | 声称 | 核实 |
|---|---|---|
| 1 | **transfers 是断链**：build_sql 无 JOIN obj_account，from_account 装 raw 原文 | **属实**（[bindings.json L92](../../../ontology/default/bindings.json#L92)；links.json transfers endpoints 亦无 ref 直出） |
| 2 | tipoff.reporter_raw 未归一（只归一 target_raw） | **属实**（tipoff_targets_person 仅 JOIN target_raw；endpoints 亦未声明） |
| 3 | 6 条边归一为 `raw_name = xxx_raw` 硬编码等值 JOIN | **属实**（calls_to/owns/involved_in/osint_mentions/tipoff_targets_person/co_located） |
| 4 | call/trackpoint 是"隐形枢纽"（links.json 无其端点却物理支撑边） | **属实**（calls_to/co_located 物理上出自 obj_call/obj_trackpoint） |
| 5 | content_raw 无排除标记 | **属实**（objects.json 无 entity_ref 类标记） |
| 6 | 其余边中 time_window 无归一但不算断链（时间窗条件连接） | 与本仓 build_sql 一致 |
| 7 | 沙盒"归一建议 account.raw_name ⊇ transaction.from_raw 正是 transfers 缺的一环" | 推断成立：account 与 transaction 同源（主体/对方），等值 JOIN 可干净补上 |

### 0.4 与沙盒文档的确定性差异（不许照抄结论）

- **间类声明本仓已全覆盖**（G-013）：objects.json 因1/内1/死2/生3 + links.json 反1。沙盒"缺内间/生间/因间、
  因间无对象声明"在本仓**不成立**，L4 须读声明重新实测；
- **无 CaseContext**：时间窗参数治理统一走 REQ-027 thresholds.json，不引入沙盒概念；
- **图库双轨同源红线**：[graph.py L204](../../../core/graph.py#L204) `_flow_source` 语义层 lnk_transfers
  优先、回落 银行流水，两轨现均基于 raw——**transfers 改造必须保留 raw 列语义（新增 id 列做加法）**。

### 0.5 红线继承

画像与地图均只观察不写回；结论恒【待核实】候选；全部模拟数据；取数只走网关消费 obj_*/lnk_*。

## 一、需求清单

状态：【缺失】本仓无实现｜【部分】有可复用半成品｜【差异】与沙盒结论不同，按本仓口径实现。
波次：F（已核实缺陷修复，最高优先，不等画像器）→ M（数据地图）→ P0 前置 → P1 识别 → P2 六层 → P3 治理。

### 第〇波 F —— 已核实缺陷修复（数据层，独立可先行）

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-031 | **transfers 断链修复** | build_sql 补 `LEFT JOIN obj_account`（from/to 各一，raw_name 等值，与 transaction 同源故可干净命中）；**双列方案**：保留 from_account/to_account（raw，保图库双轨同源红线与 overpass 自连接兼容），新增 from_account_id/to_account_id（account_id 外键）；links.json transfers endpoints 同步增补 ref；ladybug golden CSV 刷新基线；graph/overpass/MCP 回归 | 【缺失】 |
| REQ-P-032 | tipoff 举报人归一 | reporter_raw 与 person 归一：新增链接（如 tipoff_from_reporter）或扩展现有链接，objects/links/bindings/policies 四处同步声明；AC：举报人可连到 person 节点 | 【缺失】 |
| REQ-P-033 | 归一映射声明化 | `raw_name = xxx_raw` 等值 JOIN 抽成 bindings.json 声明（如 normalize 段）；发现新别名改配置不改 SQL；与 G-015 endpoints 声明协同（物化归一 ≠ 导出 ref JOIN，两层语义须在文档中划清） | 【缺失】 |
| REQ-P-034 | 内容字段排除声明化 | objects.json 增加实体引用排除标记（如 `entity_ref: false`），content_raw 等长文本字段声明排除；connectable_props 消费该标记（与 REQ-P-001 联动） | 【缺失】 |

### 第一波 M —— 数据地图 L0+L1（沙盒 28 AC 重建）

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-025 | L0 静态拓扑 | 对象资产清单、语义度（links.json 端点出现次数）/物理度（被 build_sql 引用次数）、**隐形枢纽**（语义度 0 却支撑边，call/trackpoint）、孤立对象（clue/decision）；缺陷 1 内置：物理度累计须在 `_parse_link_sql` 之后、`_find_orphans` 之前 | 【缺失】 |
| REQ-P-026 | L1 物理血缘 | 对象←业务表（含 person 的 UNION 六源）、边←物理来源对象、清洗规则清单、归一 JOIN 清单 | 【缺失】 |
| REQ-P-027 | 归一 JOIN 定向与判定 | 定向：**JOIN 的表是 target**、另一侧是 source（缺陷 3 内置，8 条边实测）；等值归一判定：两侧属性名都含 raw（缺陷 2 内置——owns/osint_mentions 两侧 raw_name 不误判业务条件）；业务条件（时间窗等）不算归一 | 【缺失】 |
| REQ-P-028 | 归一缺口判定（看 build_sql） | 判据是"build_sql 是否已归一"，**不是** links.json 有无端点（缺陷 4 内置）；AC 固化"已归一的不是缺口"（call.caller_raw 等不误报）；当前应检出：transaction.from_raw / to_raw / tipoff.reporter_raw 三项（REQ-P-031/032 修复后转零） | 【缺失】 |
| REQ-P-029 | 声明解析鲁棒 | TABLE_ALIAS_RE 把 SQL 关键字 JOIN 抓成别名的**已知行为固化**（AC 防后人误判正则安全，过滤在 _alias_map 层）；bindings 缺失 → **不判定并在 notes 声明未知**（"归一缺口未计算"≠"无缺口"——诚实反映已知）；无 JOIN 不报错；mini 声明独立解析；`obj_\w+` 正则无 CTE/动态表名前提 AC（引入 CTE 时测试失败提醒重估） | 【缺失】 |
| REQ-P-030 | 渲染与红线 | Markdown + Mermaid 输出；只观察不写回；**零依赖**（L0/L1 解析 bindings.json 静态文本，不连 duckdb） | 【缺失】 |

### 第二波 P0 —— 画像前置设施（"第七批"补齐）

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-001 | 可连接属性清单 | `connectable_props()`：从 pack 声明取参与实体连接的 string 属性，元数据属性（status/title/note）与 entity_ref:false 字段（REQ-P-034）排除；来源是 objects.json 声明，不硬编码 | 【缺失】 |
| REQ-P-002 | 物化判定两方法分离 | `materialized_objects()`（对象已物化）与 `materialized_props()`（属性已物化且可连接）拆开；前者不按值类型过滤——decimal/date 也算已物化（画像缺陷 3 内置） | 【缺失】 |
| REQ-P-003 | 网关值画像 | `gateway.value_profile()`：基数/非空数/样例，SQL 侧聚合（COUNT/DISTINCT/LIMIT），原始明细不取回上下文 | 【缺失】 |
| REQ-P-004 | 网关值交集 | `gateway.value_overlap()`：属性间精确交集/包含率（不采样）；**这是揭示 `account.raw_name ⊇ transaction.from_raw` 归一环的工具，transfers 断链（REQ-P-031）正是其价值实证** | 【缺失】 |
| REQ-P-005 | 实体变体双轨 | 规则轨（复用 entity_resolution normalize/相似度）+ 别名轨（subject_aliases）；AC 固化"变体数 0 ≠ 干净"与"无别名表检不出"降级行为；与数据地图归一缺口互证（混装属性即断链温床） | 【部分】 |

### 第三波 P1 —— 值类型识别

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-006 | 值类型识别（否定式优先） | 特异性强排前：手机号 → 身份证 → 日期串 → 金额串 → 账号 → 纯数字；人名/机构名为"肯定式需确认"（画像缺陷 2 内置） | 【缺失】 |
| REQ-P-007 | 金额串正则收紧 | 货币符号/千分位/小数/单位至少其一；AC：`6222000111110001`→账号、`¥1,200,000`/`15万元`→金额串（画像缺陷 1 内置） | 【缺失】 |
| REQ-P-008 | 混装判定与落点建议 | 值类型分布 ≥2 类 → 报混装；同时报两个归一方向，人工二选一；禁止自动拆分/写回；本仓数据分布实施后实测（不照抄沙盒 50/50 结论） | 【缺失】 |
| REQ-P-009 | 元数据属性不识别 | 只对可连接属性识别/评分/报警（依赖 REQ-P-001+034）；防 org.status"存续"误判人名刷屏 | 【缺失】 |

### 第四波 P2 —— 画像六层（L0–L5）

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-010 | 画像 L0 显式 N/A | 物化后无文件，画像产物中 L0 标注"不适用"（数据地图的 L0 拓扑另由 REQ-P-025 承担，两者不混同） | 【缺失】 |
| REQ-P-011 | L1/L2 列值层画像 | 按 对象.属性 输出空值率、值类型分布、混装、落点；未物化对象不报错；与数据地图 L1 血缘互引（地图讲"从哪来"，画像讲"内容如何"） | 【缺失】 |
| REQ-P-012 | 部分物化不崩溃 | 对象表缺列 → 标注"对象已物化，但表中无此列"+ 落健康诊断，禁 BinderException 崩溃（画像缺陷 4 内置） | 【缺失】 |
| REQ-P-013 | L3 万元整数率 | `MOD(CAST(amount AS BIGINT),10000)=0`，仅 decimal | 【缺失】 |
| REQ-P-014 | L3 时间窗覆盖（参数化） | `ABS(date_diff)<=window_days`；窗口从 REQ-027 thresholds.json 读（不引入沙盒 CaseContext），AC 固化可配置；不硬编码 | 【缺失】【差异】 |
| REQ-P-015 | L3 关注主体命中 | string 属性对 focus_entities 命中 | 【缺失】 |
| REQ-P-016 | L3 与已有重合数 | `COUNT(DISTINCT ... IN known_entities)` 精确计数 | 【缺失】 |
| REQ-P-017 | L4 间类层（声明化） | 正向（属哪间）/反向（缺哪间→补什么数据）；映射只读 objects/links.json jian 声明，禁止 DEFAULT_JIAN_MAP；本仓五间已全声明，实施后重新实测覆盖 | 【缺失】【差异】 |
| REQ-P-018 | L5 质量分决策层 | 阻断：混装-25/空值率≥50% -20/0 行-30；告警：未物化-12/基数≤2 -8/含肯定式-5/无万元整数-5/有变体-5；只对可连接属性；分数区间与"结论可推翻"有 AC | 【缺失】 |
| REQ-P-019 | 画像→地图→探测协同 | 工作流固化：**地图检归一缺口（031/032）→ 画像检混装（008）→ 人工拆分/归一 → entity_link 探测 → 落点明确**；防跳步：直接探测会同时报两方向，人工二选一选错即断链（transfers 即实证） | 【缺失】 |

### 第五波 P3 —— 集成与治理

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-020 | 只观察不写回（红线） | 画像与地图全程只读，不写 obj_*/lnk_*/业务表；结论恒【待核实】候选；走 AccessContext（REQ-009/010/011） | 【缺失】 |
| REQ-P-021 | 健康度接线 | 画像/地图过程失败（未物化/缺列/版本锚点缺失）落 core/run_health.py run_diagnostic；画像节可选并入 run_all 报告 | 【缺失】 |
| REQ-P-022 | 数据源纪律 | 取数一律经 OntologyReadGateway 消费 obj_*/lnk_*；地图 L0/L1 为唯一例外（静态解析声明 JSON，不连库）；演示/夹具合成数据，PII 扫描复用 test_golden 扫描器 | 【缺失】 |
| REQ-P-023 | 验收与演练 | tests.test_ontology_profiler + tests.test_data_map 建置并入 run_tests GROUPS；端到端演练脚本入库（demo_profiler 适配）；MCP 暂不新增工具（需要时另立需求） | 【缺失】 |
| REQ-P-024 | 遗留跟进（不阻塞验收） | ① 肯定式识别误判（供应商/存续/设备采购判人名）——领域词典或人工回流；② `_columns_of` 缓存 schema 变更后不失效——补失效机制。记技术债 | 【缺失】 |

### 第六波 G —— 生成器：新表接入画像与声明草案（回答"模型/ETL 怎么建"）

| ID | 需求 | 判据 | 状态 |
|---|---|---|---|
| REQ-P-035 | **新表接入画像 + 两大推荐器**（诊断→生成的延伸） | ① **画像入口与契约**：`scripts/profile_table.py` 经根目录 [data_ingest.py](../../../data_ingest.py) 适配器读任意外部表（CSV/Excel/JSON/SQLite/Parquet），产出 TableProfile（列→空值率/值类型分布/样例/混装/候选关联），对 obj_* 全量可连接属性跑 value_overlap 得候选关联；**边界**：治理工具读原始表是本职（只读），不违反"检测器不准直读 Parquet"红线（该线约束取证路径）<br>② **草案组装推荐器**：TableProfile + 候选关联 → `output/drafts/<table>/` 输出 objects/bindings/links **JSON 草案**（头部 `{"_draft": true, "_status": "待核实", "_evidence": [...]}`；值类型经 value_type→TYPE_SQL 同口径映射；pk/name_property 为高基数标识列**候选**；metadata_props 建议；候选关联超阈值（thresholds.json profiler 段 `draft_overlap_min_ratio`）→ 归一 JOIN 与 lnk 草案）——**绝不写 ontology/**，人工审核后复制进 `ontology/<pack>/` 走 build_ontology/loader 校验三道闸<br>③ **步骤序列推荐器**：`recommend_steps(profile)` 输出**有序** ETL 步骤清单（依赖排序：混装拆分→清洗规则→类型转换→冷层建表（G-014 已声明化）→对象绑定→链接绑定→**画像复检**），每步携 evidence（画像证据）与 done_when（可执行验收，如"复检后混装=0"）；序列退化为干净表三步（建表→绑定→复检）有 AC；**只输出清单不自动执行**<br>④ 共同红线：草案恒【待核实】候选；夹具合成数据 + PII 扫描；不新增 MCP 工具 | 【缺失】 |

## 二、实施注记

1. **先修 F 波再谈画像**：REQ-P-031（transfers 断链）是数据层 bug，不依赖任何画像器代码，且是数据地图
   价值的第一实证——先修它，M 波的"归一缺口应检出三项"验收才能从"检出 3 → 修复后归零"走完整闭环。
2. **AC 基线**：沙盒 42（画像）+ 28（地图）项分组作为本仓 AC 设计起点；"因间无数据源""映射可覆盖"按本仓
   声明现状重写（REQ-P-017）；地图 AC 全组零依赖设计（REQ-P-030）保留。
3. **混装实测待办**：沙盒 from_raw 账号/人名 50/50 系其数据分布；本仓 golden/demo 分布不同，REQ-P-008
   实施后实测为准。
4. **波次依赖**：F（031→032→033→034 可独立先行，034 联动 001）→ M（025~030 顺序：025/026 → 027/028 → 029/030）
   → P0（001~005）→ P1（006~009）→ P2（010~019）→ P3（020~024）→ **G（035，依赖 P0+P1 的算法层与
   034 标记，M4 后启动）**。031 的 endpoints 改动须与 G-015
   的 _edge_sql / golden 比对流程回归联动。
5. **四问能力对照**（035 加入后）：① 表/列情况——能；② 候选关联——能（半自动+人工确认）；
   ③ ontology 模型推荐——**能**（035② 草案组装器，候选+三道闸）；④ ETL 步骤推荐——**能**
   （035③ 序列推荐器，只输出清单不自动执行）。
