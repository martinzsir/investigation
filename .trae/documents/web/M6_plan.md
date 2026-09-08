# M6 前端配套后端端点补齐里程碑实施计划（REQ-W-P-001~017）

**日期** 2026-09-08 ｜ **状态** 待评审（v2：契约字段级对齐前端验收红线）
**关联** [前端需求清单.md](file:///d:/dev/inves_duckdb/.trae/documents/web/frontend/前端需求清单.md) v1.8 附录 F（backlog 事实源）与四、页面层 FE-P 验收标准、[配置中心后端API核对报告.md](file:///d:/dev/inves_duckdb/.trae/documents/web/frontend/配置中心后端API核对报告.md) §3、[backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)、[M5_plan.md](file:///d:/dev/inves_duckdb/.trae/documents/web/M5_plan.md)（已实施，121 组全绿）
**范围** 附录 F 待补端点，编号 **W-P-001 ~ W-P-017**（W-P = Web 页面配套）：
- 高（批次 2 前）：W-P-001 处置看板、W-P-002 列分析、W-P-003 映射保存、W-P-004 关系图谱
- 中（批次 2）：W-P-005 庙算假设、W-P-006 数据画像、W-P-007 接入建议、W-P-008 质量检查、W-P-009 隔离区、W-P-010 清洗留痕、W-P-011 数据元、W-P-012 ETL 管道
- 低（批次 4）：W-P-013 系统设置、W-P-014 任务取消、W-P-015 门户汇总/归档、W-P-017 配置中心
- **W-P-016 函数目录：不实施**（FE-P-003 已明定"函数目录取规则响应内 `function_catalog` 字段，无独立 functions 端点"；附录 F 原文即"或 rules 响应继续内嵌"。编号保留以闭合 backlog 对照表）
**不含** 前端页面、Postgres、LadybugDB 在线图库（W-P-004 走语义层 DuckDB，前端约定端点缺失即降级关系表格）、GB 级性能验证、settings/logging 与配置导入导出（核对报告标"可选"，后续批次）、平台阈值对既有案件快照的回灌（仅作新建案件默认，见 D-M6-10）。

**最小影响红线（继承 M1~M5）**：M6 对 `core/` 的改动目标为**仅可选参数追加、缺省零行为变化**（M3/M4 同模式）。案件快照隔离要求 server 读面消费建案锁定的快照（`cases/{cid}/ontology/{pack}/`），而 `OntologyReadGateway`/`OntologyProfiler`/四类质量扫描内部 `load_pack(pack)` 均隐式读模板包、无 `base_dir` 入口——唯一 core 触点是 6 个文件追加可选 `base_dir`（缺省 None = CLI/MCP 现状）。

**server 纪律不变**：router 不直连 DuckDB（storeback 门禁）、不写业务 SQL（聚合落组装器）、写操作一律入队（业务面短频写走 state.sqlite 快速通道任务）、未 BUILD 逐节降级不 500、跨租户一律 404、信封 `{ok,data,data_version}`/`{ok:false,error:{code,message}}`、列表 `{items,total,page,page_size}` 且 page_size≤50、operator 只取会话不取请求体。

---

## 一、仓库研究结论（M5 交付现状与数据锚点）

1. **路由/组装器/存储模式成熟**：router 薄壳（`get_principal` + `_get_owned_case` + `factory.for_case(mode="read")` + 组装器 + 信封），组装器范式见 [dashboard.py](file:///d:/dev/inves_duckdb/server/app/dashboard.py)、[clues_view.py](file:///d:/dev/inves_duckdb/server/app/clues_view.py)；state.sqlite 写面经 worker 快速通道（DISPOSE/REVIEW 先例）。
2. **看板数据齐备**：产物 `artifacts/clues_v{N}.json`（clues_view 已封装加载/真值拼接/内间秩级过滤），state `clue_disposal_status` 含 operator/note/updated_at；五态常量 `core.registry.ClueStatus`。缺分组+停留天数组装器。operator 为 ID（server 无姓名表，署名映射前端按 me 处理，与审计链一致）。
3. **图谱取数**：links.json `endpoints.from/to`（`col`=lnk 表列名，`ref.object/key/name` 指向对象类型/主键/名列，[links.json](file:///d:/dev/inves_duckdb/ontology/default/links.json)）；objects.json 声明 `kind`(entity/event)/`name_property`/`jian`（五间归类）/`title`。event 型（交易/通话/轨迹点）是边载体不作节点。
4. **向导半成品**：upload 已返回列画像+declared_tables（[routers/ingest.py](file:///d:/dev/inves_duckdb/server/app/routers/ingest.py)）；`worker/ingest.py` 有 `declared_source_tables()`/`validate_mapping()`；meta `case_sources.mapping_json` 列已存在（仅 imported 写入），缺 staged 草稿 UPDATE；import 端点当前只认请求体 mapping，需补"未显式传 mapping 时回落 source.mapping_json"闭环。
5. **画像 core 能力**：`OntologyProfiler(gateway, pack, focus_entities, anchor_date, ..., clean_stats).profile_all()` 六层直出（L1/L2 属性画像含 null_rate/distinct/samples、L3 指标、L4 变体 rule/alias、L5 扣分明细、compliance、health）；**clean_stats 需调用方传入**——server 从 run_diagnostic（clean_drop_rate 诊断 detail 含 object/prop/rules/dropped/rows_before/sample_masked）重建；focus_entities 从 obj_person 行数 top N + 高优线索主体派生；anchor 缺省时窗口指标 not_evaluated（Profiler 自带降级）。
6. **质量/清洗数据源已落盘**（关键）：
   - `build_quarantine` 表（BUILD 期 CAST 失败整行隔离：object/property/src_column/reason/sample_masked/name_value/source_table/quarantined_at）——cast_error 行级；
   - `run_diagnostic`：`source_value_cast_failed`（脏值）、`source_column_missing`（缺列）、`clean_drop_rate`（null_policy:reject 剔除，detail 带 dropped_rows/rows_before/rate/rules/sample_masked 前 3）、`dedup_key_conflict`（业务键去重收敛留痕，同样带样本）；
   - 落账函数 `core/run_health.py record_clean_stats/record_dedup_conflicts` 已在 BUILD 链路调用；
   - 四类扫描 `compliance/sensitive_scan/data_freshness/unit_scan` 均 `scan(gateway, *, health=None, ...)`，health=None 走 NullRunHealth 不写库、返回聚合 dict。
7. **庙算口径**：维度双覆盖诊断已落 run_diagnostic（source=`miaosuan:dimension` 声明 / `miaosuan:dimension:empirical` 实证，dashboard 已消费）；MiaoSuan 沙盘为 run_all 内存态、server 无持久化——hypotheses 端点为**派生视图**（线索产物聚合热力 + 覆盖诊断 + 未处置线索候补池），响应标注派生口径，候补不改等级（红线二）。
8. **治理配置写范式**：[snapshot_config.py](file:///d:/dev/inves_duckdb/server/app/snapshot_config.py) "临时副本过 load_pack 全量校验 → 原子写 → ops 审计"（rule_workshop/model_designer/knowledge/views 四先例）。ETL 字段（clean/on_cast_error/null_policy/key 去重/composite_props）本就声明在 bindings.json；data_elements.json 虽非八段，load_pack 装载期校验属性→数据元引用，临时副本校验有效。
9. **状态机现状**：任务态 PENDING/RUNNING/SUCCEEDED/FAILED（无 CANCELLED，需追加终态）；案件态 侦查中→已封存 合法迁移已在状态机；`handle_archive` 压实任务已实现（前置已封存）；平台审计 `record_platform_event` 已有。
10. **测试基建**：run_tests.py 现 121 组；MCP 69 项不受影响（core 仅可选参数）。新增 3 组后 124 组。无新第三方依赖。

---

## 二、端点契约明细（字段级，对齐 FE-P 验收）

> 路径均省略 `/api/v1` 前缀；🔒=写权限（role 取会话）、⚡=异步任务（SSE 进度，决策 11 字段）。未 BUILD 案件：读端点返回 `{available:false, ...空结构}` 不 500。

### W-P-001 `GET /cases/{cid}/disposal/board` — 09 五泳道看板（高，读）

- **响应 data**：`{available, stale_days_threshold:14, counts:{total, by_status:{待查,查证中,已排除,已固证,已立案}}, columns:{"待查":[card...],...}}`
- **card 字段**：`clue_id, title, jian_types[], level, priority_score, subjects[]（主体摘要：名称+类型）, status, note, operator, updated_at, stay_days, overdue(bool)`
- **口径**：状态真值取 state.sqlite（缺省待查）；stay_days = 今 - updated_at；overdue = stay_days > stale_days_threshold（阈值读快照 thresholds.json `disposal.stale_days`，缺省 14）；卡片迁移状态走**已有** `POST /clues/{clue_id}/actions`（不新增写端点）；内间线索秩级过滤复用 clues_view（正兵及以下不见内间卡）。
- **红线**：降级态（health 横幅 warn）前端禁用立案按钮由 DEGRADED_WRITE_REJECTED 驱动，board 本身只读。

### W-P-002 `POST /cases/{cid}/sources/{uid}/analyze` — 02 列分析（高，同步只读计算）

- **请求**：path uid=upload_id；body 可空或 `{target_table?}`（预选声明表）。
- **响应 data**：`{upload_id, filename, format, row_count, sha256, columns:[{name, inferred_type, null_rate, distinct, samples[]}], declared_tables:[{name, title, required_columns[], optional_columns[]}], suggestion:{target_table, confidence:0~1, matches:[{source_col, target_prop, match_type:exact|normalized|fuzzy|none, confidence}], missing_required:[], low_confidence:[]}, element_hints:[{col, element_id, element_name, confidence, evidence:{match_values[]}}]}`
- **口径**：列画像复用 `ingest_io.profile_columns`；match 规则 = 精确 > 去下划线/大小写归一 > 包含/近义模糊；element_hints 复用 `core/de_recommend.recommend_for_table`（读快照 data_elements）；低置信高亮前端做，后端给 confidence 与 low_confidence 清单。
- **红线**：纯只读——不产生任务、不写 mapping、不改状态；uid 不存在/跨租户 404；格式不支持 400（VALIDATION，与"列映射失败"区分错误码文案）。

### W-P-003 `PUT /cases/{cid}/sources/{uid}` — 02 映射保存（高，写 meta）

- **请求 body**：`{target_table, mapping:{属性名: 源列名}, notes?}`（mapping 经 `validate_mapping` 校验：目标表/属性须在声明集、源列须在上传件列集）。
- **响应 data**：回显保存后的 source 记录（含 mapping_json、status）。
- **闭环**：`POST /sources/{uid}/import` 未显式带 mapping 时回落读 `source.mapping_json`（[worker/tasks.py](file:///d:/dev/inves_duckdb/server/app/worker/tasks.py) handle_import 薄改）；已保存映射不影响幂等指纹。
- **红线**：仅 staged/queued 态可改（imported/importing 409 CONFLICT）；非法目标表/列 400；operator 取会话。

### W-P-004 `GET /cases/{cid}/graph?node_limit=300&edge_limit=500` — 10 图谱（高，读）

- **响应 data**：`{available, truncated:{nodes:bool, edges:bool, dropped_edges:n}, nodes:[{id:"<obj>:<pk>", label, type, type_title, jian:[]}], edges:[{source, target, label, type}]}`
- **口径**：节点=entity 型非 runtime 对象（person/account/company/employee/phone/device/osint/tipoff），每类按度数（边计数）排序取 top；边=仅 endpoints 双侧带 ref 的链接（transfers/calls_to/owns/holds/used/contact_of/registered_at...），`SELECT <from.col>,<to.col> FROM lnk_<name> LIMIT`，label 取链接 title，端点不在节点集则补 id-only 节点（计入上限）；无 ref 的链接（time_window）跳过计数。
- **红线**：表缺失/未 BUILD 返回空图 `{available:false}` 不 500；节点 id 与边端点同域（`<obj>:<pk>`）。

### W-P-005 `GET /cases/{cid}/hypotheses` — 11 庙算（中，读）

- **响应 data**：`{available, derived:true（派生口径标注）, coverage:{declared:[{dimension, covered, total, missing[]}], empirical:[...]}, heatmap:{jians:["因","内","反","死","生"], levels:["观察","线索","确认"], counts:[[...5×3...]]}, candidates:[{clue_id, title, jian_types[], level, priority_score, reason}], restricted:[{clue_id, reason:"内间线索·权限不足"}]}`
- **口径**：heatmap 由产物线索按 jian_types×level 聚合；coverage 透传 run_diagnostic 双路 miaosuan 覆盖；candidates=未处置线索按 priority 取 top 20；restricted=秩级过滤掉的内间线索（灰显原因，不泄露内容）。
- **红线二**：候补条目不改变交叉等级——candidates 纯展示，响应不含等级升格字段；内间秩级过滤同 clues。

### W-P-006 `GET /cases/{cid}/profiles?focus=&anchor_date=` — 07 数据画像（中，读）

- **响应 data**：`OntologyProfiler.profile_all()` 直出（pack/params/health/note + L1L2 属性画像数组 `{obj,prop,declared_type,connectable,materialized_*,value_profile:{row_count,non_null,null_rate,distinct,samples},variants:{rule,alias},composite?,score_deductions[]}` + L3 指标 + L4 变体明细 + L5 扣分明细 + compliance），server 外层包 `{available, data_version}`。
- **口径**：快照感知 gateway+profiler（base_dir=案件快照）；clean_stats 从 run_diagnostic 重建传入；focus 缺省=obj_person top N + 高优线索主体，query 可覆盖；anchor 缺省 null（窗口指标 not_evaluated）。
- **红线**：空态="尚未接入数据源"（无物化对象时 available:false + 空结构，不报错）；样本值经敏感遮蔽（Profiler 内置 _mask）。

### W-P-007 接入建议 — 08（中，写 state + 任务）

- `GET /cases/{cid}/de-recommendations`：`{items:[{rid, status:"待核实"|"采纳"|"驳回", upload_id, created_at, decided_by, decided_at, note, recommendations:[{col, element_id, element_name, code_table?, confidence, evidence:{match_values[], sample?}}]}], total}`
- `POST /cases/{cid}/de-recommendations` 🔒⚡：body `{upload_id}` → 202 `{task_id}`（TASK_DE_RECO：worker 读 staged 文件采样列值 → 快照 data_elements + `de_recommend.recommend_for_table` → 落 state.de_recommendation，状态恒"待核实"）
- `POST /cases/{cid}/de-recommendations/{rid}/decide` 🔒⚡：body `{decision:"adopt"|"reject", note?}` → 202 `{task_id}`（TASK_DE_DECIDE：状态更新 + 审计链追加）
- **红线**：推荐永不自动生效——decide 只记录，**不改 bindings/不自动映射**（同 core/de_recommend 纪律）；红条「待核实草案，非生效声明」为前端固定文案；rid 不存在/跨租户 404；重复 POST 同 upload_id 幂等（Idempotency-Key，已有任务进行中 409 回跳既有任务）。

### W-P-008 质量检查 — 21（中，写 state + 任务）

- `POST /cases/{cid}/quality-checks` 🔒⚡：body 可空 → 202 `{task_id}`（TASK_QUALITY：只读连接 + 快照 gateway，跑 compliance/data_freshness（确定性）+ sensitive_scan/unit_scan（启发式），health=None，汇总落 state.quality_check，**不产版本文件**）
- `GET /cases/{cid}/quality-checks/latest`：`{available:false|true, check_id, created_at, created_by, data_version, summary:{total, passed, warnings, violations}, checks:[{category:"compliance"|"freshness"|"sensitive"|"unit", mode:"deterministic"|"heuristic", rule_id, obj, prop?, severity:"block"|"warn"|"suggest"|"ok", count, message, samples_masked[]}]}`
- **红线六（启发式只告警）**：sensitive/unit 结果 severity 封顶 `suggest`，永不 block、数据完整保留；**红线七（确定性不静默）**：compliance 违规/空值策略按 block|warn 给出，策略为 null 时 NULL 计数可见（freshness/null 计数项）；latest 只反映最近一次（带 created_at + 当时 data_version）。

### W-P-009 `GET /cases/{cid}/quarantine?reason=&page=&page_size=` — 22 隔离区（中，读）

- **响应 data**：`{items:[{object, property, rule?, src_column?, reason:"cast_error"|"null_value"|"dedup"|"other", source_table, sample_masked, name_value?, quarantined_at}], total, stats:{cast_error, null_value, dedup, other}, empty_message}`
- **口径**：cast_error 行级取 build_quarantine 表（全行留存）；null_value/dedup 行级取 run_diagnostic 中 clean_drop_rate/dedup_key_conflict 的 detail.sample_masked（前 3/属性，聚合分页）；reason 过滤四类；零隔离时 `empty_message="本次装载无数据被丢弃"`（红线五，不留空）。
- **红线**：sample_masked 只出脱敏样本，原始值不回传；page_size≤50。

### W-P-010 `GET /cases/{cid}/clean-trace?object=&page=&page_size=` — 22 清洗留痕（中，读）

- **响应 data**：`{items:[{object, property, rules:[], rows_before, rows_after, dropped_rows, rate, samples_masked[], source:"build"|"rescan", created_at}], total}`
- **口径**：按 (object,property) 聚合 run_diagnostic 的 clean_drop_rate + dedup_key_conflict + source_value_cast_failed/source_column_missing（other 类留痕）；纯读不新增存储。

### W-P-011 `GET/PUT /cases/{cid}/data-elements` — 18 数据元（中，快照配置）

- GET：案件快照 data_elements.json 直出（分类树前端构建；含代码表预览字段）。
- PUT 🔒：body=全量 data_elements JSON → snapshot_config 范式（临时副本 load_pack 校验 → 原子写 → ops 审计，clearance≥2）。
- **红线**：loader 校验失败（ID 重复/属性引用不存在等）400 且不落盘；页面标注"平台级上下文切案件不清空"为前端行为，后端按案件快照隔离（FE-P-018 备注的案件快照级口径）。

### W-P-012 ETL 管道 — 19/20（中，快照配置）

- `GET /cases/{cid}/etl-pipeline`：`{sources:[{source_table, object, clean:[], on_cast_error:"reject"|"quarantine"|"null", null_policy:{属性:allow|reject|quarantine}, dedup_key:[], dedup_on_conflict:"keep_latest"|"keep_first"|"fail", composite_props:[]}]}`（从快照 bindings.json 派生）
- `PUT /cases/{cid}/etl-pipeline` 🔒：body=同上 sources 结构 → 回写 bindings.json（snapshot_config 校验范式，clearance≥2）。
- `POST /cases/{cid}/etl-pipeline/validate`：body `{target_table, mapping:{属性:源列}}` → `{valid:bool, conflicts:[{type:"one_to_one"|"missing_column"|"unknown_prop", target_a?, target_b?, source_col?, target_prop?, message}], paths:[{key:"A_split_source_sql", label:"上游 source_sql 拆分（复合列定义）"}, {key:"B_degrade_column", label:"整列降级为低可信度"}]}`
- **红线四（FE-T-016）**：1:1 冲突给冲突双方列名 + 两路出路；**响应不包含 force/ignore 继续字段**（前端不得渲染"忽略并继续"）；split 类操作不提供为可执行动作（路径 A 仅指引复合列定义）；validate 不写盘；缺列现状前端可另取 governance/missing-columns。

### W-P-013 系统设置 — 25（低，平台 meta）

- `GET /settings/queue`：`{max_workers, poll_interval_ms, backoff?（不实现，固定缺省）}`；`PUT /settings/queue` 🔒(admin) body 同上子集。
- `GET /settings/resources`：`{storage_root, max_rows_default, query_timeout_ms}`；`PUT` 同（白名单键）。
- `GET /settings/health`（登录可读）：`{meta_ok, queue:{pending, running, succeeded_today?}, worker:{pool_alive, max_workers, poll_interval_ms}, versions:{frontend?（前端注入）, backend, ontology_default}}`。
- **红线**：写仅 admin（非 admin 403 + authz 平台事件，前端只读态明示原因）；写操作 **reason 必填** + record_platform_event（FE-T-012）；键名白名单，白名单外 400；**审计合规开关不暴露可写键**（前端 🔒 锁定，FE-T-011 红线常量不可配置覆盖）。

### W-P-014 `POST /tasks/{tid}/cancel` — 24 任务取消（低，写 meta）

- body `{reason?}` → `{task_id, status:"CANCELLED"}`。
- **口径**：仅 PENDING 可取消（条件 UPDATE，返回 false → 409）；RUNNING/终态 409（不协作中断，D-M6-6）；权限=任务创建人或 admin；取消后同 Idempotency-Key 重发产生新任务行。

### W-P-015 门户 — 23b（低）

- `GET /cases/{cid}/summary`：`{case:{...案件 dto}, data_version, todos:{clues_pending, review_pending, anomalies_pending}, recent_tasks:[...5], health:{chain_ok, degraded, diagnostics_warn:n}}`（聚合 cases/dashboard/disposal/review，待办计数前端不再自行拼 dashboard）。
- `POST /cases/{cid}/archive` 🔒(clearance≥2) body `{reason}`：案件状态迁移 侦查中→已封存（状态机非法迁移 409）+ 入队 TASK_ARCHIVE 版本压实（复用 handle_archive）→ 202 `{task_id}`。

### W-P-017 配置中心 — 25（低，平台 meta，依核对报告 §3）

- `GET/PUT /settings/policies-thresholds` 🔒(admin)：五间升格/降级阈值（平台默认值），PUT 带**红线下限区间校验**（阈值不得低于 L0 红线常量，如单源不升格）+ reason + 平台审计；响应标注"案件生效值以案件快照 thresholds.json 为准，平台值用于新建案件默认"（D-M6-10）。
- `GET /settings/snapshots` 🔒(admin)：跨案件本体快照列表（case_pack_snapshots 聚合：案件名/pack/snapshot_version/created_at）。
- `GET/PUT /settings/features` 🔒(admin)：功能开关白名单键（如 ui 默认密度等非红线项）；**llm_enabled 等红线/安全键不在白名单**（llm 开关仍只由案件包 llm_policy 声明）；PUT reason + 审计。
- 不做：logging、配置导入导出、queue 退避字段（报告标"可选"，后续批次）。

---

## 三、文件与模块

### core 修改（仅可选参数追加，缺省零行为变化）

| 路径 | 改动 |
|---|---|
| [core/gateway.py](file:///d:/dev/inves_duckdb/core/gateway.py) | `OntologyReadGateway.__init__` 加 `base_dir=None`：透传 `load_pack(pack, base_dir=)`、`PolicyEngine(pack, path=快照/policies.json if base_dir)`、`all_views(pack, base_dir=)` |
| [core/ontology_profile.py](file:///d:/dev/inves_duckdb/core/ontology_profile.py) | `OntologyProfiler.__init__`/`connectable_props()`/`EntityLinkExplorer.__init__` 加 `base_dir`，透传 load_pack/load_profiler_settings |
| [core/compliance.py](file:///d:/dev/inves_duckdb/core/compliance.py) | `scan()`/`resolve_checks()` 加 `base_dir` 透传（load_pack/load_data_elements/load_compliance_checks） |
| [core/sensitive_scan.py](file:///d:/dev/inves_duckdb/core/sensitive_scan.py) | `scan()` 加 `base_dir`（透传 load_pack；PolicyEngine 由 server 以快照 policies 构造后经既有 `policy=` 参数传入） |
| [core/data_freshness.py](file:///d:/dev/inves_duckdb/core/data_freshness.py) / [core/unit_scan.py](file:///d:/dev/inves_duckdb/core/unit_scan.py) | `scan()` 加 `base_dir` 透传 |

### server 新增

| 路径 | 内容 |
|---|---|
| `server/app/disposal_board.py` | W-P-001 组装器：五泳道卡片/stay_days/overdue/内间过滤 |
| `server/app/graph_view.py` | W-P-004 组装器：声明确认 + information_schema 探活 + 节点边采样截断 |
| `server/app/hypotheses_view.py` | W-P-005：双覆盖透传 + 五间热力 + 候补池 + 受限灰显 |
| `server/app/profiles_view.py` | W-P-006：快照 gateway/profiler 构造、clean_stats 重建、focus 派生 |
| `server/app/quality.py` | W-P-008 worker：四 scan 编排（mode 标注 deterministic/heuristic）→ state.quality_check |
| `server/app/recommend.py` | W-P-007 worker：handle_de_reco / handle_de_decide（StateSink + 审计链） |
| `server/app/routers/disposal.py` | W-P-001 |
| `server/app/routers/graph.py` | W-P-004 |
| `server/app/routers/research.py` | W-P-005/006/007 |
| `server/app/routers/quality.py` | W-P-008/009/010 |
| `server/app/routers/etl.py` | W-P-011/012 |
| `server/app/routers/settings.py` | W-P-013/017（/settings 前缀，admin 写门禁） |
| `tests/test_m6_board.py` | W-P-001~004（组 m6board） |
| `tests/test_m6_insight.py` | W-P-005~010（组 m6insight） |
| `tests/test_m6_govern.py` | W-P-011~015、017（组 m6govern） |

### server 修改

| 路径 | 改动 |
|---|---|
| [server/app/routers/ingest.py](file:///d:/dev/inves_duckdb/server/app/routers/ingest.py) | W-P-002 analyze、W-P-003 PUT 映射 |
| [server/app/worker/tasks.py](file:///d:/dev/inves_duckdb/server/app/worker/tasks.py) | TASK_QUALITY/TASK_DE_RECO/TASK_DE_DECIDE 注册（快速通道，不产版本）；handle_import 回落 source.mapping_json |
| [server/app/routers/tasks.py](file:///d:/dev/inves_duckdb/server/app/routers/tasks.py) | W-P-014 cancel |
| [server/app/routers/cases.py](file:///d:/dev/inves_duckdb/server/app/routers/cases.py) | W-P-015 summary/archive |
| [server/app/store/state_store.py](file:///d:/dev/inves_duckdb/server/app/store/state_store.py) | 新表 `quality_check`(check_id/case_id/created_at/created_by/summary_json)、`de_recommendation`(rid/case_id/upload_id/created_at/status/payload_json/decided_by/decided_at/note) + 读写方法；CREATE TABLE IF NOT EXISTS 幂等 |
| [server/app/meta/repo_sqlite.py](file:///d:/dev/inves_duckdb/server/app/meta/repo_sqlite.py) | `settings_kv`(k 主键/value_json/updated_by/updated_at/reason) + get/set/list；`cancel_task(tid)` 条件 UPDATE；`save_source_mapping(cid,uid,mapping)`；`list_pack_snapshots()` |
| [server/app/meta/repo.py](file:///d:/dev/inves_duckdb/server/app/meta/repo.py) | ABC 同步签名 |
| [server/app/meta/models.py](file:///d:/dev/inves_duckdb/server/app/meta/models.py) | `TASK_CANCELLED="CANCELLED"` 入 TASK_STATUSES（终态，claim/list_leaseable 自然排除） |
| [server/app/main.py](file:///d:/dev/inves_duckdb/server/app/main.py) | 挂载六个新 router |
| [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) | 注册 m6board/m6insight/m6govern |

**明确不改**：core 默认行为（base_dir 全可选）；ActionExecutor/actions.json（W-P-007 裁决是 state 业务面写，同 REVIEW 范式）；不新写 DuckDB 版本文件（quality/de-reco 落 state）；不接 LadybugDB；建议不自动生效；RUNNING 不中断；W-P-016 不做；前端页面不做。

---

## 四、实施步骤（依赖顺序）

- **阶段 A**：core 六文件 base_dir 可选参数 → 先跑 121 组回归确认零影响。
- **阶段 B（高）**：disposal_board + router；graph_view + router；ingest analyze/PUT + import 回落；test_m6_board + 注册。
- **阶段 C（中读面）**：hypotheses_view；profiles_view（含 clean_stats 重建）；quality router 的 quarantine/clean-trace 读端点；test_m6_insight 读面部分。
- **阶段 D（中写面）**：state_store 两表；tasks 三类型；worker/recommend.py、worker/quality.py；research router（de-reco 三端点）；quality POST/latest；补齐 test_m6_insight。
- **阶段 E（治理配置）**：routers/etl.py（data-elements/etl-pipeline/validate，snapshot_config 范式）。
- **阶段 F（低 + 收口）**：meta settings_kv/cancel_task/save_source_mapping/list_pack_snapshots + TASK_CANCELLED；routers/settings.py；tasks cancel；cases summary/archive；test_m6_govern + 注册；main.py 挂载。
- **收口**：WSL 全量 `run_tests.py`（124 组）+ MCP 69 项；回填本文第八章；前端清单附录 F 标 ✅；backend_api.md 补 M6 段；git 分拣提交（排除 data/ 产物）。

---

## 五、决策点

| 编号 | 决策点 | 推荐 |
|---|---|---|
| D-M6-1 | 快照感知：core 加可选 base_dir / chdir / server 自读 | **可选 base_dir**（M3/M4 模式；chdir 并发不安全；自读重复装载逻辑） |
| D-M6-2 | quality-checks：入队任务落 state / 同步 / 写 run_diagnostic 产版本 | **入队 QUALITY 落 state.quality_check**（秒级扫描合任务体系+SSE；不产版本文件） |
| D-M6-3 | de-recommendation 存储：state 新表 / meta / 产物 JSON | **state.sqlite 新表 + DE_RECO/DE_DECIDE 快速通道**（per-case 业务面，同 review_decision） |
| D-M6-4 | graph 范围：实体非 runtime 节点+双侧 ref 边 / 含 event / 全量 | **前者**，默认节点 300/边 500（query 可调），无 ref 边跳过计数，按度数采样 |
| D-M6-5 | clean-trace/quarantine 数据源：run_diagnostic+build_quarantine 纯读 / 新建表 | **纯读聚合**（cast 行级在 build_quarantine；null/dedup 样本在 clean_drop_rate/dedup_key_conflict detail，零新存储） |
| D-M6-6 | 任务取消：仅 PENDING / RUNNING 协作中断 | **仅 PENDING→CANCELLED**（Worker 无中断点，强杀有半成品风险；RUNNING 409） |
| D-M6-7 | etl-pipeline 落点：回写 bindings.json / 新配置文件 | **回写 bindings**（字段本就在 bindings 声明，编译器唯一事实源；新文件双源） |
| D-M6-8 | settings 存储：meta.settings_kv / 配置文件 | **settings_kv 单表**（运行时可改+审计；键白名单 + reason 必填 + admin） |
| D-M6-9 | W-P-016 functions 独立端点 | **不实施**（FE-P-003 已定 function_catalog 内嵌；编号保留闭合 backlog） |
| D-M6-10 | policies-thresholds 平台阈值与案件快照关系 | **平台值=新建案件默认（settings_kv），案件生效值=快照 thresholds.json**；不回灌既有案件（避免静默改变进行中案件的升格口径）；PUT 红线下限校验 |
| D-M6-11 | W-P-017 范围 | policies-thresholds + snapshots(只读列表) + features 三端点；logging/导入导出/退避字段后续 |
| D-M6-12 | quality 检查 mode 标注 | compliance/freshness=deterministic（block/warn，红线七不静默）；sensitive/unit=heuristic（封顶 suggest，红线六不阻断） |

---

## 六、验收标准映射（W-P → 测试组与红线用例）

| W | 核心 AC | 组 |
|---|---|---|
| 001 | 五泳道与真值一致；stay_days/overdue；未 BUILD available:false；正兵不见内间卡；卡片 actions 走既有端点 | m6board |
| 002 | 画像/声明表/建议映射/element_hints；confidence 与 low_confidence；纯只读无任务；坏文件 400 与 404 区分 | m6board |
| 003 | 草稿落 mapping_json；import 回落草稿闭环；imported 态 PUT 409；非法映射 400 | m6board |
| 004 | 节点边字段齐备且 id 同域；截断 truncated 计数；空图降级不 500 | m6board |
| 005 | 热力计数与产物一致；双覆盖透传；候补不含升格字段（红线二）；restricted 灰显不泄内容 | m6insight |
| 006 | 六层结构；clean_stats 重建后 rows_before/after 可见；空态 available:false；样本脱敏 | m6insight |
| 007 | 状态恒"待核实"；decide 落 state+审计且不改 bindings；幂等 409 回跳；跨租户 404 | m6insight |
| 008 | 202+SSE；latest 四类 checks；启发式封顶 suggest（红线六）；确定性违规 block/warn 且 NULL 计数可见（红线七）；不产版本文件 | m6insight |
| 009 | 四类 stats；cast 行级 + null/dedup 样本；零隔离 empty_message 非空（红线五）；无原始值回传 | m6insight |
| 010 | 按对象/属性聚合计数与诊断一致；纯读 | m6insight |
| 011 | GET 快照直出；PUT 非法声明 400 不落盘；clearance<2 拒；ops 审计可查 | m6govern |
| 012 | GET 派生视图；PUT 过 loader；validate 检出 1:1 冲突给双方+两出路且**无 force 字段**（红线四 FE-T-016）；validate 不写盘 | m6govern |
| 013 | 非 admin 写 403+authz 事件；reason 必填；白名单外键 400；health 队列计数真实 | m6govern |
| 014 | PENDING→CANCELLED 不被认领；RUNNING/终态 409；非创建人非 admin 403 | m6govern |
| 015 | summary 待办计数与看板/裁决一致；archive 状态迁移+压实任务；非法迁移 409 | m6govern |
| 017 | 阈值红线下限拒写；features 白名单外拒（llm 键不可写，红线 FE-T-011）；snapshots 仅 admin；写操作 reason+审计 | m6govern |

**通用红线（每组必测）**：无 token 401；跨租户 404；信封/列表契约；page_size≤50；新 router 文件过 storeback 门禁（无直连 duckdb）；快照感知显式断言（改案件快照 thresholds 后 profiles/quality 读到快照值，模板包不变）。

---

## 七、测试与验证

- 新增 3 组（m6board/m6insight/m6govern），预计 ~60 例；全量 121+3=**124 组**全绿（WSL `/root/.venvs/inves/bin/python run_tests.py`）。
- MCP `scripts.mcp_client_test` 69 项保持绿（core 仅可选参数，MCP 不传 base_dir 路径零变化）。
- 阶段 A 收口先跑全量回归验证 base_dir 缺省零影响。
- 红线用例显式断言：FE-T-016（validate 响应无 force/ignore 字段）、红线五（零隔离 empty_message 非空）、红线六（heuristic checks 无 severity=block）、红线七（确定性违规有 block/warn 记录）、FE-T-011（settings 写 llm/红线键 400）。

---

## 八、遗留与风险

- **庙算无持久沙盘**：hypotheses 为派生视图（响应 derived:true）；候补池编辑/排序持久化需新 state 表与 Action 设计，后续批次。
- **画像 focus/anchor 缺省口径**：focus 取 top N 主体、anchor 缺省 null（窗口指标 not_evaluated）；前端可经 query 传参，后续可做用户级关注主体持久化。
- **graph 规模**：300/500 为 MVP 默认，按度数采样；完整图分析走 LadybugDB 专线后续。
- **quality 时点性**：latest 反映最近一次扫描（带 created_at/data_version）；规则/数据元变更后需手动重扫，不自动触发。
- **settings 热生效**：queue 并发等落库后 Worker 进程需重启/轮询重载；MVP 仅持久化+回显，health 如实回报运行中池参数。
- **平台阈值不回灌**（D-M6-10）：既有案件快照 thresholds 不受平台 PUT 影响，避免静默改变进行中案件升格口径；新建案件继承默认值的接线在案件创建流程（后续批次随 pack 初始化读取 settings_kv）。
- **quarantine null/dedup 样本上限**：诊断 detail 仅留前 3 条脱敏样本/属性（cast_error 为全行留存）；行级完整追溯需走审计/原始数据复核流程。
- **W-P-016 闭合说明**：functions 独立端点不实施，若后续 OpenAPI 契约需要可再补（读快照 functions.json，工作量 <0.5 人日）。

---

## 九、实施记录

> 回填于 M6 全量交付后（W-P-001~015、017 已实施；W-P-016 函数目录端点
> 决策不实施——rules 响应已内嵌函数元数据，编号保留以闭合 backlog）。

### 9.1 阶段 A：core base_dir 透传（零行为变化）

七文件追加 keyword-only `base_dir: Path | None = None`（缺省回落既有解析，
全部只读扫描）：`core/gateway.py`、`core/functions.py`、
`core/ontology_profile.py`（含 `build_table_profile`）、`core/compliance.py`、
`core/sensitive_scan.py`、`core/data_freshness.py`、`core/unit_scan.py`。

附带 core 修复（web 只读路径暴露的既有缺陷）：
`core/ontology_version.py` `_ensure_meta_table` 先查
`information_schema.tables`，表已存在直接 return——**read-only DuckDB 连接
拒绝一切 CREATE**（即使 IF NOT EXISTS），此前只读 gateway 首读即报错。

### 9.2 阶段 B（W-P-001~004，m6board 组 16 例）

- 新增 `server/app/disposal_board.py`（五泳道/停留天数/超期）、
  `graph_view.py`（节点 id 域 `"<obj>:<pk>"`，node_limit=300/edge_limit=500）、
  `source_analyze.py`（置信度 exact 1.0 / normalized 0.85 / fuzzy 0.6）；
- 新增 `routers/disposal.py`、`routers/graph.py`；
  `routers/ingest.py` 加 analyze + PUT 映射草稿；
  `store/repo_sqlite.py` 加 `save_source_mapping`；
  `worker/ingest.py` handle_import 草稿回落。

### 9.3 阶段 C/D（W-P-005~010，m6insight 组 15 例）

- 新增组装器 `hypotheses_view.py`（heatmap 5×3/coverage/candidates top20/
  内间 restricted，candidates **无升格字段**）、`profiles_view.py`
  （gateway `allow_stale=True`；clean_stats 从 run_diagnostic 重建）、
  `quality_view.py`（list_quarantine 四类 reason 聚合；零隔离必给
  empty_message；list_clean_trace 按 object.property 聚合）；
- 新增 `routers/research.py`（hypotheses/profiles/de-recommendations）、
  `routers/quality.py`（quarantine/clean-trace/quality-checks）；
- 写面落 per-case `state.sqlite`（不产 DuckDB 版本）：state_store 加
  `quality_check` / `de_recommendation` 两表及配套方法；
  `worker/tasks.py` 注册 QUALITY_CHECK / DE_RECOMMEND / DE_RECO_DECIDE；
  新增 `worker/quality.py`（四扫描汇总；**heuristic severity 恒 suggest**）、
  `worker/recommend.py`（数据元推荐幂等；采纳/驳回只记 state 不改 bindings）。

### 9.4 阶段 E（W-P-011/012，m6govern 组覆盖）

- 新增 `server/app/routers/etl.py`：
  - `GET/PUT /cases/{cid}/data-elements`（GET 快照直出；PUT 🔒 clearance≥2，
    全量 dict，临时副本写**新内容**过 `load_pack` 校验后 os.replace 原子落盘，
    校验失败 400 不落盘）；
  - `GET /cases/{cid}/etl-pipeline`（bindings 派生 sources：清洗/CAST/空值/
    去重策略）；`PUT` 按 object 匹配回写 bindings.json（同校验范式）；
  - `POST /cases/{cid}/etl-pipeline/validate`（不写盘；冲突三类 one_to_one /
    unknown_prop / missing_column；`paths` 固定 A_split_source_sql /
    B_degrade_column 两路，**响应无 force/ignore/continue**）。
- 注意：未使用 `snapshot_config.save_config_json/validate_snapshot`
  （二者校验旧副本，属既有 bug）；etl.py 内联 `_write_validated()` 正确范式。

### 9.5 阶段 F（W-P-013/014/015/017，m6govern 组覆盖）

- `meta/models.py`：加 `TASK_CANCELLED="CANCELLED"`（TASK_STATUSES 五元组；
  list_leaseable/claim 天然只取 PENDING）；
- `meta/repo_sqlite.py`：DDL 加 `settings_kv` 表 + `get_setting/set_setting/
  list_settings`；`cancel_task()`（BEGIN IMMEDIATE 条件 UPDATE
  `WHERE status='PENDING'`，rowcount≠1 返回 None）；
  `list_pack_snapshots()`（join `cases c ON c.id=s.case_id`——cases 主键列名
  是 id）；
- `routers/tasks.py`：`POST /tasks/{tid}/cancel`（跨租户 404；权限=创建人或
  admin，否则 403 + authz_failure 平台事件；非 PENDING 409；条件 UPDATE 竞态
  亦 409）；
- 新增 `routers/settings.py`（平台级，**不带 case_id**，main.py 直挂
  API_PREFIX）：
  - `GET/PUT /settings/queue`（白名单 max_workers 1-16 / poll_interval_ms≥10）、
    `GET/PUT /settings/resources`（max_rows_default / query_timeout_ms 白名单；
    storage_root 只读出透传、不可写）、`GET /settings/health`（登录可读：
    meta_ok/队列计数/worker 配置/versions）；
  - `GET/PUT /settings/policies-thresholds`（白名单 cross_level_min_sources≥1、
    cross_level_min_clues≥2、stale_days 1-3650；响应明示平台值仅用于新案默认、
    生效以案件快照为准）、`GET /settings/snapshots`（list_pack_snapshots）、
    `GET/PUT /settings/features`（白名单 ui_density；**llm_enabled 等红线键
    不在白名单 → 400**）；
  - 写纪律：仅 admin（audit.py 范式 + authz_failure 事件）、**reason 必填**
    （空 400）、键白名单与区间/enum 校验（越界 400）、写后
    record_platform_event("settings_change")；
- `routers/cases.py`：`GET /cases/{cid}/summary`（case dto + data_version +
  todos{clues_pending 产物线索待查数 / review_pending / anomalies_pending} +
  recent_tasks 前 5 + health{chain_ok / degraded / diagnostics_warn}；未 BUILD
  不 500）；`POST /cases/{cid}/archive`（🔒 clearance≥2；已封存显式 409——
  状态机对同态迁移放行，路由层拦截重复归档；非法迁移 409；成功后入队
  ARCHIVE 版本压实任务，202）；
- 新增 `server/app/portal_view.py` 纯读聚合（state 开启在 routers 层——
  state_store 引用门禁：仅 store/routers/worker 可 import）；
- `main.py`：注册 settings_router；CORS allow_methods 补 PUT。

### 9.6 测试与回归

- 新增 `tests/test_m6_govern.py` 12 例（含 admin 用户夹具、线索/诊断种子、
  任务 claim 竞态）；连同 m6board 16 例、m6insight 15 例，M6 共新增
  **43 例**；`run_tests.py` GROUPS 注册 m6board/m6insight/m6govern；
- 全量 `run_tests.py` 全绿（含 statestore state_store 引用门禁、storeback
  duckdb 直连门禁——新 router/组装器零直连）；
- `scripts.mcp_client_test` **69/69** 通过；
- 测试范式要点：手工语义库必须建 `meta_ontology_state` 表（真实 BUILD 必写，
  只读 gateway 跳过 DDL 的前提）；只读 gateway 一律 `allow_stale=True`。

### 9.7 未实施 / 保留

- W-P-016 函数目录独立端点：不实施（rules 响应已内嵌函数元数据），编号保留；
- 配置中心核对报告中的 `/settings/logging`、配置导入导出：未纳入 M6 编号，
  保留为后续 backlog。

