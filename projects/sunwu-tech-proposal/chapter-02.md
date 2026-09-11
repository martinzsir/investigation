# 第 2 章　总体架构设计

本章从整体视角描述孙武侦查官（SunWu）确定性侦查推演内核的骨架：系统由"五层存储（L0–L4）+ 四层逻辑"两个正交维度构成，前者解决多源异构调查数据的物理落盘与生命周期，后者解决声明式语义层之上的统一读、计算与受控写。第 1 章已定义语义层、对象、链接、间类、维度、庙算、线索、代理键、降级、健康度等术语，本章直接引用，不再重新解释。需要提前说明的一点是，整个内核 66 个 Python 模块（core/ 根目录 57 个 + core/llm/ 9 个）被刻意划分为九大职责分组，全部围绕"声明是数据、实现是代码"这一核心命题展开，且对外的三条产品形态（CLI / MCP / Web）在依赖方向上严格单向：它们依赖内核，内核不反向依赖它们。

## 2.1 架构总览：五层存储 + 四层逻辑

孙武侦查官的架构可以用两个相互正交的维度来理解。第一个维度是**存储分层**（L0–L4），它回答"数据以什么形态、存在哪里、活多久"；第二个维度是**逻辑分层**（消费层 → 统一读网关 → 语义层 → 计算与写层），它回答"一次推理请求如何穿过系统、在哪里被管控、在哪里落地"。这两个维度正交的意义在于：无论底层是 Parquet 原始文件还是 DuckDB 温层，上层逻辑看到的始终是统一的语义层接口，存储载体的替换不应波及规则与权限。

存储侧的五层并非简单的冷热分级，而是带有明确的"数据形态跃迁"语义：L0 是原始文件（Parquet / CSV / Excel / JSON / SQLite），L1 是进程内特征缓存（`feat_l1` 表，特征集哈希驱动失效），L2 是 DuckDB 单文件温层（`investigation.duckdb`，承载中文业务冷表、`obj_*`/`lnk_*` 语义层、诊断表与审计链），L3 是不可变分区冷层（`data/*.parquet`），L4 是可选的图库（LadybugDB，`data/ladybug/*.lbug`）。逻辑侧的四层则保证：所有读请求都被 `OntologyReadGateway`（REQ-002）这一唯一入口收口，所有写请求都被 `ActionExecutor` 这一唯一入口收口，而 `AccessContext`（frozen、operator 必填）作为五条出口（网关 / Function / Action / MCP / 导出）的统一鉴权载体贯穿始终。这种"双层收口"是本系统设计稳健性的根本来源，后续 2.2 与 2.3 将分别展开。

需要强调，五层存储与四层逻辑并非各自为政，而是通过主数据流（`run_all.py:47 main()`）被串联成一条可回放的确定性链路：数据接入 → 实体解析 → 采样预演 → 语义层编译（`build_ontology`）→ 质量门扫描 → 规则执行 → 庙算 → 血缘去重 → 图计算 → 处置看板 → 产物导出。任何一个环节的健康度异常都落在 `run_diagnostic`，与线索 findings 物理隔离，这一点在 2.5 的 ADR-5 中有专门论证。

## 2.2 存储层设计（L0–L4）

存储层的核心职责是把"不可控的多源异构原始数据"逐步收敛为"可控、可审计、可回放的语义层与诊断数据"。每一层都有明确的载体、职责与数据生命周期，且层与层之间的流转存在严格条件，并非任意可达。理解这一层要先理解一个前提：本系统是纯离线、零 LLM 依赖的确定性内核，可部署于物理隔离网络，因此存储选型首先服从"单文件、嵌入式、零外部服务依赖"的硬约束，而非传统的分布式数仓思路。

