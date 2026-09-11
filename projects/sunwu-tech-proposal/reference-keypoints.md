# 孙武侦查官 · 技术事实基线（参考资料压缩版）

> 用途：本文件是各章节写作的**唯一事实来源**。
> 全部内容来自 `D:\dev\inves_duckdb` 源码逐模块核对（2026-09-11），未引用既有 md/txt 文档结论。
> **写作纪律**：凡本文件未记载的数字、模块名、文件名，一律不得臆造。需要扩展说明时可用通用工程语言，但不得编造本项目的具体事实。

---

## 0. 项目身份

- **名称**：孙武侦查官 —— 确定性侦查推演内核
- **仓库**：`D:\dev\inves_duckdb`
- **技术栈**：Python 3.12+ / DuckDB / LadybugDB（可选）/ FastAPI / Vue 3 + TypeScript
- **核心命题**：把"侦查研判"从人的经验手艺，变成机器可复现、可回放、可追责的确定性计算过程
- **关键边界**：机器只做计算与提示，绝不做定性结论、绝不置"已立案"
- **离线能力**：内核零 LLM 依赖、零 API Key，可部署于物理隔离网络

### 规模实数（已核对）

| 项 | 值 | 核对方式 |
|---|---|---|
| core 内核 Python 模块 | 66（根 57 + `core/llm/` 9） | Glob |
| server/ Python 模块 | 79 | Glob |
| frontend/ | 57 个 .vue + 85 个 .ts | 项目结构快照 |
| ontology 声明文件 | 27 份 json，3 个包 | Glob |
| 测试文件 | 98 | Glob `tests/*.py` |
| **测试组** | **121**（`run_tests.py` GROUPS 注册表） | 读取源码计数 |
| 检测规则 | 仅 R1–R6 | `ontology/default/rules.json` |
| Function | 11（7 Python + 4 SQL） | `functions.json` + `FUNCTION_IMPLS` |
| MCP 工具 | 13 | `scripts/mcp_server.py` `_TOOL_IMPL` |
| CLI 脚本 | 22 | `scripts/` |

> ⚠️ **已知文档漂移**：`AGENTS.md` 写"70 组测试/73 组全绿"，实际注册表为 **121 组**。写作时以代码为准。

---

## 1. 三条禁令（代码强制，非软约束）

| 禁令 | 强制点 |
|---|---|
| ① 不自己写业务 SQL | `core/store.py` 维护 `_FORBIDDEN_TABLES = (银行流水, 通话记录, 招投标档案, 工商信息, 轨迹出行, 公开OSINT, 举报材料)`，命中抛 `DirectSourceAccessError`；`scripts/audit_straight_sql.py` 静态扫描，CI 测试组 `audit` 强制通过。绕过仅 `unsafe=True` 通道，必须带 `operator + reason` 并落 `meta_unsafe_query` |
| ② 不把原始明细搬进上下文 | Function 只返回聚合结果与溯源 ID；`core/row_uri.py` 用 `dataset@version#partition/rowid` 内容寻址，需要时 `resolve_row_uri` 回捞 |
| ③ 不下定性结论 / 不置"已立案" | `core/access.py` `HUMAN_ONLY_STATUSES = frozenset({"已立案"})`；`core/action_executor.py` `FORBIDDEN_OPERATORS = {system, ai, assistant, model, bot, auto, llm}`；MCP `clue_transition` 额外拦截 `agent:` 前缀 |

---

## 2. 六条设计原则

1. **确定性优先于智能** —— 核心链路不依赖大模型；LLM 仅为可选增强层（草案/解释/对齐），有一键降级开关与影子模式（REQ-040）
2. **声明是数据，实现是代码** —— "查什么、怎么判定、谁能看"写在 `ontology/<pack>/*.json`；Python 只提供编译器、执行器、原子能力
3. **类型层与管道层分离** —— `objects.json`/`links.json` 定义"是什么"，`bindings.json` 定义"数据从哪来"
4. **失败要留痕，不许静默** —— 零命中四分类、脏值降级计数、结构降级、派发失败全部进 `run_diagnostic`，与 findings **物理分离**
5. **未声明即拒绝（fail-closed）** —— 对象/链接未在 `policies.json` 声明 → 运行时拒绝；文件缺失 → 全拒；`schema_version != 2` → 装载期硬失败
6. **写路径唯一** —— 所有写操作经 `ActionExecutor`；Function 只读，SQL 白名单强制首词为 `SELECT`/`WITH`

---

## 3. 分层架构

### 存储层

| 层 | 载体 | 职责 |
|---|---|---|
| L0 | 原始文件 | Parquet / CSV / Excel / JSON / SQLite 五适配器（`data_ingest.py`） |
| L1 | 进程内 dict + `feat_l1` 表 | 特征缓存（REQ-015），`feature_set_hash = sha1(entity_id\|time_window\|feature_name\|inputs)`，输入变化即 `mark_stale` |
| L2 | DuckDB `investigation.duckdb` | 温层：中文业务冷表 + `obj_*`/`lnk_*` + 诊断表 + 审计链 |
| L3 | Parquet `data/*.parquet` | 冷层不可变分区 |
| L4 | LadybugDB `data/ladybug/*.lbug` | 图库（可选），未安装降级 SQL 单轨并标 degraded |

