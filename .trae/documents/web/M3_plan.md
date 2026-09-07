# M3 研判主流程里程碑实施计划（REQ-W-010/012/014/019/020/025 + D1 写路径接线）

**日期** 2026-09-07 ｜ **状态** ✅ 已实施（2026-09-08，见第八节实施记录；决策点 D-M3-1~5 均按推荐项落地）
**关联** [backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)（批次表：M3=批 5；2.4 业务面写路径定论；第三部分端点契约 B/C/E/G 组）、[M2_plan.md](file:///d:/dev/inves_duckdb/.trae/documents/web/M2_plan.md)（已实施，2026-09-07 全绿；附录 A D1 迁移方案）、[req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)（W-010/012/014/019/020/025 AC 原文）
**范围** 批 5 六项：W-019 线索列表与详情、W-020 线索处置与状态机（含 **D1 state.sqlite 写路径接线**，M2 挂起项）、W-010 数据源注册与列映射向导、W-012 导入任务幂等、W-014 规则工坊、W-025 LLM 边界守卫。
**不含** W-011 可选属性缺列降级（批 6 数据治理页，M4）、W-021 实体裁决人审队列（M4）、W-022 异常通道端点（M4）、W-013 模型设计器/W-015 权限遮蔽/W-016 知识包（M4 批 6）、跨案件 ATTACH（M5）、案件包导入导出（M5）、Postgres 实现（方言预留不实现）、前端页面（本计划仅后端端点与任务）。

**最小影响红线（继承 M1/M2 首要原则）**：M3 对 server 外存量代码的改动目标为 **core/ 4 文件纯追加式分派**（audit.py / action_executor.py / lineage.py / disposal.py，见修改表），全部以**可选构造参数注入、缺省即现状**的形态落地——CLI/MCP/run_all 路径零感知、零行为变化。**禁止借 sink 抽象重构既有写路径**；既有函数签名与语义不改；`core/store.py` 门面化经核对无 Web 消费者（Web 全程走 server StoreFactory），**本批显式裁剪、不做**。

---

## 一、仓库研究结论（M2 交付现状与 M3 依赖锚点）

1. **处置写汇聚点单一**：[action_executor.py](file:///d:/dev/inves_duckdb/core/action_executor.py) 是写操作唯一入口，四步校验（角色/必填参数/状态机/权限上下文）全在 `_validate()`（L115-143），落库在 `_apply()`（L146-164）。写点共四处：① 审计链经 `_chain()` 惰性构造 `AuditChain(self.store.conn)`（L82-94）；② 两阶段提交 `action_request` 表 DDL/INSERT（L34、L186+）；③ `clue.set_status/set_filed` 经 audit_chain 回调；④ `_create_decision()` 写 `obj_decision`/`lnk_decision_for` 语义表（L352、L358）。**校验逻辑一行不用动**，只换"写往哪"。
2. **处置状态持久化在 core/lineage.py**：`save_statuses()`/`load_statuses()`（[lineage.py:242,276](file:///d:/dev/inves_duckdb/core/lineage.py#L242)）操作 `clue_disposal_status` 表，SQL 为标准 `CREATE TABLE IF NOT EXISTS` / `INSERT ... ON CONFLICT` 风格 + `?` 占位符——**sqlite3 与 DuckDB 方言兼容**，传 sqlite 连接即可能零改动运行（实测验证为准）；`DisposalBoard.persist()/restore()`（[disposal.py:115-125](file:///d:/dev/inves_duckdb/core/disposal.py#L115)）是薄封装。
3. **state.sqlite 骨架已就位**（M2 交付）：[state_store.py](file:///d:/dev/inves_duckdb/server/app/store/state_store.py) 四表 `audit_chain`（14 列与 DuckDB 版逐列同构）/ `action_request` / `clue_disposal_status` / `review_decision` 已建；`import_from_duckdb()` 幂等迁移演练可用（root_hash 断言已验）。**M3 补写面方法并接线，不改 schema 结构**（若接线中发现列缺口，ALTER ADD COLUMN 追加，不重建）。
4. **线索读面是双源**：MCP `_build_board()`（[mcp_server.py:383-406](file:///d:/dev/inves_duckdb/scripts/mcp_server.py#L383)）证实——线索主体（title/jian_types/source_rows/merged_from/优先级/suppressed）来自**报告产物 JSON**（output 快照），处置状态以 DuckDB `clue_disposal_status` 为真值源回灌；语义层 `obj_clue`（[objects.json:106](file:///d:/dev/inves_duckdb/ontology/default/objects.json#L106)）是处置快照表。Web 读面须组装三源：**报告产物（属性/溯源/合并/抑制）+ obj_clue 语义字段（Gateway 策略过滤）+ state.sqlite 状态（接线后真值）**。
5. **obj_clue 状态陈旧问题**（M2 实施记录偏差 1 的延续）：Web 架构下处置写 state.sqlite、不重建版本，版本文件内 `obj_clue.status` 停在 BUILD 时旧值。**裁决：server 线索读面以 state.sqlite `clue_disposal_status` 为状态真值覆盖**，core 不动；M2 加的 `core/disposal.py:status_counts()` 读 DuckDB 表，接线后仪表盘待办计数改读 state（server 侧 [dashboard.py](file:///d:/dev/inves_duckdb/server/app/dashboard.py) 组装器改数据源，core 函数保留给 CLI/MCP 旧路径，不删不改）。
6. **业务写通道定论**（backend_api.md 2.4）：处置类短频写**仍然入队**（API 不直接写 state.sqlite），走案件 FIFO"快速通道"——秒级完成、**不产 DuckDB 版本**，复用任务表幂等/审计/operator 绑定；Worker 是唯一写者。现有任务类型 BUILD/PING/ARCHIVE（[tasks.py:34-36](file:///d:/dev/inves_duckdb/server/app/worker/tasks.py#L34)），M3 新增 **DISPOSE**（处置快速通道）、**IMPORT**（数据导入）、**RESCAN**（规则调整增量重算）三类。
7. **规则写入目标是案件快照**：建案时 13 声明文件锁定至 `cases/{cid}/ontology_snapshot/`（W-005）。规则工坊编辑/启停/调参写**案件快照内 rules.json**（不是共享 ontology/，多租户隔离），保存后过 loader 强校验（AC-1），调阈值可触发 RESCAN（AC-6）。LLM 辅助端点（W-025）**只返回 rule_text 候选、不落盘**，含 function/params 一律拦截（AC-1），产物默认"待核实"（AC-2）。
8. **LLM 闸门现状**：`AccessContext.network` 已含 `"web"`（[access.py:34](file:///d:/dev/inves_duckdb/core/access.py#L34)，D5 已落地）；`require_llm_allowed` isolated 拒网（L118-121）；pack 级 `llm_enabled` 默认 false、默认 shadow 模式。M3 规则工坊 LLM 端点在 llm_enabled=false 时返回明确 503（功能未启用），启用时输出校验在 server 侧先行、core llm_policy 闸门兜底。
9. **数据接入链路**：冷层为 `cases/{cid}/cold/*.parquet`（版本文件视图以相对路径 `read_parquet('data/...')` 挂载——M1 教训：案件库内路径需随案件目录调整，IMPORT 产出 parquet 后 BUILD 的 bindings source_sql 指向案件 cold 目录）；上传支持 CSV/Excel/Parquet/JSON/SQLite 五格式（AC-1），指纹=文件名+内容哈希+行数，幂等判定在**任务创建阶段**完成（W-012 AC-4，不耗 Worker）。
10. **测试基建**：run_tests.py GROUPS 现 103 组（M2 后）；MCP 69 项须保持绿。M3 新增 5 组后 108 组。无新第三方依赖（pandas/openpyxl/pyarrow 已在 venv）。

---

## 二、文件与模块

### 新增

| 路径 | 内容 |
|---|---|
| `server/app/store/state_sink.py` | **D1 接线核心**：`StateSink`——StateStore 的写适配层，对 core 暴露窄协议：`audit_append(event_dict)`（复用 core 签名算法，写 audit_chain）、`upsert_action_request(row)`、`save_statuses(clue_rows)`/`load_statuses(clue_ids)`、`create_decision(decision_dict)`（写 review_decision 表，替代 obj_decision/lnk_decision 语义表落库）、`timeline()/chain_integrity()/root_hash()`（读面，签名逐条可验）。所有写在 sqlite 单库内短事务；WAL busy_timeout 已配 |
| `server/app/worker/dispose.py` | W-020 DISPOSE 任务处理器：案件 FIFO 快速通道——构造 AccessContext（operator/role/clearance/network="web" 取自任务行，入队时从会话快照）→ StateSink → `DisposalBoard`（线索对象从报告产物恢复 + state 状态回灌）→ `ActionExecutor.execute()`（四步校验全在 core）→ 落 state 三表 → 任务成功。**不产 DuckDB 版本、不触碰 vN.duckdb**；失败回 FAILED 可重试（状态机迁移幂等，action_request 幂等键去重） |
| `server/app/worker/ingest.py` | W-010/012 IMPORT 任务处理器：解析五格式 → 落 `cases/{cid}/cold/<source>.parquet` → 列画像/类型推断产物 →（向导映射确认后的 bindings 案件覆盖写入快照目录，过 loader 校验）→ 触发 BUILD 任务（链式入队）。失败不留部分数据（AC-5：先写临时目录，成功后原子 rename 进 cold/） |
| `server/app/routers/clues.py` | W-019/020：`GET /api/cases/{cid}/clues`（level/dimension/jian/subject/status 筛选 + 分页，AC-1/2/6 只读不重扫）、`GET /api/cases/{cid}/clues/{clue_id}`（详情：五间横条、source_rows 溯源、merged_from，AC-3/5）、`GET /api/cases/{cid}/clues/suppressed`（suppressed_log，AC-4）、`POST /api/cases/{cid}/clues/{clue_id}/actions` 🔒（五动作入队 DISPOSE，Idempotency-Key 支持；operator 强制取会话；file 的 legal_basis 必填校验在 API 侧先拦一道，core 校验兜底） |
| `server/app/clues_view.py` | 线索读面组装器（server 侧，无 SQL）：报告产物 + Gateway 查 obj_clue（PolicyEngine 遮蔽/内间线索过滤，沿用 MCP REQ-011 逻辑：正兵以下不见内间）+ state 状态覆盖；产出列表/详情信封 |
| `server/app/routers/ingest.py` | W-010/012：`POST /api/cases/{cid}/sources/uploads`（multipart 上传 → upload_id + 内容哈希）、`POST /api/cases/{cid}/import` 🔒⚡（指纹幂等拦截在入队前，AC-1/2/3/4；202+task_id）、`GET /api/cases/{cid}/sources`（已注册数据源列表）、`POST /api/cases/{cid}/sources/mappings`（向导映射确认 → 生成 bindings 案件覆盖 → loader 校验 AC-2/3/4 → 入队 BUILD，AC-5）、列清洗规则挂载（strip/exclude_org_tokens，AC-6） |
| `server/app/routers/rule_workshop.py` | W-014/025：`GET /api/packs/{pack}/rules`（rule_text+function+params 双轨+启用状态）、`PUT /api/packs/{pack}/rules/{rid}` 🔒（编辑/启停/调参；function 白名单校验 AC-2；string 参数必须 enum 白名单 AC-3；绑定关系记录可审计 AC-4；单独启停 AC-5；调阈值可触发 RESCAN 入队 AC-6）、`POST /api/packs/{pack}/rules/draft` 🔒（LLM 辅助：**响应只许含 rule_text**，含 function/params 拦截告警 AC-1；默认"待核实"AC-2；脱敏在调用前 AC-3；注入特征拒绝 AC-4；永不自动生效 AC-5；llm_enabled=false → 503） |
| `server/app/worker/rescan.py` | W-014 AC-6 RESCAN 任务：规则调整后增量重算（MVP 语义：复用 BUILD 编排重跑检测 + 产新版本文件；"增量"在 MVP 体现为任务级跳过未变更数据源，规则引擎本身全量重跑——与现有 build_ontology 幂等语义一致，不引入新 core 增量机制） |
| `tests/test_state_sink.py` | D1 接线：StateSink 写 audit_chain 签名逐条可验（复用 core 算法）；DisposalBoard+ActionExecutor 在 sqlite 后端跑通五动作；file 终态 human 红线/legal_basis 红线/占位 operator 红线在 sqlite 后端同等生效；**sink=None 缺省路径逐字节不变**（锚断言） |
| `tests/test_dispose_api.py` | W-020 AC-1~7：五动作入队→Worker 执行→落 state 持久链（AC-6，非内存日志）；正兵/AI 触发 file 拒绝（AC-2）；缺 legal_basis 拒绝（AC-3）；非"已固证"迁移拒绝（AC-4）；system/ai/agent:* 占位名拒绝（AC-5）；处置后列表状态一致（AC-7）；Idempotency-Key 重复提交不产生两条链事件 |
| `tests/test_clues_read.py` | W-019 AC-1~6：五维筛选；优先级分数与排序依据；详情 source_rows 可回溯；suppressed 列表不删除仅移出；merged_from 保留被合并 id；分页筛选不触发任务（无新 task 行断言）；内间线索低权限不可见（REQ-011 延续） |
| `tests/test_ingest_api.py` | W-010/012：五格式上传列识别+类型推断（AC-1）；映射生成 bindings 过 loader（AC-2/4）；映射未声明属性硬失败（AC-3）；提交后 BUILD 任务可见进度（AC-5）；空格列名/单位后缀挂清洗规则（AC-6）；同文件重复提交幂等拦截（AC-1）；内容同名不同放行（AC-2）；同名不同内容不拦截（AC-3）；幂等判定在任务创建阶段（AC-4，无 Worker 任务行）；失败重试无部分数据（AC-5） |
| `tests/test_rule_workshop.py` | W-014/025：规则保存过 loader（AC-1）；function 非白名单拒绝（AC-2）；string 无 enum 硬失败（AC-3）；绑定关系可审计（AC-4）；单条启停不影响他条（AC-5）；调阈值触发 RESCAN 任务（AC-6）；LLM draft 含 function/params 拦截（AC-1）；待核实标记（AC-2）；脱敏前置（AC-3）；注入拒绝（AC-4）；draft 永不自动生效（AC-5，rules.json 无变更断言）；llm_enabled=false 返回 503 |

### 修改（core/ 4 文件纯追加 + server 既有文件）

| 路径 | 改动 | 性质 |
|---|---|---|
| [core/audit.py](file:///d:/dev/inves_duckdb/core/audit.py) | `AuditChain.__init__` 增可选 `backend="duckdb"` 参数（M2 附录 A 预定方案）：backend="sqlite" 时 DDL/append/verify/timeline 的 SQL 分派到 sqlite 方言薄封装（签名计算 `_compute_signature` 抽为两后端共用的模块级函数，若已是模块级则直接复用）；**缺省 "duckdb" 路径一行不动**，auditinteg 组原样全绿 | 追加式分派 |
| [core/action_executor.py](file:///d:/dev/inves_duckdb/core/action_executor.py) | `__init__` 增可选 `sink=None`：`_chain()` 在有 sink 时构造 sqlite 后端 AuditChain；`action_request` 读写经 sink 分派（两阶段提交路径）；`_create_decision()` 在有 sink 时调 `sink.create_decision()`（落 state.review_decision）而非 obj_decision/lnk_decision 语义表；**`_validate()` 零改动**；sink=None 时全部走现状 | 追加式分派 |
| [core/lineage.py](file:///d:/dev/inves_duckdb/core/lineage.py) | `save_statuses()/load_statuses()` 经实测若 sqlite 直接兼容则**零改动**（server 传 sqlite 连接）；若有方言缝隙（如 UPSERT 语法差异），追加最小方言分支，不重构 | 可能零改动 |
| [core/disposal.py](file:///d:/dev/inves_duckdb/core/disposal.py) | `DisposalBoard.__init__` 透传 `sink` 给 ActionExecutor；`persist()/restore()` 在有 sink 时走 sink 状态面；其余方法零改动 | 薄透传 |
| [server/app/store/state_store.py](file:///d:/dev/inves_duckdb/server/app/store/state_store.py) | M2 骨架补写面方法（append/upsert/decision/timeline/integrity），由 state_sink.py 编排；schema 不动（缺列才 ALTER ADD） | server 内 |
| [server/app/worker/tasks.py](file:///d:/dev/inves_duckdb/server/app/worker/tasks.py) | 注册 DISPOSE/IMPORT/RESCAN 三个处理器（TASK_DISPOSE/TASK_INGEST/TASK_RESCAN 常量 + 分派表）；enqueue_task 支持快速通道标记（不产版本） | server 内 |
| [server/app/meta/models.py](file:///d:/dev/inves_duckdb/server/app/meta/models.py) / repo_sqlite.py | tasks 表 params 快照扩展（DISPOSE 任务行存 operator/role/clearance/action/params 入队时会话快照——Worker 不信任请求体 operator）；source 注册表（数据源指纹/行数/状态，幂等判定用）；规则编辑审计记 platform_audit 或 ops_events | server 内 |
| [server/app/dashboard.py](file:///d:/dev/inves_duckdb/server/app/dashboard.py) | 待办计数数据源切换：disposal 计数改读 state.sqlite（M2 偏差 1 收口）；review 待办仍 available=false（M4） | server 内 |
| [server/app/main.py](file:///d:/dev/inves_duckdb/server/app/main.py) | 挂载 clues/ingest/rule_workshop 三个新 router | server 内 |
| [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) | 注册 5 个新测试组：statesink/disposeapi/cluesread/ingestapi/ruleworkshop | 纯追加 |

**明确不改**（最小影响决策）：

- **`core/store.py` 不做门面化**：M2 计划曾挂"推 M3"，经核对 Web 路径全程使用 server 自有 StoreFactory，core Store 仅 CLI/MCP/run_all 使用——无 Web 消费者，门面化是无收益重构，本批显式裁剪（记入实施记录）。
- **不引入双写**：M2 附录 A 设想的"DuckDB+sqlite 双写过渡"在版本化架构下不成立（API 物理上写不进不可变版本文件）。真实形态是 **backend 分派**（Web→state.sqlite、CLI/MCP→DuckDB）；旧案件历史链用 M2 已交付的 `import_from_duckdb()` 一次性迁移（处置端点首次访问案件时惰性迁移或建案后运维命令，幂等）。
- **11 处无参 `Store()`、3 处脚本直连、MCP 13 工具**：全部原样。
- **`_validate()` 四步校验逻辑**：角色/必填参数/状态机/权限上下文一行不动——Web 与 CLI 共用同一校验，红线语义单点维护。
- **core 规则引擎/LLM 策略**：rules.json 写入是 server 文件操作（loader 只读不改）；LLM 输出拦截在 server 侧，core llm_policy 仅兜底，不新增 core LLM 功能。
- **W-011 缺列降级、W-021 人审裁决端点**：M4 批 6，本批不做（ReviewQueue 候选仍无持久化读面，仪表盘 review 卡片保持 available=false）。

---

## 三、实施步骤（依赖顺序）

### 阶段 A：D1 写路径接线（W-020 地基，最高优先）

1. `core/audit.py` backend 分派：抽签名共用函数 + sqlite 后端薄封装；补单测进 statesink 组（DuckDB/sqlite 两后端同输入同签名同 root_hash 断言）。
2. `server/app/store/state_sink.py`：实现窄协议五方法；StateStore 补写面。
3. `core/action_executor.py` + `core/disposal.py` sink 注入点；`core/lineage.py` sqlite 兼容性实测（兼容则零改动，留测试断言）。
4. `tests/test_state_sink.py`：五动作 sqlite 后端跑通 + 三条红线（file human/legal_basis/占位名）+ sink=None 缺省锚断言。
5. 回归锚：action/writeback/reconcile/auditinteg/disposal/reviewloop/proposal/reviewwrite/audit 九组原样全绿后才进入下一阶段。

### 阶段 B：DISPOSE 快速通道与处置端点（W-020）

6. meta tasks 扩展会话快照字段；`worker/dispose.py` 处理器（不产版本、FIFO、失败可重试、幂等键去重）；tasks.py 注册 TASK_DISPOSE。
7. `routers/clues.py` 处置端点：`POST .../clues/{id}/actions`——鉴权 → 会话快照构造任务行 → 入队 → 202+task_id；API 侧前置校验（action 名合法、file 须 legal_basis、operator 取会话忽略请求体）。
8. 处置后状态一致性（AC-7）：前端经任务 SSE/轮询确认 SUCCEEDED 后刷新；线索读面状态走 state（阶段 C 交付后闭合）。
9. `tests/test_dispose_api.py` 全 AC。

### 阶段 C：线索读面（W-019）

10. 报告产物持久化锚点：Worker BUILD/SCAN 成功后将线索报告 JSON 落 `cases/{cid}/artifacts/clues_v{N}.json`（随版本、不可变；server 侧任务编排，core run_all 产物写盘逻辑不变——Worker 调既有产出路径后复制/登记）。
11. `server/app/clues_view.py` 组装器：artifacts 报告 + Gateway obj_clue（策略遮蔽）+ state 状态覆盖；内间线索过滤沿用 REQ-011 秩级判定。
12. `routers/clues.py` 三个 GET 端点（列表筛选分页/详情/suppressed）；只读不触发任务（AC-6 断言无新 task 行）。
13. `dashboard.py` 待办计数切 state 数据源（M2 偏差收口）。
14. `tests/test_clues_read.py` 全 AC。

### 阶段 D：规则工坊与 LLM 守卫（W-014/025）

15. `routers/rule_workshop.py`：规则列表读案件快照 rules.json；PUT 编辑——function 白名单（functions.json 目录）、string 参数 enum 强制、启停、绑定审计；写盘前 loader 强校验（不合法不落盘）；调阈值入队 RESCAN。
16. `worker/rescan.py`：RESCAN 任务（MVP=规则变更后重跑检测产新版本，复用 BUILD 编排）。
17. LLM draft 端点：llm_enabled 开关检查（false→503）；输出解析层白名单（只许 rule_text 键）；脱敏前置；注入特征拒绝；产物"待核实"、不落盘、永不自动生效。
18. `tests/test_rule_workshop.py` 全 AC。

### 阶段 E：数据接入向导与导入幂等（W-010/012）

19. 上传端点：multipart 收五格式 → 暂存 `cases/{cid}/uploads/` → 算指纹（文件名+sha256+行数）→ 返回 upload_id + 列画像（列名/推断类型/样本值）。
20. 导入端点：指纹幂等判定在入队前（source 注册表查重：同指纹拦截 AC-1、同内容异名放行 AC-2、同名异内容不拦 AC-3、判定阶段无任务行 AC-4）。
21. `worker/ingest.py`：解析 → 写 cold 临时目录 → 原子 rename（失败无残留 AC-5）；映射确认产物 bindings 案件覆盖写快照目录 → loader 校验 → 链式入队 BUILD。
22. 清洗规则挂载（strip/exclude_org_tokens，AC-6）：映射确认时按列声明，随 bindings 落盘。
23. `tests/test_ingest_api.py` 全 AC。

### 阶段 F：端到端冒烟与回归

24. TestClient 全链路：建案 → 上传 CSV → 映射确认 → BUILD → 线索列表/详情 → 正兵处置 verify/exclude（落 state 链）→ file 红线三拒 → human+legal_basis file 成功 → 审计时间线可见处置事件 → 仪表盘待办计数随 state 变化 → 规则调参 → RESCAN 任务可见。
25. MCP 69 项回归（CLI/MCP 路径零感知验证）。

### 阶段 G：全量回归与收口

26. `run_tests.py` 全量 108 组（含 e2e）全绿；MCP 69/69；backend_api.md 里程碑状态 M3 标已实施；M3_plan.md 回填实施记录（测试数、core 触点 diff 核对、偏差清单）。

---

## 四、决策点（待评审，推荐项已标注）

| # | 决策点 | 选项 | 推荐 |
|---|---|---|---|
| D-M3-1 | core 写后端分派形态 | A. core 定义 sink 窄协议、server 实现注入（ActionExecutor/AuditChain/DisposalBoard 加可选 sink/backend 参数）；B. server 侧完全重写一套写逻辑不经 core ActionExecutor | **A**——校验/状态机/签名算法单点维护在 core，server 只换落库目标；B 会导致红线双份实现、必然漂移 |
| D-M3-2 | 线索报告产物持久化 | A. Worker BUILD/SCAN 后落 `cases/{cid}/artifacts/clues_v{N}.json`（随版本不可变，server 读当前版本）；B. 写入 state.sqlite；C. 直接查 obj_clue 语义表（字段不全：source_rows/merged_from/suppressed 不在语义表） | **A**——产物是分析结果随版本不可变；state 只放"人对结论的操作"（D1 边界）；C 字段缺口无法满足 W-019 AC-3/4/5 |
| D-M3-3 | 处置写同步/异步 | A. 入队 DISPOSE 快速通道（backend_api.md 2.4 定论：业务写仍入队，FIFO 秒级，不产版本）；B. API 进程直写 state.sqlite（WAL 支持，但 Worker 唯一写者原则被突破） | **A**——遵循 2.4 明文定论与"Worker 唯一数据面写者"架构约束；AC-7 实时一致性由任务秒级完成 + SSE/轮询闭合 |
| D-M3-4 | 规则写入目标 | A. 写案件快照 `cases/{cid}/ontology_snapshot/rules.json`（案件级隔离，RESCAN 读快照）；B. 写共享 ontology/pack | **A**——建案锁快照原则（W-005）的延续；多租户/多案件规则演进互不干扰；平台级 pack 模板升级走 M5 案件包机制 |
| D-M3-5 | 旧案件审计链迁移时机 | A. 处置端点首次访问时惰性迁移（import_from_duckdb 幂等）；B. 运维命令批量迁移；C. 不迁移（旧链只读留 DuckDB，新写走 state，时间线双源拼接） | **C 备选 A**——时间线读面双源拼接（state 为主 + DuckDB 历史链只读追加展示）零迁移成本且不丢历史；若评审倾向单源，则 A 惰性迁移（已验证幂等）。**推荐 C**：迁移是一次性数据运动，双源拼接是永久读面小复杂度，两者权衡取运维零操作 |

---

## 五、验证

- **全量回归**：`wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"`——108 组全绿（含 e2e）。
- **单组**：`python run_tests.py --only statesink` / `disposeapi` / `cluesread` / `ingestapi` / `ruleworkshop`（--only 单组，逐个执行）。
- **MCP 回归**：`/root/.venvs/inves/bin/python -m scripts.mcp_client_test`——69/69（证明 CLI/MCP 缺省路径零行为变化）。
- **core 零行为锚**：action/writeback/reconcile/auditinteg/disposal/reviewloop/proposal/reviewwrite/audit 九组原样全绿。
- **grep 门禁**：core/ 内不出现 `state_store`/`state_sink` import（sink 是鸭子类型协议，core 不依赖 server）；server/ 外不出现 sqlite3 直连（除 M2 既有 state_store 定位在 server 内）；routers 内无业务 SQL。

---

## 六、风险与处置

| 风险 | 处置 |
|---|---|
| sink 抽象被顺手做成写路径重构（本批最大风险） | 硬约束：sink/backend 全部可选参数、缺省逐字节现状；阶段 A 第 5 步九组锚测试不过不进下一阶段；code review 对照 git diff 逐行核对 `_validate()` 零改动 |
| sqlite 与 DuckDB SQL 方言缝隙（UPSERT/DDL/类型） | lineage.py 先行实测；audit.py 后端分派封装在薄适配层；statesink 组以"同输入同签名同 root_hash"断言两后端等价 |
| 处置入队后 AC-7"实时一致"体验 | DISPOSE 快速通道秒级 + SSE 任务进度（复用 M1 SSE）；前端任务 SUCCEEDED 后刷新；任务行含会话快照，Worker 不依赖请求体 |
| 线索三源拼接状态不一致 | 状态真值唯一=state.sqlite（D-M3-2/研究结论 5）；obj_clue 状态列在 Web 读面忽略；测试断言三源同 clue_id 状态以 state 为准 |
| 向导生成 bindings 污染案件快照 | 写盘前必经 loader 强校验（AC-4），不合法不落盘；bindings 覆盖文件保留版本（rules/bindings 编辑留审计，可回滚） |
| LLM 端点范围蔓延（接真实模型/出境） | M3 只交付守卫与拦截层；llm_enabled=false 默认 503；脱敏/注入/白名单三道在 server 侧；不引入模型 SDK 依赖 |
| IMPORT 大文件阻塞 Worker | 导入走 Worker 并发槽位（全局上限 N）；解析进度回报 task.progress；cold 写入原子 rename，失败零残留 |
| RESCAN"增量"名不副实 | MVP 明确语义=任务级跳过未变更数据源 + 规则引擎幂等全量重跑；不新增 core 增量机制（防范围蔓延）；UI 文案与计划如实标注 |
| 仪表盘待办计数切换漏改 | dashboard.py disposal 计数改读 state 为阶段 C 显式步骤；M2 dashboard 组测试同步改断言源 |

---

## 七、非目标（显式裁剪，防范围蔓延）

- 前端 26 页面任何实现（本计划纯后端；前端访问层另由 api_access_layer_plan.md 线推进）。
- W-011 缺列降级、W-021 人审裁决、W-022 异常通道、W-013/015/016 配置界面（M4）。
- core/store.py 门面化（无消费者，不做）。
- 双写过渡期（backend 分派替代，D-M3-5）。
- Postgres 方言实现、跨案件 ATTACH、案件包导入导出（M5）。
- 真实 LLM 接入与模型调用（仅守卫层）。

---

## 八、实施记录（2026-09-08 回填）

**状态**：已实施。五个决策点均按推荐项落地（D-M3-1 sink 注入 / D-M3-2 线索产物随版本不可变 / D-M3-3 DISPOSE 入队快速通道 / D-M3-4 规则写案件快照 / D-M3-5 时间线双源拼接）。

### 新增测试（6 组，69 例，全绿）

| 组 | 覆盖 | 例数 |
|---|---|---|
| statesink | D1 写路径接线：StateSink 适配 core 五动作 sqlite 后端、签名链与 DuckDB 等价 | 11 |
| disposeapi | W-020：DISPOSE 五动作 202/幂等/跨租户/NO_VERSION/CLUE_NOT_FOUND/ACTION_FORBIDDEN/ACTION_REJECTED/file 前置红线 | 13 |
| cluesread | W-019：列表筛选排序分页/详情/suppressed 富集、内间秩级过滤、state 状态覆盖、dashboard 切 state | 12 |
| ruleworkshop | W-014/025：loader 强校验不落盘、结构字段不可改、单条启停、调参触发 RESCAN、LLM 503/注入拦截/输出键白名单/永不落盘 | 20 |
| ingestapi | W-010/012：五格式上传+列画像、指纹幂等三例、映射校验、clean 落 bindings、冷层 CTAS→BUILD→obj_transaction、解析失败零残留 | 12 |
| m3chain | 阶段 F 步骤 24 全链路：上传→导入→BUILD 产物→线索读面→verify→file 三拒→confirm→file 成功→审计双源→仪表盘 state 计数→调参 RESCAN→v2 产物 | 1 |

run_tests.py GROUPS：103（M2 后）→ **111 组**（含 M3 六新组；另有 REQ-D 系列组在 M2 后持续并入，以注册表为准）。

### core/ 实际触点（7 文件，全部为可选参数注入/缺省即现状）

| 文件 | 改动 | 性质 |
|---|---|---|
| core/audit.py | AuditChain `backend="duckdb"/"sqlite"` + `ontology_version` 可选参数；readonly 同步 | 计划内（阶段 A，纯追加） |
| core/action_executor.py | `sink=None` 写后端分派 | 计划内（阶段 A，纯追加） |
| core/disposal.py | DisposalBoard `sink/access` 透传 | 计划内（阶段 A，纯追加） |
| core/ontology.py | RuleSpec 增 `enabled: bool = True` | **计划外新增**（阶段 D，W-014 AC-5 单条启停必需） |
| core/ontology_loader.py | `_load_rules` 读 enabled 标志 | 同上（缺省 True，旧包零变化） |
| core/rules.py | run_rules 跳过停用规则（不执行/不产 finding/不记零命中）；增可选 `base_dir`（案件快照规则装载） | enabled 为阶段 D；base_dir 为阶段 F D-M3-2 检测读快照规则必需 |
| core/functions.py | FunctionExecutor 增可选 `base_dir`（随 run_rules 同传快照基目录） | **计划外新增**（阶段 F，同上） |

> 偏差说明：计划第二节预估"core/ 4 文件纯追加"（实际 lineage.py 零改动）。阶段 D 的规则启停要求规则声明带 enabled 位（类型层 3 文件最小追加）；阶段 F 收口 D-M3-2 时发现 BUILD 后检测编排（worker/detect.py）必须能装载**案件快照**规则（否则工坊调参/启停对 RESCAN 不生效），故 run_rules/FunctionExecutor 各加一个可选 base_dir（None=共享 ontology/，CLI/MCP 路径零变化）。7 文件均为追加式、缺省行为不变。

### server/ 新增文件（11）

- store/state_sink.py（StateSink：core sink 窄协议的 sqlite 实现）
- clues_artifact.py（artifacts/clues_v{N}.json 原子写/读/最新版本探测）
- clues_view.py（列表/详情/suppressed 三源组装：产物 + state 覆盖 + 秩级过滤）
- ingest_io.py（五格式嗅探/指纹/解析/列画像/冷层 parquet 原子写）
- worker/dispose.py、worker/ingest.py、worker/rescan.py、**worker/detect.py**（BUILD 后检测编排：run_rules 快照 base_dir → skills 适配转 LineageClue → 血缘去重/优先级 → 落线索产物；检测失败=BUILD 失败、版本指针不前进）
- routers/clues.py、routers/ingest.py、routers/rule_workshop.py

### server/ 既有文件改动

- worker/tasks.py：TASK_DISPOSE/RESCAN/IMPORT 常量与处理器注册；handle_build 增冷层 CTAS 挂载钩子 + 检测产物步骤；
- meta/repo_sqlite.py：case_sources 注册表（staged/queued/imported 状态机 + 指纹判重，排除自身行）；
- store/state_store.py：status_map() 读面；
- dashboard.py / routers/dashboard.py：处置卡 state_counts 注入 + source 标记；
- routers/audit.py：**D-M3-5 双源时间线**（state 链为主 + 版本文件历史链只读追加，每条带 chain_source；verify 以 state 活链自检）；
- main.py：四新路由挂载。

### 其他偏差与记录

- **新依赖**：python-multipart 0.0.32（FastAPI UploadFile multipart 上传必需；阿里云镜像安装）；sqlalchemy 2.0.52（pandas 以 sqlite URI 读取上传的 .db 文件，使 ingest_io.py 不出现 `sqlite3.connect(`/`duckdb.connect(` 字面量，通过 store/ 外禁直连门禁）。
- **门禁调整**：tests/test_state_store.py 的 state_store 引用门禁从"仅限 store/+tests/"放宽为"store/routers/worker+tests，core/ 永不依赖"（M3 把 state 接进 Web 读写面，核心不变量仍是依赖方向 server→core）。
- **D-M3-2 收口位置**：计划步骤 10 的"Worker BUILD 后落线索报告"在阶段 F 落地（worker/detect.py），RESCAN 复用 handle_build 自动重产产物；线索读面/处置均消费 artifacts/clues_v{N}.json。
- **指纹幂等语义**：同指纹源已 queued/imported 即 409（判定在入队前，拒绝不产任务行，W-012 AC-4）；staged 重传不拦（任务幂等键去重）。
- **SERVICE_VERSION 保持 "M1"**（test_api_base 断言不动）。
- core/store.py 门面化按计划显式裁剪（无 Web 消费者）。
- 回归结果：见本节末尾（全量 run_tests.py + MCP 69 项）。