L0 原始文件层是全部数据的起点，`core/data_ingest.py` 提供 Parquet / CSV / Excel / JSON / SQLite 五类适配器（`DataIngestManager.ingest_directory`），数据在此以原生格式落盘，不做任何语义改造。L1 是进程内字典加 `feat_l1` 表的轻量特征缓存（REQ-015），其键 `feature_set_hash = sha1(entity_id|time_window|feature_name|inputs)`，输入一旦变化即触发 `mark_stale`，保证特征重算的正确性而不必每次回源。L2 是真正的温层与系统重心：DuckDB 单文件 `investigation.duckdb`，既存放未做语义抽象的中文业务冷表，也存放编译产物 `obj_*`/`lnk_*`、诊断表与审计链，是绝大多数查询与规则执行的物理底座。L3 冷层为 `data/*.parquet` 不可变分区，用于快照与归档；L4 为可选的 LadybugDB 图库，未安装时全线降级为 SQL 单轨并标记 `degraded`。

为什么选 DuckDB 而非传统数据仓库？根本原因在于部署形态与确定性诉求。传统数仓（如 PostgreSQL 集群、ClickHouse、Snowflake）要求常驻服务、网络可达与运维团队，这与"可部署于物理隔离网络、零 API Key"的边界直接冲突；而 DuckDB 是进程内嵌入式分析引擎，单文件数据库随内核一起分发，无需独立服务进程，离线环境天然可用。对于本系统以读为主、批量为辅、数据规模受"案件包"边界约束（如 `default` 包 11 对象 / 10 链接、`reqd_case` 包 7 对象 / 2 链接）的负载画像而言，DuckDB 的列存向量化执行与 SQL 表达能力已足够覆盖 R1–R6 规则与 11 个 Function 的计算需求，引入分布式数仓带来的运维复杂度与确定性回放成本远高于其收益。

为什么图库是可选而非必需？这是本系统一个重要的能力边界选择。图算法在本实现中实测仅支持两跳定长 `MATCH (a)-[e1]->(m)-[e2]->(b)`、变长跳 `MATCH (a)-[:*1..N]->(b)` 与 degree 统计（见 `server/app/graph_view.py`），而社区发现（Louvain / LPA）、PageRank、各类中心性、最短路径、k-core 均**未实现**。同时 LadybugDB 官方 CI 不构建 Windows 扩展，导致 Windows 原生不可用，须走 CSV 中转且 `ATTACH DuckDB` 在 Windows 下失效；在 WSL / Linux 下也需手工放置 `libduckdb.so`。鉴于图能力既薄又受平台限制，系统设计了 Cypher / SQL 双轨互校（`compare_engines` 按 `(source, bridge, dest)` 规范化键比对，金额不参与以防浮点差，不一致只标 `only_in_cypher / only_in_sql` 而不自动采信任一侧），确保"无图库也能跑、有图库只是增强"。将其设为可选增强而非核心依赖，使系统在任何环境下都能以一个确定的 SQL 单轨交付结果，避免图库可用性成为推理正确性的单点风险。

五层之间的数据流转并非任意可达，而是受明确的生命周期条件约束。L0 原始文件经适配器进入后，特征缓存（L1）按需从 L2 源表计算并写入 `feat_l1`，一旦输入变化即由 `mark_stale` 使旧哈希失效，避免脏缓存被复用。L2 语义层的重建由增量规划驱动，`core/rebuild_planner.py` 的 `RebuildPlan` 以 `DEFAULT_BATCH_THRESHOLD=5000` 为批处理阈值，经 `materialize_changed` 做行级 diff 后推进版本时钟；重建前 `snapshot_source_rows` 把溯源快照写入 `row_archive` / `row_build_index`，保证事后可回捞。L2 到 L3 的流转是分区不可变归档，用于审计与历史比对，写入后不再变更。L4 图库则由 `core/graph.py` 的 `build_from_duckdb` 从语义层派生，节点必须全部导入否则边 COPY 报错，且 Windows 下因路径反斜杠被 Cypher 解析器当转义，导出路径须用 `as_posix()`。这套流转条件的核心意图是：越靠近 L0，数据越原始越不可控；越靠近 L2，数据越语义化越可审计；L3/L4 是 L2 的派生快照，永远不反向改写 L2。

## 2.3 逻辑层设计