### 逻辑层

消费层（CLI / MCP / Web / LLM Agent / 导出）→ **统一读入口 OntologyReadGateway** → 语义层 `obj_*`/`lnk_*` → 规则引擎 / Function / 庙算 / 写路径

**五条出口统一管控**：`AccessContext`（frozen、operator 必填）贯穿 网关 / Function / Action / MCP / 导出。

---

## 4. 核心模块清单（`core/` 66 个）

### A. 存储与连接
- `store.py` — L1/L2/L3 三层门面。`Store(root="data", db_path="investigation.duckdb")`，`query/execute/cold_scan/set_feature`，`touches_forbidden_table()` 直查拦截
- `features.py` — `FeatureStore(conn)`，`put/get/get_or_compute/mark_stale/stale_hashes`
- `graph.py` — `GraphBackend(db_path="data/ladybug/investigation.lbug")`，`build_from_duckdb/overpass_two_hop/neighbors_within/compare_engines`

### B. 语义层
- `ontology.py` — **1380 行，全仓最大模块**。编译器 + 类型模型。`build_ontology(conn, pack, base_dir)`、`ensure_runtime_tables`、`materialize_changed`、`rebuild_from_partition`、`reverse_reach`
- `ontology_loader.py` — `load_pack(pack, base_dir)` → `OntologyPack`，schema_version=2 分层装载
- `ontology_version.py` — 版本时钟（REQ-001）：`compute_version/record_version/current_version/freshness/dependency_graph`，表 `meta_ontology_state`
- `ontology_profile.py` — 六层本体画像，`EntityLinkExplorer`（规则轨 + 别名轨双轨）
- `views.py` — Object Views（REQ-046），`ViewMaterializer` 查询时派生不物化
- `derived.py` — DerivedProperty 查询时派生（REQ-028），`_FORBIDDEN_REGISTRY_NAMES` 禁止打分进注册表
- `pack.py` — 多案件包隔离（REQ-044），`PackManager.list_packs/init_pack/assert_authorized/cross_pack_audit`
- `data_map.py` — 数据地图 L0/L1，零依赖（只 import json/re）
- `draft_assembler.py` — 新表接入草案（只写 `output/drafts/`，绝不写 `ontology/`）
- `rebuild_planner.py` — 增量影响范围（REQ-018），`RebuildPlan`，`DEFAULT_BATCH_THRESHOLD=5000`

### C. 权限面
- `access.py` — `AccessContext` frozen dataclass + `ROLE_RANK`/`ROLE_CLEARANCE`/`jian_clearance_for_role`/`require_llm_allowed`
- `policy.py` — `PolicyEngine(pack, path)`：`check_object/check_link/can_read_property/mask_value/apply_row_masks/coverage_missing`
- `gateway.py` — `OntologyReadGateway(conn, pack, *, access, allow_stale, base_dir)`，语义层唯一读入口（REQ-002）
- `runtime_context.py` — `ReadOnlyStore`（屏蔽 `execute/conn/_conn/l1`）+ `RuntimeContext`

### D. 规则与函数
- `rules.py` — `run_rules(store, stage, pack, rule_ids, health, base_dir)`、`_evaluate_single/_suppress_overlaps/_scan_rows/_classify_zero`
- `functions.py` — `FunctionExecutor(store, pack, access, health, base_dir)`，注册表 `FUNCTION_IMPLS`，`_assert_readonly/render_sql_template/check_param_value`
- `rule_dsl.py` — 规则 DSL（REQ-026），AST-only 不拼 SQL，`MAX_DEPTH=5`
- `threshold.py` — 阈值策略对象（REQ-027），分位数自适应 + `bounded_by` 夹紧
- `clean_ops.py` — **唯一清洗 op 注册表**（REQ-D-004），已注册 15 个 op
- `value_type.py` — 值类型识别（否定式 phone/id_card/date_str/amount/account/number + 肯定式 person/org）
- `data_elements.py` — 数据元标准注册表（REQ-D-001），`checksum_idcard_mod11`、`checksum_luhn`
- `de_recommend.py` — 数据元落点推荐（REQ-D-021），`CONFIRM_THRESHOLD=0.70 / HIGH_THRESHOLD=0.90`

### E. Action 与写路径
- `action_executor.py` — `ActionExecutor(store, pack, access, health, sink)`，`execute/submit/approve/dispatch/mark_confirmed/_create_decision`
- `registry.py` — `ClueStatus`（待查/查证中/已排除/已固证/已立案）、`ClueStatusMachine._TRANSITIONS`、`LineageClue`、`SkillRegistry`
- `disposal.py` — `DisposalBoard(clues, store, pack)`，`transition/verify/exclude/confirm/file/persist/restore/report`
- `outbox.py` — 回写发件箱（REQ-013），幂等键 `wb:{action_id}`
- `writeback.py` — `WritebackAdapter` Protocol + `StubLedgerAdapter` + `WritebackDispatcher`
- `reconcile.py` — 对账/重试/死信（REQ-014），`MAX_ATTEMPTS=5`，退避 1m→5m→30m→2h→8h，409 进 `manual_409`

