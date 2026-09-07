# M1 地基里程碑实施计划（REQ-W-001~007 + API 骨架）

**日期** 2026-09-07 ｜ **状态** 已实施（2026-09-08，Windows 原生环境；见文末「实施记录」）
**关联** [backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)（决策 D1~D10 已锁定）、[api_access_layer_plan.md](api_access_layer_plan.md)（访问层 D5~D10、S1~S6）、[req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)、[adr.md](file:///d:/dev/inves_duckdb/.trae/documents/web/adr.md)
**范围** W-001/002 服务端开库收口（**grep 门禁限定 `server/` 树**，本地 CLI/MCP/脚本存量路径一律不改）、W-003 StoreBackend 抽象（**在 `server/app/store/` 纯新建**；`core/store.py` 门面化推迟 M2，见阶段 A 与风险表）、W-004 元数据层（SQLite-WAL）、W-005 案件生命周期与 pack 快照锁定、W-006 任务队列与 Worker 池、W-007 版本化文件与原子切换；外加 FastAPI 骨架与认证/案件/任务三类最小端点（M1 可端到端演示：建案→入队 BUILD→Worker 构建新版本→原子切换→只读可见）。**另纳入 API 访问层契约补丁**（S1~S6：`/api/v1` 前缀、CORS 白名单中间件默认关、`/api/health` 免认证探针、`DEGRADED_WRITE_REJECTED` 错误码、SSE 全程 Bearer 无 Cookie 旁路、幂等重试不耗键；D5：内核 `network="web"` 枚举。依据 api_access_layer_plan.md，均为几行级）。
**不含** state.sqlite 审计链迁移（M2）、仪表盘/线索/处置等业务端点（M2/M3）、跨案件 ATTACH（M5）、Postgres 实现（Repository 方言预留，不实现）。

---

## 一、仓库研究结论

1. **Store 现状**（[core/store.py](file:///d:/dev/inves_duckdb/core/store.py)）：具体类，`Store(root="data", db_path="investigation.duckdb")`，`.conn` 惰性 `duckdb.connect()`（读写），`query()` 含 REQ-003 直查拦截与 unsafe 审计通道，`execute()` 为写路径，另有 L1 dict 热层与 cold_scan。
2. **无参 `Store()` 实测分布 11 处——M1 一律不改**（逐处核实，服务端链路均不触达）：
   - 本地单案件入口（默认 `data/investigation.duckdb` 语义无歧义，永不碰 `cases/`）：[run_all.py:59](file:///d:/dev/inves_duckdb/run_all.py#L59)、[run_demo.py:31](file:///d:/dev/inves_duckdb/run_demo.py#L31)、[run_with_invoker.py:41](file:///d:/dev/inves_duckdb/run_with_invoker.py#L41)；skills [yong_jian.py:12](file:///d:/dev/inves_duckdb/skills/yong_jian.py#L12)、[xu_shi.py:20](file:///d:/dev/inves_duckdb/skills/xu_shi.py#L20)、[qi_zheng.py:12](file:///d:/dev/inves_duckdb/skills/qi_zheng.py#L12)；scripts [build_ontology.py:45](file:///d:/dev/inves_duckdb/scripts/build_ontology.py#L45)、[export_ladybug.py:117](file:///d:/dev/inves_duckdb/scripts/export_ladybug.py#L117)、[q2_overpass_cypher.py:31](file:///d:/dev/inves_duckdb/scripts/q2_overpass_cypher.py#L31)、[demo_profile.py:150](file:///d:/dev/inves_duckdb/scripts/demo_profile.py#L150)、[profile_table.py:178](file:///d:/dev/inves_duckdb/scripts/profile_table.py#L178)；MCP [scripts/mcp_server.py](file:///d:/dev/inves_duckdb/scripts/mcp_server.py) 8 处（348/403/449/651/780/806/900 等）
   - [core/ontology.py:314](file:///d:/dev/inves_duckdb/core/ontology.py#L314)：`_default_org_names(conn=None)` 的**兜底分支**；`build_ontology` 内两处调用（386/1114 行）均显式传 conn，Worker 路径不触发，且有 try/except 吞底
   - [core/disposal.py:140](file:///d:/dev/inves_duckdb/core/disposal.py#L140)：在 `_cmd()` **CLI 子命令入口**内；服务端将使用的 `DisposalBoard` 类本就支持 `store=` 注入（[disposal.py:35](file:///d:/dev/inves_duckdb/core/disposal.py#L35)）
   - W-001"不得隐式开库"的真实意图是约束**服务端多案件代码**：以 grep 门禁测试强制（`server/` 树内 `Store()` 命中 0），不依赖改写存量
   - 测试/benchmark 的 `Store(db_path=":memory:")` 按 W-001 AC-4 豁免。
3. **生产直连 `duckdb.connect` 3 处——M1 不改**：[scripts/init_duckdb.py:78](file:///d:/dev/inves_duckdb/scripts/init_duckdb.py#L78)、[scripts/incremental.py:95](file:///d:/dev/inves_duckdb/scripts/incremental.py#L95)、[scripts/export_dashboard.py:12](file:///d:/dev/inves_duckdb/scripts/export_dashboard.py#L12) 均为本地运维脚本，server 不调用；W-002 收口同样以 grep 门禁限定 `server/app/`（除 `store/` 模块外 `duckdb.connect` 命中 0）。v1 案件库初始化由 Worker 新代码完成，需要时 import 脚本的辅助函数（如 `_derived_cold_tables`），不改脚本本身。
4. **pack 机制可复用**：`PackManager.init_pack(from_pack=)` 已能复制声明目录（[core/pack.py:71](file:///d:/dev/inves_duckdb/core/pack.py#L71)）；`load_pack(pack, base_dir=)` 支持自定义根（[core/ontology_loader.py:69](file:///d:/dev/inves_duckdb/core/ontology_loader.py#L69)）——案件快照 = 复制到 `cases/{cid}/ontology_snapshot/` 后以 `base_dir=cases/{cid}`、pack 名 `ontology_snapshot` 装载，无需改 loader。
5. **内核入口全部以"连接/store 注入"为参数，服务端不需要 `Store` 对象**：`build_ontology(conn, pack=...)`（[ontology.py:372](file:///d:/dev/inves_duckdb/core/ontology.py#L372)）与 `OntologyReadGateway(conn, pack=, access=)` 均接收裸连接；`DisposalBoard(clues, store=...)` 支持 store 注入（M2 才用）。即 `CaseStore` 产出 `read_conn/write_conn` 后直接喂内核——**M1 服务端链路无一处构造 `Store`，这是 `core/store.py` 可以不改的直接依据**。
6. **测试基建**：[run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) GROUPS 注册表（现 92 组），新组须同步注册；MCP 69 项端到端（mcp_client_test）须保持绿。
7. **环境**：WSL venv `/root/.venvs/inves`，需新增 `fastapi`、`uvicorn`、`httpx`（TestClient 用），pip 走国内镜像。

---

## 二、文件与模块

### 新增

| 路径 | 内容 |
|---|---|
| `server/app/store/__init__.py`、`server/app/store/backend.py` | **纯新建，不动 core/**。`StoreBackend` ABC（read_conn/write_conn/query/version/case_id 五抽象方法）、`CaseStore(mode="read"/"write")`、`CrossCaseStore`（M1 仅骨架：write_conn 抛 UnsupportedOperation，ATTACH 留 M5）、`UnsupportedOperation`、`StoreFactory`（for_case/for_local；查版本指针→拼路径→开连接→登记读者引用计数）。目录与 backend_api.md 第五部分 `server/app/store/` 结构对齐；M1 单文件 `backend.py` 收口实现，M2 按需拆分为 factory.py/backend_case.py/backend_cross.py 并加 state_store.py |
| `server/__init__.py`、`server/app/__init__.py` | FastAPI 应用包 |
| `server/app/main.py` | 应用入口、中间件（**CORS 白名单 env 驱动、默认关闭** S2；会话鉴权→AccessContext，network="web"）、路由统一挂载于 **`/api/v1`** 前缀（D8）、统一响应信封与异常处理 |
| `server/app/deps.py` | 依赖注入：会话解析、AccessContext 构造、案件 membership 校验、只读连接请求作用域（yield + close） |
| `server/app/auth/` | 登录/会话/token（MVP：user 表 + 密码哈希（PBKDF2，标准库 hashlib，无新依赖）；双因子留接口不实现） |
| `server/app/meta/models.py` | 元数据表层定义：tenant/user/session/case/case_membership/task/version_pointer/platform_audit |
| `server/app/meta/repo.py` | Repository 抽象接口（方言隔离，Postgres 留空实现占位） |
| `server/app/meta/repo_sqlite.py` | SQLite-WAL 实现：建表、WAL pragma、busy_timeout、任务唯一约束、版本指针强一致读写 |
| `server/app/cases.py` | 案件生命周期：create_case（建目录+锁快照+哈希清单）、list/get、快照校验 |
| `server/app/worker/pool.py` | Worker 调度循环：案件级 FIFO + 全局并发 N、老任务优先、状态机流转 |
| `server/app/worker/tasks.py` | 任务处理器注册表：M1 实现 `BUILD`（复制/新建版本文件→write_conn→build_ontology(snapshot)→切指针）与 `PING`（队列自测）；IMPORT/SCAN/EXPORT 留注册占位 |
| `server/app/worker/retry.py` | 指数退避（base×2^n）、语义错误白名单（缺列/权限/声明非法不重试） |
| `server/app/routers/{auth,cases,tasks}.py` | 三类最小端点 + `GET /api/v1/health` 免认证探针（S5：版本/元数据层状态/Worker 最近心跳） |
| `server/app/sse.py` | SSE 进度推送（D4：500ms 轮询 task 表，Last-Event-ID 支持） |
| `server/run_worker.py` | Worker 进程入口（`python -m server.run_worker`，D3 独立进程） |
| `tests/test_store_backend.py` | W-001/002/003 验收（ABC 不可实例化、read 模式连接写失败、CrossCase 写拒绝、query 语义不变、A/B 案件互不可见）+ **grep 门禁两条硬断言**：`server/` 树内 `Store()` 命中 0；`server/app/` 除 `store/` 模块外 `duckdb.connect` 命中 0 |
| `tests/test_meta_store.py` | W-004：建表、50 并发写不丢不重、指针强一致、元数据不可用明确报错 |
| `tests/test_case_snapshot.py` | W-005：建案锁 13 文件快照、改共享本体不影响案件、案件读快照、快照篡改检出 |
| `tests/test_version_switch.py` | W-007：仓内复现 spike H1~H4（读者持 vN 时就地写失败/构建 vN+1 成功/旧句柄仍可读/失败回滚 vN 完好/指针切换原子） |
| `tests/test_task_queue.py` | W-006：同案件串行、跨案件并行≤N、幂等唯一约束、状态机、语义错误不重试、退避重试、进度单调、老任务优先 |
| `tests/test_api_base.py` | FastAPI TestClient：登录→me→建案→入队 BUILD→SSE 收到进度→任务成功→案件版本更新→无 token 401→跨租户 404/403 |

### 修改（M1 对存量代码仅 3 处，均为追加性质）

| 路径 | 改动 |
|---|---|
| [core/access.py](file:///d:/dev/inves_duckdb/core/access.py) | `NETWORKS` 增加 `"web"`（LLM 语义同 `local`，审计区分 Web 会话来源，决策 D5）；补 access/policy 测试用例。**M1 对 core/ 的唯一一行改动** |
| [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) | 注册 6 个新测试组：storeback/metastore/casesnap/versionswitch/taskqueue/apibase（纯追加） |
| [.gitignore](file:///d:/dev/inves_duckdb/.gitignore) | 新增 `cases/`、`meta/`（案件数据与元数据库为运行产物，纯追加两行） |

**明确不改**（最小化决策，2026-09-07 评审确认）：

- [core/store.py](file:///d:/dev/inves_duckdb/core/store.py) **不做门面化**：M1 服务端全程消费裸 conn（研究结论 5），无一处构造 `Store`；StoreBackend/CaseStore/StoreFactory 纯新建于 `server/app/store/`。门面化推迟 M2（处置端点需要 store 协议时），届时改动面与现在等价、不因此欠债。
- [core/__init__.py](file:///d:/dev/inves_duckdb/core/__init__.py) 不导出新符号（StoreBackend 属 server 层，不经 core 包导出）。
- 生产路径 11 处无参 `Store()` 与 3 处脚本直连 `duckdb.connect` **全部保留**（研究结论 2、3）；W-001/002 的服务端收口由 grep 门禁测试在 `server/` 树内强制，不靠改写存量。

---

## 三、实施步骤（依赖顺序）

### 阶段 A：Store 抽象与服务端收口（W-001/002/003，**零存量改写**）

1. 新建 `server/app/store/` 包（`__init__.py` 导出 + `backend.py` 单文件实现）：StoreBackend ABC + CaseStore + CrossCaseStore 骨架 + StoreFactory（for_case 读版本指针拼 `cases/{cid}/v{N}.duckdb`；for_local 兼容现有 `data/investigation.duckdb` 布局，供 M1 自测复用）。
2. **`core/store.py` 不动**：server 全程以裸 conn 调内核（`build_ontology(conn, ...)` / `OntologyReadGateway(conn, ...)`），不构造 `Store` 对象；门面化推迟 M2（见修改表"明确不改"）。
3. **生产 11 处无参 `Store()` 与 3 处脚本直连一律不改**（研究结论 2、3：本地单案件入口/CLI/不触达兜底，语义无歧义）；W-001/002 收口由门禁测试在 `server/` 树内强制。
4. 写 `tests/test_store_backend.py` 并注册 storeback 组：功能验收（ABC 不可实例化、read 模式写失败、CrossCase 写拒绝、query 语义不变、A/B 案件互不可见）+ **grep 门禁两条硬断言**（`server/` 内 `Store()` 命中 0；`server/app/` 除 `store/` 外 `duckdb.connect` 命中 0）。
5. 全量回归（92 组 + mcp 69 项）必须全绿——本阶段不碰 core/scripts/skills/MCP，预期原样通过。
6. 阶段 A 出口检查：`git diff --stat` 确认改动仅限 `server/`、`tests/`、`run_tests.py`、`.gitignore`（W-001/002/003 以"新增 + 门禁"达成，而非改写存量）。

### 阶段 B：元数据层（W-004）

7. `server/app/meta/`：models + repo 接口 + SQLite-WAL 实现（WAL pragma、busy_timeout=5000、任务表 `(case_id,task_type,idempotency_key)` UNIQUE）。
8. `tests/test_meta_store.py`：并发/强一致/故障明确报错。

### 阶段 C：案件生命周期与快照（W-005）

9. `server/app/cases.py`：create_case 建 `cases/{cid}/`（v 文件目录、cold/、ontology_snapshot/）；复制 pack 声明 13 文件；生成 `snapshot_manifest.json`（逐文件 SHA-256 + snapshot_version）；meta 落 case/version_pointer 行。
10. 快照装载验证：`load_pack("ontology_snapshot", base_dir=cases/{cid})` 可被 build_ontology 使用；快照哈希在使用时校验，篡改即报错。
11. `tests/test_case_snapshot.py`。

### 阶段 D：版本化文件与原子切换（W-007）

12. StoreFactory 接 version_pointer：read 模式开当前版本只读连接；读者登记/注销（引用计数，为 M2 延迟回收铺路，M1 只登记不回收）。
13. 版本构建流程：BUILD 任务在 `cases/{cid}/` 内生成 `v{N+1}.duckdb`（MVP 策略：**从 vN 复制后重建语义层**——保证冷层 Parquet 路径相对可解析；复制失败/构建失败删半成品），成功后在 meta 单事务内 UPDATE 指针（原子切换）；旧版本标记 pending_reclaim（不删除）。
14. `tests/test_version_switch.py` 仓内复现 H1~H4（含"指针无半成品可观测窗口"断言）。

### 阶段 E：任务队列与 Worker（W-006）

15. `worker/retry.py` + `worker/pool.py`：轮询 meta task 表，两级调度（有 PENDING 无 RUNNING 的案件 → 按 created_at 最老派发；全局并发 N，默认 N=2 可配）；状态机 PENDING→RUNNING→SUCCEEDED/FAILED/CANCELLED；progress_pct/stage 写入（阶段边界 + 500ms 节流）。
16. `worker/tasks.py`：PING（队列自测）、BUILD（接步骤 13 流程，调 build_ontology(write_conn, pack 用快照)）；处理器注册表 + 未知任务类型 fail-closed。
17. `server/run_worker.py` 入口；启动时预留孤儿扫描钩子（M1 记日志，W-009 完整实现属 M2）。
18. `tests/test_task_queue.py`（8 条 AC 覆盖）。

### 阶段 F：API 骨架与端到端

19. `server/app/main.py` + deps + auth：token 会话（secrets 模块生成，存 session 表，超时可配）；登录失败统一文案；`/api/auth/login`、`/api/auth/logout`、`/api/auth/me`。路由统一挂 `/api/v1` 前缀（D8）；CORS 中间件按 `SUNZI_CORS_ORIGINS` 白名单、默认关闭（S2）；`GET /api/v1/health` 免认证探针（S5）；SSE 端点与普通端点同一 Bearer 鉴权，**不开 Cookie/query-token 旁路**（D7，客户端用 fetch 流而非原生 EventSource）；会话构造 `AccessContext(network="web")`（D5）。
20. routers/cases：`GET /api/cases`（仅本租户）、`POST /api/cases`（建案+快照）、`GET /api/cases/{cid}`（含 data_version）。
21. routers/tasks：`POST /api/tasks`（入队，幂等键，202）、`GET /api/tasks`、`GET /api/tasks/{tid}`、`GET /api/tasks/{tid}/events`（SSE 轮询版）、`POST /api/tasks/{tid}/cancel`。
22. 统一响应信封 `{ok,data,data_version}` / `{ok,error:{code,message}}` 与错误码表（backend_api.md 4.3 子集：401/403/404/409/422/503；409 含 `IDEMPOTENCY_CONFLICT` 与 `DEGRADED_WRITE_REJECTED`（S4，降级态写/导出拒绝））。
23. `tests/test_api_base.py`：TestClient 端到端（建案→BUILD→SSE→版本切换可读）；红线断言：无 token 401、跨租户访问 404、operator 不取请求体；**增补**：SSE 不带 token 401、`/api/v1/health` 免认证 200、CORS 默认无跨域头（白名单外 Origin 拒绝）、降级写返回 409 `DEGRADED_WRITE_REJECTED`。
24. WSL venv 安装 fastapi/uvicorn/httpx（清华镜像）；跑 `python -m server.run_worker` 与 uvicorn 手工冒烟一次。

### 阶段 G：收口

25. 全量回归：`run_tests.py` 全部组（92+6=98 组）+ `mcp_client_test` 69 项全绿。
26. 更新 backend_api.md 里程碑状态（M1 已落地项）；提交 git（排除 cases/、meta/ 数据产物）。

---

## 四、依赖与注意事项

- **最小改动红线（本计划首要原则）**：M1 对存量代码仅 `core/access.py` 一行枚举 + 两处机械追加（run_tests.py 注册、.gitignore）；`core/store.py` 不门面化、11 处无参 `Store()` 与 3 处脚本直连不改。原计划"门面只读化波及 101 处调用点"的最大回归风险由此消除；服务端开库纪律改由 grep 门禁测试强制（破线即红）。
- **core/ontology.py:314 与 core/disposal.py:140 经核实不触达、不改**：314 是 `_default_org_names(conn=None)` 兜底分支，`build_ontology` 内调用均传 conn；140 在 `_cmd()` CLI 入口内，`DisposalBoard` 类已支持 `store=` 注入（M2 服务端直接注入 CaseStore 适配对象）。
- **MCP 零改动**：mcp_server 8 处无参 `Store()` 保持原样（本地 stdio 单案件模式语义正确）；69 项 mcp_client_test 作为回归照常全绿。
- **BUILD 新版本策略**：MVP 用"复制 vN → 重建语义层"而非从零建库（冷层 Parquet 相对路径、meta_unsafe_query 等附属表随之保留）；v1 由建案时 Worker 新代码初始化（可 import init_duckdb 的 `_derived_cold_tables` 等辅助函数复用建表逻辑，不改脚本本身）。
- **SQLite 并发**：WAL 单写者，N 个 Worker 并发写同一 meta 库靠 busy_timeout 串行化；任务表写事务短（状态更新），不构成瓶颈。
- **load_pack 快照复用**：快照目录命名 `ontology_snapshot` 使其符合 `base_dir/pack` 装载约定，loader 零改动。
- **跨平台**：版本文件复制/删除不依赖 POSIX unlink 语义；Windows/WSL 双环境下路径用 pathlib。
- **依赖最小化**：密码哈希用标准库 hashlib.pbkdf2_hmac；不引入 sqlalchemy（SQL 直写 + Repository 封装即可，与项目"声明是数据、实现是代码"风格一致）。
- **访问层补丁（D5~D10/S1~S6）**：均为几行级、随阶段 F 落地；`core/access.py` 的 `network="web"` 枚举改完跑 access/policy 相关测试组。前端访问层（`web/` 工程）按 [api_access_layer_plan.md](api_access_layer_plan.md) 契约先行、MSW 兜底，与 M1 **并行开发**，不阻塞本里程碑；M1 骨架产出 OpenAPI 后前端替换手写类型。

## 五、验证

- 6 个新测试组全绿并注册到 GROUPS；
- 既有 92 组 + MCP 69 项全绿（阶段 A/F 后各跑一次全量）；
- grep 门禁（storeback 测试组内硬断言，破线即红）：`server/` 树内 `Store()` 命中 0；`server/app/` 除 `store/` 模块外 `duckdb.connect` 命中 0；core/scripts/skills 既有 11 处无参 `Store()` 与 3 处脚本直连**保持不变**（本地入口非回归项）；
- 存量改动清单核对：`git diff --stat` 中存量文件仅 `core/access.py`、`run_tests.py`、`.gitignore` 三项；
- 端到端冒烟：uvicorn 起 API + run_worker 起 Worker，经 HTTP 完成"登录→建案→入队 BUILD→SSE 进度→版本切换→只读查询到新数据"；
- 隔离验收：构造 A/B 两案件，写入 A 的数据在 B 查不到（W-001 AC-2）。

## 六、风险与处置

| 风险 | 处置 |
|---|---|
| ~~门面只读改造引发大面积回归~~（已消除） | M1 不改 `core/store.py`、不碰 101 处调用点；门面化推迟 M2，届时 CaseStore 按 `DisposalBoard(store=)` 协议鸭子适配或再补门面，改动面与现在等价，`server/app/store/` 结构已按 backend_api.md 第五部分预留 |
| grep 门禁被绕过（服务端代码违规直开库） | 门禁在 storeback 测试组内强制、破线即红；代码审查守边界纪律（routers/worker 只经 StoreFactory 取连接，不 import core.store.Store、不直连 duckdb） |
| BUILD 复制大文件耗时随库增长 | MVP 接受（案件级文件、构建本就是长任务异步化）；M2 视实测改为"冷层重建+delta"，接口不变 |
| Worker 崩溃留半成品版本 | 成功才切指针（失败 vN 完好）；孤儿扫描 M1 留钩子记日志，完整清理在 M2（W-009） |
| SQLite 元数据在极端并发下锁超时 | busy_timeout + 写事务极短；AC-2 并发测试验证；Postgres 切换路径已由 Repository 隔离 |
| FastAPI 新依赖在内网环境安装 | 仅 3 个纯 Python 包（fastapi/uvicorn/httpx），WSL venv 清华镜像安装；requirements 写入 server/ |
| M1 范围蔓延（业务端点诱惑） | 严格只做 auth/cases/tasks 三类端点；线索/仪表盘/处置一律 M2/M3 |

---

## 七、实施记录（2026-09-08 落地）

**测试**：新增 5 个测试组共 54 条用例，注册于 run_tests.py（storeback 16 / metastore 14 / casesnap 5 / taskqueue 9 / apibase 10），全绿；全量回归 95+ 组通过。

**与计划的偏差（均为小幅、追加性质）**：

1. **core/ 改动 2 处（计划估 1 处）**：
   - `core/access.py`：`NETWORKS` 增 `"web"`、`can_llm_call()` 同步（按计划）；
   - `core/ontology.py`：`build_ontology(conn, pack="default", base_dir=None)` 增补**可选** `base_dir` 形参并透传 `load_pack(pack, base_dir=...)`（loader 本已支持 base_dir，build_ontology 未转发）。缺省 None 时行为与之前完全一致，存量调用零改动；这是案件快照包构建（W-005）的必要挂钩。
2. **测试组合并**：计划中的 versionswitch 组并入 taskqueue 组（H1~H4 版本切换与 Worker 执行同批验证，含真实 build_ontology + 模板库冒烟）。
3. **模块归并**：重试/指数退避未单建 retry.py，内聚于 `worker/pool.py`（`_fail` + 内存退避时刻）；认证基元在 `server/app/security.py`（PBKDF2/token/Bearer）+ `routers/auth.py`，未建 `server/app/auth/` 包。
4. **pack 版本凭据**：`PackSnapshot.version` 存声明文件指纹（objects/links/bindings/rules 等 8 文件 sha1 前 12 位），内容变指纹变，兼作快照完整性基线。
5. **BUILD v1 初始化**：支持 `--template investigation.duckdb` 模板库复制（保 parquet 相对路径视图/附属表）；无模板时写连接自动建空库。

**运行态目录**：`cases/`（案件版本库 vN.duckdb + ontology 快照）与 `meta/`（meta.db SQLite-WAL）均已加入 `.gitignore`。

**启动方式**：
- API：`uvicorn server.app.main:app --reload`（默认 `meta/meta.db` + `cases/`，env：SUNZI_META_DB / SUNZI_CASES_ROOT / SUNZI_CORS_ORIGINS / SUNZI_SESSION_TTL_HOURS）；
- Worker：`python -m server.run_worker --workers 2 [--template investigation.duckdb]`。