逻辑层的职责是把底层的存储能力包装成"受控、可理解、可追责"的推理服务接口。它自外向内分为四层：消费层、统一读网关（OntologyReadGateway）、语义层（`obj_*`/`lnk_*`）、计算与写层。四层之间方向严格单向，外层依赖内层，内层不感知外层存在，这保证了核心推理逻辑与具体交付形态解耦——这是 2.6 讨论三条产品形态依赖方向的基础。

消费层是系统对外的所有触达点，包括 CLI 脚本、MCP Server、Web（FastAPI + Vue 3）、LLM Agent 与导出工具。它们共享同一套内核能力，但各自承担不同的信任边界与交互语义。统一读网关 `OntologyReadGateway`（`core/gateway.py`，REQ-002）是语义层的**唯一读入口**，任何消费方要读取 `obj_*`/`lnk_*` 都必须经它，由它施加对象级 / 链接级 / 属性级三级鉴权（见 `core/policy.py` 的 `check_object / check_link / mask_value`）并强制 `AccessContext`。这一层把"谁能看什么"从散布在业务代码中的判断收敛到一处，是 fail-closed 策略（未声明即拒绝）的物理落点。

语义层是逻辑层的圆心，由 `obj_*` 对象表与 `lnk_*` 链接表构成，是规则、Function、庙算与写路径共同操作的数据平面。它之上就是计算与写层：规则引擎（`core/rules.py` 的 `run_rules`）、Function 执行器（`core/functions.py`）、庙算假设引擎（`core/hypotheses.py` 的 `MiaoSuan`）、以及唯一的写路径 `ActionExecutor`。计算层只读语义层并产出 findings 与线索，写层则经 ActionExecutor 把人工处置结果落回 `obj_decision` / `lnk_decision_for` 等运行期表。值得注意，`OntologyReadGateway` 与 `ActionExecutor` 一进一出两个唯一入口，共同构成了"读收敛、写收敛"的双向治理，使任何一次数据的流动都可被审计哈希链（`core/audit.py`）追溯到具名 operator。

四层逻辑并非松散堆叠，而是被 `AccessContext`（frozen dataclass，`operator` 必填非空、`role` 默认"正兵"、`network` 限 local / isolated / web）这一统一载体缝合成一个受控整体。参考基线将其称为"五条出口统一管控"：网关、Function、Action、MCP、导出五处出口在放行前都必须校验同一个 `AccessContext`，且 `can_llm_call()` 在 `network="isolated"` 时返回 False，使离线隔离环境天然屏蔽 LLM 调用。这种缝合的意义在于，权限、人机边界与审计不是各层各自实现的副本，而是同一个 frozen 上下文在不同出口被反复校验的结果，从结构上杜绝了"某个出口忘记鉴权"的实现漂移。计算与写层内部也遵循只读契约：Function 经 `functions.py` 的 `_assert_readonly` 与首词 `SELECT` / `WITH` 校验，规则经 `rules.py` 的 `_evaluate_single` 只读语义层，任何写意图都会被写路径唯一入口拦截。于是四层逻辑在"统一上下文 + 读收敛 + 写收敛"三点约束下，成为一层可被独立验证的确定性管线。

## 2.4 内核模块地图（core/ 66 个模块的九大分组）

core/ 目录下的 66 个 Python 模块（根 57 + core/llm/ 9）按其职责被划分为九大分组，每组对应架构的一个切面。这一分组不是文档上的随意归类，而是反映了模块之间真实的依赖方向与契约边界：底层存储与连接在最内层，语义层在其上，权限面横切所有读路径，规则与函数、Action 与写路径、事件审计诊断纵向贯穿，线索假设实体承载业务语义，检测器作为独立同签名插件存在，治理与 LLM 作为可选增强挂在最外层。