### F. 事件 / 审计 / 诊断
- `event_bus.py` — `EventBus(conn)`，`publish/subscribe/replay/detect_cycle`；表 `event_log/event_dead_letter/event_idempotency`；**17 类事件**；环检测深度 > 10
- `audit.py` — `AuditChain(conn, case_id, health, backend)`，SHA-256 哈希链，`_GENESIS_HASH="0"*64`，duckdb/sqlite 双后端
- `run_health.py` — `RunHealth/NullRunHealth/get_health()`，`KINDS` 约 20 类，统一落 `run_diagnostic`
- `row_uri.py` — `dataset@version#partition/rowid`，`make_row_uri/parse_row_uri/snapshot_source_rows/resolve_row_uri`，表 `row_archive/row_build_index`
- `anomaly_channel.py` — 异常线索通道（REQ-G-019），`is_anomaly=True`、级别"待核实"，**绝不参与五间交叉升格**
- `metrics.py` — 规则运行时度量（REQ-030），`rule_version = hash(rule_id + rule_text + sorted params)`，`alert_override_rate()`

### G. 线索 / 假设 / 实体
- `hypotheses.py` — `MiaoSuan(pack)`：庙算假设引擎，`add/remove/reorder/promote/auto_from_findings/coverage/dimension_coverage/jian_coverage/conflict_check/enumerate_space`
- `lineage.py` — `source_overlap/dedupe_and_merge/cross_level/lineage_report/prioritize_clues`
- `entity.py` — `OrganizationResolver`、人名+组织对齐、`normalize_org_name`、`build_org_table_from_duckdb`
- `review.py` — `ReviewQueue.from_resolvers()`，accept/reject/defer
- `review_loop.py` — REQ-016 闭环：`review.decided → plan_from_review → materialize_changed → 只重算 affected_rules → finding.changed`；`entity_mapping` 受保护表
- `deferred.py` — defer 回捞（REQ-017），三类唤醒条件 `on_dataset/after/evidence_count_gte`（OR 语义）
- `case_library.py` — 案例库（REQ-031），`settle_fragment()` 四质量门（终态/脱敏/适用条件/legal_basis）
- `object_set.py` — ObjectSet 查询构造器（REQ-029），AST 节点 `Eq/Ne/In/Gt/Lt/And/Or/Not`，不拼 SQL
- `sampling.py` — 1% 采样预演，≥5% 全量 / 1~5% 扩样 / <1% 否方向
- `validate.py` — 六段输出结构校验，`REQUIRED_SECTIONS = [庙算基线, 双向盘点, 虚实扫描, 奇正分工, 用间交叉, 全胜校验]`
- `geo.py` — 地点标准化/同框（REQ-G-021），离线，去 K 桩号/方位距离修饰
- `ingest_validate.py` — 分区校验（REQ-005），四类 `DATA_GAP/PK_DUPLICATE/SCHEMA_DRIFT/TIME_NON_MONOTONIC`
- `search.py` — Semantic Search（REQ-042），Qwen embedding，无 key 离线降级返回 0

### H. 检测器（4 个同签名 `scan(gateway, ...)`）
- `compliance.py`（REQ-D-016 数据元合规）、`sensitive_scan.py`（REQ-D-018 敏感列，`hit_ratio` 默认 0.3）、`unit_scan.py`（REQ-D-020 单位口径，中位数差 ≥ `ratio_threshold=10000`）、`data_freshness.py`（REQ-D-019，`stale_days=180`，空时间列跳过不误报）

### I. 治理 / 提案 / LLM
- `parameters.py`（REQ-032 参数治理，`MIN_SAMPLE_SIZE=20`，状态 draft/shadow/production/retired）
- `proposal.py`（REQ-033，七项硬校验，`confidence_only_sorts()`）
- `core/llm/redact.py`（REQ-038 脱敏，PII 正则**顺序敏感**：身份证先于银行卡）
- `core/llm/guard.py`（REQ-039 提示注入防护）
- `core/llm/llm_client.py`（DashScope 兼容，`fake_invoke` 注入）
- `core/llm/draft_rule.py` / `explain.py`（含 `_QUALITATIVE_WORDS` 定性词黑名单）/ `align.py` / `plan.py` / `fallback.py`（REQ-040 降级开关 + `LLM_OWNED_TABLES`）

---

## 5. 语义层编译内核

### 八段声明（schema_version = 2）

| 文件 | 层 | 内容 |
|---|---|---|
| `objects.json` | 类型层 | pk、kind(entity\|event)、name_property、`properties{属性:值类型}`；**不含数据来源信息** |
| `links.json` | 类型层 | 端点对象、边属性；**只表达关系，不含检测判据** |
| `bindings.json` | 管道层 | object_bindings（source/source_sql/clean/optional）+ link_bindings（build_sql） |
| `rules.json` | 规则手册 | `rule_text`（自然语言判据）+ `function/params`（唯一机器挂钩） |
| `functions.json` | 能力目录 | SQL 强制 SELECT/WITH 白名单 + `{{param}}`；py 需注册 `FUNCTION_IMPLS` |
| `actions.json` | 写路径 | 角色、必填参数、副作用、终态；`allowed_from` 由 `states.json` **反向派生** |
| `views.json` | 语义视图 | 按角色投影，查询时派生不物化；权限仍按 base_object 判定 |
| `policies.json` | 权限面 | 对象级/链接级策略 + 属性级敏感列遮蔽；**未声明一律 fail-closed** |

非八段配置/知识文件：`thresholds.json`、`case_knowledge.json`（**人名唯一合法存放处**）、`dimensions.json`、`enum_space.json`、`jians.json`、`states.json`、`scoring.json`、`derived_properties.json`、`data_elements.json`、`llm_policy.json`、`clean_rules.json`。

### 装载顺序（有硬依赖）
`load_data_elements` → `_load_property_mask_set` → `load_jians` → `_load_objects` → `_load_links` → `_load_bindings` → `load_states` → `_load_actions` → `_load_functions` → `load_dimensions` → `_load_rules` → `load_enum_space` → `_load_clean_rules` → 组装 `OntologyPack` → 写 `_PACK_CACHE`（mtime 指纹）

### 编译流程（`build_ontology`，`ontology.py:393`）
1. `load_pack` → `_default_org_names` + `build_clean_context` → `_load_entity_mapping`
2. `BEGIN TRANSACTION` → 逐 Object（跳过 runtime）：`_compute_object_rows` → `_create_obj_table` → `_insert_object_rows` → 逐 Link `CREATE TABLE lnk_x AS build_sql`（声明边属性与输出列对账，**不一致硬失败**）→ `COMMIT`，异常 `ROLLBACK`
3. `ensure_runtime_tables`
4. `ViewMaterializer.materialize_all(skip_missing_base=True)`
5. `compute_version` + `record_version`
6. `snapshot_source_rows`

### 代理键分配（`ontology.py:978`，在清洗/归并/去重之后）
- **event 型**（transaction/call/trackpoint）：排序后 `sha1(json.dumps(行内容))[:12]`，同内容追加 `_02/_03`；`exclude_idx` 排除 composite 列
- **entity 型**（person/org/account…）：按 name_property 值排序，`{name: f"{prefix}_{sha1(name)[:12]}"}`，**幂等，新增名字不改旧键**
- **自引用**（pk == name_property，如 clue）：直通源自然键
- `prefix = otype.pk.split("_")[0]`
- **意义**：语义层可反复重建，主键不漂移

### 类型系统
`TYPE_SQL`：`string→VARCHAR, integer→BIGINT, decimal→DOUBLE, date→DATE, boolean→BOOLEAN, timestamp→TIMESTAMP, duration_days→INTEGER, enum→VARCHAR, json→VARCHAR`

### 脏值降级
- 默认 `TRY_CAST` 降级 NULL 并记 `obj_x.prop<-raw: N 行不可转 t`；`on_cast_error=fail` 才硬失败；`quarantine` 整行剔除落 `build_quarantine`（追加隐藏列 `__raw_<alias>` 判定）
- `raw=None`（缺列）渲染 `CAST(NULL AS TYPE)` 保持列集列序
- transform op 链经 `compile_sql_expr()` 注入，位于 CAST **之前**
- 缺列预检 `DESCRIBE`：必填缺失硬失败；`optional_columns` 缺失降级类型化 NULL
- 多源 UNION 走 `_prune_union_sql()` 按已挂载表裁剪分支

### 版本时钟与增量
- 双 watermark：源端（源表含"日期/date/time"列取 MAX）vs 语义层 → `STALE + affected_objects`；无版本 → `UNBUILT`
- `_compute_input_hashes()` = sha256（按 pk 排序的全行 JSON）；`_compute_params_hash()` 含 rules params + functions parameters + **views 声明**
- `materialize_changed(conn, plan)`：对象先于链接 → TEMP `_STAGE` → `_diff_apply()` 行级 diff → 推进版本 → `snapshot_source_rows` → 发 `ontology.materialized`

---

## 6. 规则与 Function

### R1–R6（`ontology/default/rules.json`）

| 编号 | 名称 | 阶段 | 维度 | 间类 | Function | 判据 |
|---|---|---|---|---|---|---|
| R1 | 季度末整数现金存入 | 虚实 | 资金 | 生间 | `quarter_end_integer_deposits` | 金额对 `round_unit=10000` 取模为 0 **且** `to_raw='现金存入'`，日期距季末 ≤ `quarter_end_window_days=15` 天；按季度聚合。**组内 primary** |
| R2 | 整数转账聚合（第三方过桥） | 虚实 | 资金 | 反间 | `integer_transfer_aggregates` | `amount % 10000 = 0`，按 from→to 聚合单向大额链条。与 R1 同 `exclusive_group=integer_amount`，`overlap_resolution=drop_if_primary_hit` |
| R3 | 招投标公示期通话频次突增 | 虚实 | 通讯 | 生间 | `call_frequency_spike` | 头部对端通话数 ≥ 其余对端**中位数 ×2**；单一对端无法算中位数 → **降级**为绝对阈值 ≥30 并标 degraded |
| R4 | 二人公示期轨迹同框 | 虚实 | 行为 | 生间 | `co_located_pairs` | 直接 SELECT `lnk_co_located`（不同主体同地点 ±1 天） |
| R5 | 工商登记利益关联 | 虚实 | 关系 | 因间 | `org_interest_links` | 遍历 `obj_org`，`legal_rep`/`relation` 命中 `case_knowledge.json` 的 `subject_aliases` 或未过期 `relation_assertions` |
| R6 | 中标-资金时间窗碰撞 | 奇正 | 时间 | 反间 | `time_window_collision` | `lnk_time_window JOIN obj_bid_project`：金额 `%10000=0` **且** `owner_raw NOT LIKE '%公司%'`（排除对公，只留个人），按 `offset_days` 排序 |