A 组（存储与连接）负责一切物理 IO：`store.py` 是 L1/L2/L3 三层门面（`Store(root="data", db_path="investigation.duckdb")`），其 `touches_forbidden_table()` 直接落地第一条禁令的直查拦截；`features.py` 的 `FeatureStore` 实现 L1 特征缓存；`graph.py` 的 `GraphBackend` 封装 L4 图库与双轨互校。B 组（语义层）是本系统的计算重心，`ontology.py` 以 1380 行成为全仓最大模块，承载编译器与类型模型（`build_ontology` 见 `core/ontology.py:393`，代理键分配见 `core/ontology.py:978`），同组的 `ontology_loader.py`、`ontology_version.py`、`ontology_profile.py`、`views.py`、`derived.py`、`pack.py`、`data_map.py`、`draft_assembler.py`、`rebuild_planner.py` 分别覆盖装载、版本时钟、画像、视图、派生属性、案件包隔离、数据地图、草案生成与增量规划。

C 组（权限面）横切所有读路径：`access.py` 定义 `AccessContext` 与角色双尺（`ROLE_RANK` / `ROLE_CLEARANCE`），`policy.py` 的 `PolicyEngine` 实施三级鉴权，`gateway.py` 是统一读网关，`runtime_context.py` 用 `ReadOnlyStore` 屏蔽写能力。D 组（规则与函数）包含 `rules.py`、`functions.py`、`rule_dsl.py`（AST-only 不拼 SQL，`MAX_DEPTH=5`）、`threshold.py`、`clean_ops.py`（唯一清洗 op 注册表，15 个已注册 op）、`value_type.py`、`data_elements.py`、`de_recommend.py`。E 组（Action 与写路径）是写收敛的承载：`action_executor.py` 唯一写入口，`registry.py` 维护状态机与注册表，`disposal.py` 的 `DisposalBoard` 实现看板，`outbox.py` / `writeback.py` / `reconcile.py` 负责发件箱、回写与对账死信。

F 组（事件 / 审计 / 诊断）提供可观测性与留痕：`event_bus.py`（17 类事件、环检测深度 > 10）、`audit.py`（SHA-256 哈希链）、`run_health.py`（约 20 类诊断统一落 `run_diagnostic`）、`row_uri.py`（内容寻址溯源 `dataset@version#partition/rowid`）、`anomaly_channel.py`（异常线索通道，绝不参与五间交叉升格）、`metrics.py`（规则度量与推翻率告警）。G 组（线索 / 假设 / 实体）是业务语义核心：`hypotheses.py` 的 `MiaoSuan` 庙算引擎、`lineage.py` 血缘去重、`entity.py` 组织解析、`review.py` / `review_loop.py` 人审闭环、`deferred.py` 回捞、`case_library.py` 案例库、`object_set.py` 查询构造器、`sampling.py` 采样预演、`validate.py` 六段校验、`geo.py` 地点标准化、`ingest_validate.py` 分区校验、`search.py` 语义检索。H 组（检测器）是四个同签名 `scan(gateway, ...)` 的插件：`compliance.py`、`sensitive_scan.py`、`unit_scan.py`、`data_freshness.py`，只告警不阻断。I 组（治理 / 提案 / LLM）包含 `parameters.py`、`proposal.py` 与 core/llm/ 下 9 个模块（`redact.py`、`guard.py`、`llm_client.py`、`draft_rule.py`、`explain.py`、`align.py`、`plan.py`、`fallback.py` 等），LLM 整体为可选增强，有一键降级与影子模式。

这九大分组本身也揭示了模块间的依赖方向：A 组（存储）被 B/C 组依赖，B 组（语义层）被 D/E/F/G 依赖，H 组（检测器）以同签名插件方式挂在 B 组之上，I 组（LLM）位于最外层的可选环。值得注意的是，分组里没有任何一组依赖 2.6 将讨论的 CLI / MCP / Web 形态——这印证了"内核零外部依赖、形态可插拔"的边界：形态的演进（例如未来新增一个桌面端）不需要改动这九组中的任何核心模块，只要新形态同样收敛到 `OntologyReadGateway` 与 `ActionExecutor` 两个入口即可。这种分组上的自洽，是系统能在离线、零 LLM 环境下独立成立的结构性保证，也是后续各章逐个展开每个分组内部契约时的统一前提。

## 2.5 关键架构决策记录（ADR）