> ⚠️ 代码注释里的 R5/R9/R13 等是 **REQ 需求编号**，不是检测规则编号。检测规则只有 R1–R6。

### 11 个 Function

**SQL 实现（4）**：`quarter_end_integer_deposits`、`integer_transfer_aggregates`、`co_located_pairs`、`time_window_collision`
**Python 实现（7）**：`call_frequency_spike`、`call_pair_coverage`、`jian_cross_level`、`tipoff_cross_reference`、`org_interest_links`、`overpass_two_hop`、`location_colocated`（实现在 `core/geo.py`）

### 执行机制
- `run_rules(store, stage="xu_shi", pack, rule_ids, health, base_dir)`：过滤 stage / rule_ids / `enabled=False` → `resolve_rule_params()` 阈值解析 → `fx.invoke()` → `_evaluate_single()` → 末段 `_suppress_overlaps()`
- `_evaluate_single`：`hit_when="result_hit"` 读 `result.hit`；否则 py 读 `result["rows"]`、SQL 读 `out["rows"]`，按 `subject_column` 提首行主体名
- 每条 finding 附 `rule_id/rule_text/dimension/jian_types/assumption/threshold_method/threshold_value/is_degraded`

### 零命中四分类（REQ-G-002）
| 分类 | 级别 | 含义 |
|---|---|---|
| `data_absent` | info | 数据本来就没有——不是"干净"，是"没数据" |
| `config_missing` | warning | 配置缺失导致跑不起来 |
| `empty_result_suspect` | warning | 数据齐全却零命中，**可疑**，需人工确认 |
| `clean_scan` | info | 确属干净——**必须由分析师在 rules.json 显式声明 `zero_is_clean`**，系统不许自行解释 |

### 只读红线
- `_FORBIDDEN_SQL` 正则命中 INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/ATTACH/DETACH/COPY/TRUNCATE/GRANT/REVOKE 即拒；`_assert_readonly()` 另要求首词为 `SELECT`/`WITH`
- `{{param}}` **双向核对**：SQL 占位符与 `parameters` 必须一一对应（多/少都硬失败）；integer 拒 bool、boolean 严格 isinstance、date 须 `YYYY-MM-DD`；**string 必须声明 enum 白名单，自由文本一律拒绝**
- py/sql 函数抛 `CatalogException/BinderException` 且引用 `obj_*/lnk_*` → **结构降级为零命中 + degraded 留痕**，不崩管线

---

## 7. 业务模型：五间 × 五维

### 五间（`jians.json`，声明式）
| 间类 | 来源对象 | weight | default_clearance | 含义 |
|---|---|---|---|---|
| 因间 | org、bid_project | 3 | 1 | 利用敌方既有关系网络 |
| 内间 | tipoff | 5 | 3 | 内部举报，权重最高、密级最高 |
| 反间 | transaction | 2 | 1 | 从资金往来反向获取信息 |
| 死间 | osint_article、org | 4 | 0 | 公开情报，密级最低 |
| 生间 | call、trackpoint | 1 | 1 | 当场取得的动态信息 |

**交叉升格**：1 源 → 观察 / 2 源 → 线索 / 3 源以上 → 可立案依据候选。**映射硬编码不可配**（名称可配）。

### 五维（`dimensions.json`）
资金（transaction）、通讯（call）、行为（trackpoint）、关系（person/org）、时间（transaction/call/trackpoint）

### 五技能（`skills/registry_bootstrap.py:245`）
`miaosuan`（庙算）→ `zhi_ji_zhi_bi`（知己）→ `xu_shi`（虚实）→ `qi_zheng`（奇正）→ `yong_jian`（用间）

---

## 8. 线索生命周期与写路径

### 状态机
`待查 → 查证中 → 已固证 → 已立案`（human 专属），分支终态 `已排除`

### ActionExecutor 校验五步
1. 角色：`requires_role="human"` 且 `_is_placeholder_operator()`（空/system/ai/assistant/model/bot/auto/llm）→ ValueError
2. 必填参数：`file` 必须 `legal_basis`
3. 状态机：`ClueStatusMachine.validate`
4. `only_from` 显式收紧（如固证仅可从"查证中"发起）
5. 权限上下文（非 system）：`access.can_transition()` + operator 一致性

### 两阶段提交（REQ-012）
`submit()`（status=proposed，幂等键 `f"{action}:{clue}:{sha256(...)[:16]}"`）→ `approve(action_id, operator)`（必须具名）→ `dispatch()`（未 approve 抛 `NotApprovedError`）→ `Outbox.enqueue()` → `pending_receipt` → `mark_confirmed()` → `confirmed`
**派发 fail-closed（REQ-G-020）**：outbox 不可用 → `dispatch_failed` + critical 诊断，**不假装在途**

### `file` 副作用建 `obj_decision`
`_create_decision()`：`sink is not None`（Web state.sqlite）→ 委托 sink；否则 `ensure_runtime_tables` → `INSERT INTO obj_decision`（9 列：`decision_id=f"decision_{int(time.time()*1000)}"`、target_status、clue_id、legal_basis、operator、note、created_at、metadata、source_rows）→ `INSERT INTO lnk_decision_for`

### 闭环
- **人审闭环（REQ-016）**：review 裁决 → `plan_from_review` → `materialize_changed` → 只重算 `affected_rules` → `finding.changed`；`entity_mapping` 受保护
- **defer 回捞（REQ-017）**：`on_dataset/after/evidence_count_gte`（OR），条件不可解析留痕告警
- **案例库（REQ-031）**：四质量门（终态/脱敏/适用条件/legal_basis）

---

## 9. 权限与合规

### 角色两把尺子（**禁止互比**）
| 角色 | ROLE_RANK | ROLE_CLEARANCE |
|---|---|---|
| 见习 | 0 | 0 |
| 正兵 | 1（默认） | 1 |
| 偏将 | 2 | 3 |
| 主办 | 3 | 3 |
| human | 4 | 3 |
| system | 99（唯一旁路） | 99 |

### AccessContext（frozen）
字段：`operator`（必填非空）、`role="正兵"`、`case_id="default"`、`purpose=""`、`clearance=1`、`network="local"`
三重校验：operator 空 / network ∉ (local, isolated, web) / role ∉ ROLE_RANK → ValueError
`can_llm_call()` 在 `network="isolated"` 返回 False

### 三级鉴权
- 对象级 `check_object`：`policies.json` 查不到 → `PolicyDeniedError`（fail-closed）
- 链接级 `check_link`：同口径
- 属性级 `mask_value`：`partial` 保前 3 后 4（`310****1234`），长度 <8 全遮；`full` → `***`；`value_profile/distinct_values` 连 samples/min/max 一起遮蔽

### fail-closed 三处
① `policies.json` 缺失 → `object_policies={}` + `_missing_file=True`（全拒）② `schema_version != 2` → ValueError ③ 声明角色集 ∉ ROLE_RANK → ValueError

### LLM 治理
`core/llm/redact.py` 脱敏（PII 正则**顺序敏感**：身份证先于银行卡，否则 18 位身份证被 16-19 位银行卡规则吃掉）；`guard.py` 提示注入防护；`explain.py` `_QUALITATIVE_WORDS` 定性词黑名单；`fallback.py` 一键降级 + 影子模式 + `LLM_OWNED_TABLES`；`proposal.py` 七项硬校验，**confidence 只排序不定性**

---

## 10. 可观测性

| 机制 | 实现 | 要点 |
|---|---|---|
| 审计哈希链 REQ-007 | `core/audit.py` `audit_chain` | SHA-256，取上条 signature 作 prev_hash，链首 `"0"*64`；锚点缺失打 `anchor_status=missing` 不改表结构；duckdb/sqlite 双后端签名等价 |
| 事件总线 REQ-006 | `core/event_bus.py` | 17 类事件；`event_log` + `event_dead_letter` + `event_idempotency`；payload_hash 幂等；`detect_cycle()` 深度 >10 判环；支持 replay |
| 运行健康度 REQ-G-010 | `core/run_health.py` | ~20 类，统一落 `run_diagnostic`，**与 findings 物理分离**；`health=None` → NullRunHealth 空操作 |
| 内容寻址溯源 REQ-008 | `core/row_uri.py` | `dataset@version#partition/rowid`，表 `row_archive/row_build_index` |
| 规则度量 REQ-030 | `core/metrics.py` | `rule_version = hash(rule_id + rule_text + sorted params)`；命中率、精确率估计、**推翻率告警** |

---

## 11. 数据流（主入口 `run_all.py:47 main()`）

```
data.gen_sim → scripts.init_duckdb（从 ontology 反推中文冷表 DDL，不 import core）
→ DataIngestManager.ingest_directory（5 适配器）
→ core.entity.run_entity_resolution / apply_org_to_duckdb
→ core.review.ReviewQueue → output/review_queue.json
→ core.sampling.SamplingPreflight(1%)
→ core.ontology.build_ontology  ★语义层
→ 质量门：compliance / sensitive_scan / data_freshness / unit_scan（只告警不阻断）
→ skills.xu_shi.run → core.rules.run_rules → FunctionExecutor.invoke
→ MiaoSuan.auto_from_findings
→ skill_invoke(xu_shi/qi_zheng/yong_jian)
→ lineage.dedupe_and_merge(0.5) → prioritize_clues → lineage_report
→ graph.GraphBackend（两跳过桥 Cypher/SQL 双轨）
→ DisposalBoard → persist → 重跑 build_ontology 刷新 obj_clue
→ 重算 report + metrics.alert_override_rate + audit.chain_integrity
→ anomaly_channel.emit_anomaly_clues
→ output/lineage_clues.json、output/entity_mapping.json
```