本节以架构决策记录（Architecture Decision Record）形式固化本系统最具争议性的六项决策。每条均按"背景 → 决策 → 理由 → 代价 → 被否决的替代方案"五段式陈述，目的是为后来的维护者与审计者提供可追溯的取舍依据，而不是把当前设计包装成唯一正确解。

### 2.5.1 ADR-1 引入语义层而非直查业务表

**背景**：调查数据天然多源异构（银行流水、通话记录、招投标档案、工商信息、轨迹出行、公开 OSINT、举报材料），若规则与权限直接面向这些业务冷表编写，则会陷入 N×M 的对齐困境——每新增一个数据源或一条规则，适配成本随两者数量乘积增长，且无法在不改动代码的前提下实现跨源统一权限。

**决策**：所有推理逻辑只操作编译后的语义层（`obj_*` 对象表 / `lnk_*` 链接表），业务冷表仅作为语义层的输入，禁止规则与 Function 直查。第一条禁令在 `core/store.py` 以 `_FORBIDDEN_TABLES` 维护受保护表清单，命中即抛 `DirectSourceAccessError`；`scripts/audit_straight_sql.py` 静态扫描，CI 的 `audit` 测试组强制通过。仅 `unsafe=True` 旁路可绕过，但必须带 `operator + reason` 并落 `meta_unsafe_query`。

**理由**：语义层把"数据是什么"与"数据从哪来"解耦，规则以稳定的对象 / 链接命名空间书写，新增数据源只需补充绑定声明而不动规则；权限可基于对象级 / 链接级 / 属性级统一施加；代理键幂等分配使语义层可反复重建而主键不漂移，支撑增量与回放。

**代价**：引入编译链路（`build_ontology`，`core/ontology.py:393`）的额外开销与一次性的映射维护成本；绑定声明错误会在装载期或运行期暴露，问题排查多了一层间接。

**被否决的替代方案**：直查业务冷表（开发最直接，但 N×M 适配成本与权限碎片化不可接受）；在业务库上建视图直连（仍把语义耦合在物理表结构上，无法支撑案件包隔离与离线单文件部署）。

### 2.5.2 ADR-2 声明式本体（ontology JSON）而非硬编码模型

**背景**：侦查研判的"查什么、怎么判定、谁能看"是高频变化的部分（规则阈值、对象属性、权限策略随案件演进），若把这些固化在 Python 代码里，每次调整都触发代码改动、评审与发版，且非工程人员无法参与。

**决策**：把"是什么"与"判定逻辑"全部外置为声明式 JSON，八段声明（`objects.json` / `links.json` / `bindings.json` / `rules.json` / `functions.json` / `actions.json` / `views.json` / `policies.json`，`schema_version = 2`）构成本体，装载顺序有硬依赖（`load_data_elements → … → 组装 OntologyPack`）。Python 只提供编译器、执行器与原子能力，"声明是数据，实现是代码"。

**理由**：调参、加规则、改权限均可在不触碰核心代码的前提下完成，分析师可主导演进；声明的结构化使静态校验（`validate_ontology.py --strict`）与失败留痕成为可能；schema_version 强制等于 2，否则装载期硬失败，杜绝半新半旧的本体进入运行态。

**代价**：编译器复杂度高，`core/ontology.py` 膨胀至 1380 行，成为全仓最难维护模块；声明式 DSL 存在学习曲线；装载链 13 步硬依赖，一处顺序错误会导致整包装载失败。

**被否决的替代方案**：Python 硬编码元数据模型（演进成本与发版耦合，非工程人员被排除在外）；ORM 映射框架（引入重依赖且无法表达"绑定 / 规则 / 权限"这类领域特有声明，反而弱化了对声明即数据的约束）。

### 2.5.3 ADR-3 类型层（objects/links）与管道层（bindings）分离

**背景**：对象的"类型定义"（它本身是什么、有哪些属性）相对稳定，而数据的"接入管道"（从哪个源表、用哪段 SQL、做哪些清洗）随数据源切换频繁变化。两者若混在一个文件里，任何管道调整都会牵连类型定义，反之亦然。