### 产物
| 文件 | 生成处 | 内容 |
|---|---|---|
| `output/lineage_clues.json` | `run_all.py:357` | 主报告：健康度、clues、jian_coverage、cross_level、miao_coverage、审计链完备性、异常线索_待核实、ontology 统计、graph_overpass |
| `output/entity_mapping.json` | `run_all.py:360` | `{person, org, review}` |
| `output/review_queue.json` | `run_all.py:131` | 人审队列 |
| `output/dashboard_data.json` / `.html` | `scripts/export_dashboard.py` | 以 DuckDB 处置表为**真值源**（覆盖 JSON 快照） |
| `output/drafts/` | `core/draft_assembler.py` | 新表接入草案 |
| 图谱 CSV | `scripts/export_ladybug.py` | 4 类节点 + 9 类边文件，导出前策略检查 |

---

## 12. 图库（L4）

### 建图流程（`core/graph.py:97-158`）—— 严格先节点后边
1. `_flow_source()`：有 `lnk_transfers` 取语义层，否则回落 L2 `银行流水`（**两轨必须同源**）
2. 节点 = 主体 ∪ 对方 去重排序（**必须全部导入**，否则 COPY 边报 "Unable to find primary key value"）
3. 导出 `nodes.csv`/`edges.csv` 到临时目录
4. `DROP TABLE` → `CREATE NODE TABLE Entity(name STRING, PRIMARY KEY(name))` + `CREATE REL TABLE TRANSFER(FROM Entity TO Entity, amount DOUBLE, tdate STRING)`
5. `COPY ... (HEADER=true)`，路径用 `as_posix()`（Windows 反斜杠被 Cypher 解析器当转义序列）

### 图算法能力（**实测边界**）
- ✅ 两跳定长 `MATCH (a)-[e1]->(m)-[e2]->(b)`，排除自环
- ✅ 变长跳 `MATCH (a)-[:*1..N]->(b)`
- ✅ degree（度）统计（`server/app/graph_view.py`）
- ❌ **未实现**：Louvain/LPA 社区发现、PageRank、中介/接近中心性、最短路径、k-core

### 双轨互校
`compare_engines()` 按 `(source, bridge, dest)` 规范化键比对（金额不参与，防浮点差）；不一致只标 `only_in_cypher/only_in_sql`，**不自动采信任一侧**

### 环境限制
- **Windows 原生不可用**：LadybugDB 官方 CI 不构建 Windows 版扩展，走 CSV 中转；`ATTACH DuckDB` 在 Windows 不可用
- WSL/Linux 可用，需手工把 `libduckdb.so` 放到 `~/.lbdb/extension/<版本>/<平台>/common/`（放 /usr/local/lib 无效）
- ladybug 未安装 → `available=False`，全线降级 SQL 单轨

---

## 13. MCP Server（13 工具）

手写 JSON-RPC 2.0 over stdio，零第三方依赖，协议版本 `2025-06-18`。

| # | 工具 | 功能与约束 |
|---|---|---|
| 1 | `scan_anomaly` | 按 scope(flow/call/trajectory/all) 过滤，只返摘要 |
| 2 | `cross_jian` | 五间交叉等级与覆盖度 |
| 3 | `graph_overpass` | 两跳过桥双轨（无图库降级 SQL 单轨标 degraded） |
| 4 | `clue_list` | **状态源强制 DuckDB**（不用 JSON 快照），按间类密级过滤 |
| 5 | `clue_transition` | 禁 system/ai/agent 操作人；置"已立案"必带 legal_basis 且只能从"已固证" |
| 6 | `run_pipeline` | **需 `confirm=true`** |
| 7 | `function_list` | Function 目录（只读声明） |
| 8 | `function_invoke` | 调只读 Function |
| 9 | `rule_list` | 规则手册目录（rule_text 原文 + 绑定 + 阈值） |
| 10 | `review.list_pending` | 待确认候选，**不附 evidence 明细** |
| 11 | `review.get_evidence` | **正兵及以下只给类型/计数摘要** |
| 12 | `action.status` | `act_` 走 ActionExecutor，`pp-` 走 ProposalStore |
| 13 | `review.submit_proposal` | **Agent 唯一写通道**：注入扫描 → 白名单清洗 → 状态变更拦截 → 建 status=draft 提案，**永不自动生效** |

所有返回体强制挂 `needs_human_review / 定性_policy / disclaimer`。冒烟测试 `scripts/mcp_client_test.py` 69 项断言。

---

## 14. CLI 脚本（`scripts/` 22 个）

`init_duckdb.py`（从 ontology 反推冷表 DDL，不 import core）、`build_ontology.py`（`--actions`/`--functions` 查看注册表）、`build_all.py`、`export_dashboard.py`、`build_dashboard.py`、`mcp_server.py`、`mcp_client_test.py`、`export_ladybug.py`、`verify_ladybug.py`、`q2_overpass_cypher.py`、`incremental.py`（季度增量）、`validate_ontology.py`（`--strict`）、`init_pack.py`、`scan_hardcoded_names.py`（SQL 不得硬编码人名）、`audit_straight_sql.py`（直查扫描）、`pr_impact.py`（PR 影响报告）、`profile_table.py`、`demo_profile.py`、`gen_reqd_case.py`、`semantic_search_demo.py`、`writeback_console_demo.py`、`install_tests.py`

---

## 15. 案件包

| 包 | 案件 | 内容 |
|---|---|---|
| `default` | 张卫国受贿/串标案 | 11 对象、10 链接、6 规则、11 Function、27 份声明 |
| `reqd_case` | 海州"11·03"电诈案 | 7 对象、2 链接；含 `call_old`（缺对端列降级）、`cp_split`（拆分路径）、`cp_whole`（composite 整列降级）三条脏数据路径 |
| `_shared` | 共享数据元 | `DE_IDCARD`/`DE_PHONE`/`DE_AMOUNT`/`DE_DATE`，含 idcard_mod11、luhn |

**default 对象（11）**：person、org、account、transaction、call、trackpoint、bid_project、clue、decision(runtime)、tipoff、osint_article
**default 链接（10）**：transfers、calls_to、owns、involved_in、co_located、time_window、decision_for(runtime)、tipoff_targets_person、tipoff_from_reporter、osint_mentions

**PackManager（REQ-044）**：每包独立 `investigation_<pack>.duckdb`，跨包 `assert_authorized` + `cross_pack_audit`

---

## 16. 新数据源接入路径（6 步）

1. `profile_table.py` 列画像，值类型识别
2. `DraftAssembler` 生成 objects/links/bindings 三份草案（**只写 `output/drafts/`，绝不写 ontology**）
3. 分析师审阅后落到 `ontology/<pack>/`
4. 若需新能力：`functions.json` 声明（py 需注册 `FUNCTION_IMPLS`）+ `rules.json` 写 rule_text 与 params
5. **在 `policies.json` 补策略**（漏了运行时被 fail-closed 拒绝）
6. `validate_ontology.py --strict` → `build_ontology.py`

**全程不改检测器 Python 代码。**

---

## 17. 测试与质量

- `run_tests.py` GROUPS 注册表 **121 组**，`tests/` 98 文件
- 每组绑定需求编号：REQ-001~046、REQ-G-001~024、REQ-D-001~022、REQ-P M1~M6、Web M1~M6
- 两条静态红线进 CI：`audit` 组（直查扫描）、`guard` 组（直查拦截）
- MCP 端到端：`scripts/mcp_client_test.py` 69 项断言
- `--fast` 跳 e2e，`--only <组>` 单跑
- **降级协议（REQ-G）**约 20 个测试组，统一协议：降级但不静默，留痕但不污染

---

## 18. 已知风险（写作时可作为"风险与演进"章节素材）

1. **文档漂移**：`AGENTS.md` 写 70/73 组，实际 121 组
2. **图算法能力薄**：无社区发现/PageRank/中心性/最短路径
3. **Windows 图库不可用**：官方不构建 Windows 扩展
4. **规则偏少**：R1–R6 中资金占 3 条，时间维度仅 R6 且依附资金判据
5. **两把角色尺子认知负担**：rank 与 clearance 数值不一致（偏将 2/3，human 4/3）
6. **启发式扫描是告警非判定**：`sensitive_scan`（hit_ratio 0.3）、`unit_scan`（≥10000 倍）
7. **`core/ontology.py` 1380 行**是全仓最难维护模块；`core/` 与 `server/` 存在职责重叠（各有 disposal/graph 实现），需警惕逻辑双写

---

## 19. 业务场景适配

| 场景 | 适配点 |
|---|---|
| 职务犯罪侦查 | 资金 + 工商关系 + 招投标时间窗 + 通话突增四维交叉（R1–R6 原型） |
| 电信诈骗资金链追踪 | 两跳过桥双轨 + 账户网络；`reqd_case` 为验证样本 |
| 招投标串标识别 | `lnk_time_window` 耦合中标时间与资金往来 + 工商法人三角验证 |
| 企业合规/反舞弊内审 | 内间(举报) + 因间(工商) + 死间(OSINT) 交叉升格，密级体系适配内审分层 |
| 通用可审计调查 | 声明式本体 + 确定性规则 + 哈希链审计，与具体罪名无关 |

---

## 20. 勘误（章节写作阶段回读源码发现）

以下三项在写作过程中由章节执笔人回读源码发现，与本文前述记载有出入，**以本节为准**：

| # | 原记载 | 实际情况 | 发现章节 |
|---|---|---|---|
| 1 | `core/llm/proposal.py` | 实际为 **`core/proposal.py`**（不在 llm 子目录） | 第 7 章 |
| 2 | `core/run_health.py` KINDS"约 20 类" | 实际 `KINDS` 元组含 **28 项** | 第 7 章 |
| 3 | `AGENTS.md` 写"70 组 / 73 组全绿" | 实际只有"70 组测试，必须全绿"一处表述，无"73 组"字样；注册表实为 121 组 | 第 8 章 |

**方法论结论**：本项目文档与代码存在系统性漂移，任何引用既有 md 的数字都应回读源码复核。这也是第 8 章 8.5 第 1 项把"文档漂移"列为首要技术债的原因。