**决策**：类型层（`objects.json` 定义 pk / kind / name_property / properties；`links.json` 定义端点对象与边属性，**只表达关系不含检测判据**）与管道层（`bindings.json` 的 object_bindings / link_bindings，含 source / source_sql / clean / build_sql）严格分离。这是六条设计原则之一。

**理由**：类型定义稳定可复用，管道声明可针对不同案件包自由替换而不污染类型；同一对象类型可被多个源绑定，支撑多源 UNION 与跨案复用；缺列预检、脏值降级等机制可针对管道层独立施加。

**代价**：分析师接入新源须同时维护两层声明，心智负担增加；两层间的契约（如 binding 的输出列必须与类型属性对账，否则链接物化时硬失败）需要工具保障。

**被否决的替代方案**：类型与管道合一的单文件声明（最直观，但失去类型复用性与管道可替换性，违背案件包隔离诉求）；完全交给我们代码内部硬编码绑定（退回 ADR-2 已否定的硬编码路径）。

### 2.5.4 ADR-4 写路径唯一（ActionExecutor 是唯一写入口）

**背景**：线索状态、处置结论、决策依据属于高敏感写操作，且涉及人机边界（机器绝不置"已立案"）。若允许各模块直写数据库，写操作将散布在规则、看板、回写、MCP 等各处，无法统一施加状态机校验、角色校验与审计。

**决策**：所有写操作经 `ActionExecutor`（`core/action_executor.py`）；Function 只读，SQL 白名单强制首词为 `SELECT` / `WITH`。ActionExecutor 实施五步校验：角色必须是具名 human（`requires_role="human"` 且非占位 operator）、必填参数含 `legal_basis`、状态机 `ClueStatusMachine.validate`、显式 `only_from` 收紧、权限上下文一致性。第三条禁令以 `FORBIDDEN_OPERATORS = {system, ai, assistant, model, bot, auto, llm}` 强制人机边界，MCP `clue_transition` 额外拦截 `agent:` 前缀。

**理由**：写收敛使每一次状态变更都经过同一套校验与两阶段提交（submit → approve → dispatch → mark_confirmed，REQ-012），幂等键 `f"{action}:{clue}:{sha256(...)[:16]}"` 防止重复；与审计哈希链、`AccessContext` 五出口管控天然对齐；派发不可用时 fail-closed 为 `dispatch_failed` + critical 诊断，不假装在途（REQ-G-020）。

**代价**：写能力收敛牺牲了局部灵活性，新写场景必须先定义 ActionSpec 再走网关；两阶段提交引入 propose / approve 的人工环节，自动化的即时写被刻意排除。

**被否决的替代方案**：各模块直写 DuckDB（最灵活但无法统一审计与人机边界，违背第三条禁令与写路径唯一原则）；在 Function 内允许写（被只读红线 `_FORBIDDEN_SQL` 正则与 `_assert_readonly()` 首词校验直接否决）。

### 2.5.5 ADR-5 诊断与 findings 物理分离

**背景**：推理过程会产生大量健康度信号——零命中四分类、脏值降级计数、结构降级、派发失败等。若把这些诊断混入线索 findings，会污染"线索真实性"，使审计者无法区分"系统告诉我们发现了什么"与"系统告诉我们过程哪里不健康"。

**决策**：诊断数据统一落入 `run_diagnostic`（由 `core/run_health.py` 的 `RunHealth` / `NullRunHealth` 驱动，约 20 类 KINDS），与 findings 在物理上分表存放。降级协议（REQ-G）统一约定"降级但不静默，留痕但不污染"。`health=None` 时退化为 `NullRunHealth` 空操作。

**理由**：物理分离保证了线索集合的纯净——findings 只反映规则命中与实体关联，不受过程噪声干扰；诊断可独立查询、独立告警而不影响研判结论；与审计链、事件总线共同构成可观测性底座（`core/audit.py` / `core/event_bus.py`）。

**代价**：需要维护两套视图与两套生命周期，导出与看板须分别读取；消费方须理解何时看 findings、何时看 diagnostic，认知成本上升。

**被否决的替代方案**：同表加一列 `is_diagnostic` 标记（看似简单，但查询与导出极易误把诊断当作线索，且无法从存储层面保证审计纯净）；把诊断丢弃只保留异常（违反"失败要留痕，不许静默"的设计原则）。

### 2.5.6 ADR-6 图库作为可选增强而非核心依赖

**背景**：资金过桥、账户网络、两跳过桥等侦查场景天然适合图计算，但本实现图能力实测仅覆盖两跳定长、变长跳与 degree，且 LadybugDB 在 Windows 原生不可用，算法能力薄、平台依赖强。

**决策**：L4 图库（LadybugDB，`data/ladybug/*.lbug`）设为可选增强层。未安装时 `available=False`，全线降级 SQL 单轨并标记 `degraded`；已安装时走 Cypher / SQL 双轨互校（`compare_engines`），不一致只标注不采信。图计算仅作为确定性 SQL 单轨的旁证，而非结论来源。

**理由**：把图库降为可选，使系统在任何部署环境（含物理隔离的 Windows 节点）都能以一个确定的 SQL 单轨交付结果，避免图库可用性成为推理正确性的单点；双轨互校在不信任任一侧的前提下提供额外置信，符合确定性优先原则。

**代价**：失去社区发现、PageRank、中心性、最短路径、k-core 等高级图分析能力，复杂网络拓扑研判须以多跳 SQL 近似；平台限制使 Windows 下无法启用原生图加速。

**被否决的替代方案**：将图库作为核心存储与计算底座（会令 Windows 环境完全不可用，且薄算法能力成为系统正确性瓶颈）；完全放弃图能力（损失两跳过桥等直观研判手段，故选择"可选增强 + 双轨互校"折中）。

## 2.6 三条产品形态与职责边界

孙武侦查官对外交付为三条独立的产品形态，但它们共享同一套内核能力，且在依赖方向上严格单向：CLI、MCP、Web 都依赖 core/，core/ 不反向依赖任何一条形态。这种单向依赖是"内核确定性、形态可插拔"的保证——形态的增删不影响推理正确性，内核的演进也不必为形态妥协。

CLI（命令行）是 22 个 `scripts/` 脚本构成的离线操作面，覆盖从 `init_duckdb.py`（从 ontology 反推中文冷表 DDL，且不 import core）、`build_ontology.py`、`build_all.py`、`incremental.py`（季度增量）、`validate_ontology.py --strict` 到 `export_dashboard.py` / `export_ladybug.py` 等长尾工具，是内核最直接的调用者，面向工程与批处理场景。MCP Server 是 13 个工具（`scripts/mcp_server.py` 的 `_TOOL_IMPL`）构成的手写 JSON-RPC 2.0 over stdio 服务（协议版本 `2025-06-18`，零第三方依赖），是 Agent 接入本系统的唯一通道；其中 `review.submit_proposal` 是 Agent 唯一写通道，注入扫描 → 白名单清洗 → 状态变更拦截 → 建 `status=draft` 提案，永不自动生效，所有返回体强制挂 `needs_human_review / 定性_policy / disclaimer`。Web（FastAPI + Vue 3 + TypeScript）是人工处置看板的最终承载，以 DuckDB 处置表为真值源（`scripts/export_dashboard.py` 覆盖 JSON 快照）。

三条形态的职责边界可概括为：CLI 负责"构建与运维"，MCP 负责"Agent 受控读写"，Web 负责"人工研判与处置"。它们的写能力最终都汇流到 `ActionExecutor`，读能力都经过 `OntologyReadGateway`，因此无论哪种形态触发，数据流动的治理闸门（权限、审计、人机边界、诊断分离）都一致生效。值得特别指出，MCP 的 `clue_transition` 与 `review.submit_proposal` 在人机边界上比 Web 更受约束（Agent 写通道只产生 draft 提案、禁止 `agent:` 前缀操作人），这是因为 Agent 形态绕过了具名人工操作员的直接在场，必须用更紧的 fail-closed 来补偿信任缺口。
