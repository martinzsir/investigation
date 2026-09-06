# M1 地基里程碑实施计划（REQ-W-001~007 + API 骨架）

**日期** 2026-09-07 ｜ **状态** 待批准
**关联** [backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)（决策 D1~D4 已锁定）、[req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)、[adr.md](file:///d:/dev/inves_duckdb/.trae/documents/web/adr.md)
**范围** W-001 Store 无参消除、W-002 直连收口、W-003 StoreBackend 接口与门面、W-004 元数据层（SQLite-WAL）、W-005 案件生命周期与 pack 快照锁定、W-006 任务队列与 Worker 池、W-007 版本化文件与原子切换；外加 FastAPI 骨架与认证/案件/任务三类最小端点（M1 可端到端演示：建案→入队 BUILD→Worker 构建新版本→原子切换→只读可见）。
**不含** state.sqlite 审计链迁移（M2）、仪表盘/线索/处置等业务端点（M2/M3）、跨案件 ATTACH（M5）、Postgres 实现（Repository 方言预留，不实现）。

---

## 一、仓库研究结论

1. **Store 现状**（[core/store.py](file:///d:/dev/inves_duckdb/core/store.py)）：具体类，`Store(root="data", db_path="investigation.duckdb")`，`.conn` 惰性 `duckdb.connect()`（读写），`query()` 含 REQ-003 直查拦截与 unsafe 审计通道，`execute()` 为写路径，另有 L1 dict 热层与 cold_scan。
2. **无参 `Store()` 实测分布**（生产路径 11 处，须改）：
   - [run_all.py:59](file:///d:/dev/inves_duckdb/run_all.py#L59)、[run_demo.py:31](file:///d:/dev/inves_duckdb/run_demo.py#L31)、[run_with_invoker.py:41](file:///d:/dev/inves_duckdb/run_with_invoker.py#L41)
   - [core/disposal.py:140](file:///d:/dev/inves_duckdb/core/disposal.py#L140)、[core/ontology.py:314](file:///d:/dev/inves_duckdb/core/ontology.py#L314)
   - skills：[yong_jian.py:12](file:///d:/dev/inves_duckdb/skills/yong_jian.py#L12)、[xu_shi.py:20](file:///d:/dev/inves_duckdb/skills/xu_shi.py#L20)、[qi_zheng.py:12](file:///d:/dev/inves_duckdb/skills/qi_zheng.py#L12)
   - scripts：[build_ontology.py:45](file:///d:/dev/inves_duckdb/scripts/build_ontology.py#L45)、[export_ladybug.py:117](file:///d:/dev/inves_duckdb/scripts/export_ladybug.py#L117)、[q2_overpass_cypher.py:31](file:///d:/dev/inves_duckdb/scripts/q2_overpass_cypher.py#L31)、[demo_profile.py:150](file:///d:/dev/inves_duckdb/scripts/demo_profile.py#L150)、[profile_table.py:178](file:///d:/dev/inves_duckdb/scripts/profile_table.py#L178)
   - MCP：[scripts/mcp_server.py](file:///d:/dev/inves_duckdb/scripts/mcp_server.py) 8 处（348/403/449/651/780/806/900 等）
   - 测试/benchmark 的 `Store(db_path=":memory:")` 按 W-001 AC-4 **豁免不改**。
3. **生产直连 `duckdb.connect` 3 处**：[scripts/init_duckdb.py:78](file:///d:/dev/inves_duckdb/scripts/init_duckdb.py#L78)、[scripts/incremental.py:95](file:///d:/dev/inves_duckdb/scripts/incremental.py#L95)、[scripts/export_dashboard.py:12](file:///d:/dev/inves_duckdb/scripts/export_dashboard.py#L12)；tests/benchmarks 直连豁免（W-002 AC-4）。
4. **pack 机制可复用**：`PackManager.init_pack(from_pack=)` 已能复制声明目录（[core/pack.py:71](file:///d:/dev/inves_duckdb/core/pack.py#L71)）；`load_pack(pack, base_dir=)` 支持自定义根（[core/ontology_loader.py:69](file:///d:/dev/inves_duckdb/core/ontology_loader.py#L69)）——案件快照 = 复制到 `cases/{cid}/ontology_snapshot/` 后以 `base_dir=cases/{cid}`、pack 名 `ontology_snapshot` 装载，无需改 loader。
5. **内核写操作以"连接"为参数**：`build_ontology(conn, pack=...)` 接收裸连接，Worker 打开新版本文件的 write_conn 传入即可，内核零改造。
6. **测试基建**：[run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) GROUPS 注册表（现 92 组），新组须同步注册；MCP 69 项端到端（mcp_client_test）须保持绿。
7. **环境**：WSL venv `/root/.venvs/inves`，需新增 `fastapi`、`uvicorn`、`httpx`（TestClient 用），pip 走国内镜像。

---

## 二、文件与模块

### 新增

| 路径 | 内容 |
|---|---|
| `core/store_backend.py` | `StoreBackend` ABC（read_conn/write_conn/query/version/case_id 五抽象方法）、`CaseStore(mode="read"/"write")`、`CrossCaseStore`（M1 仅骨架：write_conn 抛 UnsupportedOperation，ATTACH 留 M5）、`UnsupportedOperation`、`StoreFactory`（for_case/for_local；查版本指针→拼路径→开连接→登记读者引用计数） |
| `server/__init__.py`、`server/app/__init__.py` | FastAPI 应用包 |
| `server/app/main.py` | 应用入口、中间件（会话鉴权→AccessContext）、路由挂载、统一响应信封与异常处理 |
| `server/app/deps.py` | 依赖注入：会话解析、AccessContext 构造、案件 membership 校验、只读连接请求作用域（yield + close） |
| `server/app/auth/` | 登录/会话/token（MVP：user 表 + 密码哈希（PBKDF2，标准库 hashlib，无新依赖）；双因子留接口不实现） |
| `server/app/meta/models.py` | 元数据表层定义：tenant/user/session/case/case_membership/task/version_pointer/platform_audit |
| `server/app/meta/repo.py` | Repository 抽象接口（方言隔离，Postgres 留空实现占位） |
| `server/app/meta/repo_sqlite.py` | SQLite-WAL 实现：建表、WAL pragma、busy_timeout、任务唯一约束、版本指针强一致读写 |
| `server/app/cases.py` | 案件生命周期：create_case（建目录+锁快照+哈希清单）、list/get、快照校验 |
| `server/app/worker/pool.py` | Worker 调度循环：案件级 FIFO + 全局并发 N、老任务优先、状态机流转 |
| `server/app/worker/tasks.py` | 任务处理器注册表：M1 实现 `BUILD`（复制/新建版本文件→write_conn→build_ontology(snapshot)→切指针）与 `PING`（队列自测）；IMPORT/SCAN/EXPORT 留注册占位 |
| `server/app/worker/retry.py` | 指数退避（base×2^n）、语义错误白名单（缺列/权限/声明非法不重试） |
| `server/app/routers/{auth,cases,tasks}.py` | 三类最小端点 |
| `server/app/sse.py` | SSE 进度推送（D4：500ms 轮询 task 表，Last-Event-ID 支持） |
| `server/run_worker.py` | Worker 进程入口（`python -m server.run_worker`，D3 独立进程） |
| `tests/test_store_backend.py` | W-001/002/003 验收（ABC 不可实例化、只读连接写失败、CrossCase 写拒绝、门面默认只读、query 语义不变、A/B 案件互不可见） |
| `tests/test_meta_store.py` | W-004：建表、50 并发写不丢不重、指针强一致、元数据不可用明确报错 |
| `tests/test_case_snapshot.py` | W-005：建案锁 13 文件快照、改共享本体不影响案件、案件读快照、快照篡改检出 |
| `tests/test_version_switch.py` | W-007：仓内复现 spike H1~H4（读者持 vN 时就地写失败/构建 vN+1 成功/旧句柄仍可读/失败回滚 vN 完好/指针切换原子） |
| `tests/test_task_queue.py` | W-006：同案件串行、跨案件并行≤N、幂等唯一约束、状态机、语义错误不重试、退避重试、进度单调、老任务优先 |
| `tests/test_api_base.py` | FastAPI TestClient：登录→me→建案→入队 BUILD→SSE 收到进度→任务成功→案件版本更新→无 token 401→跨租户 404/403 |

### 修改

| 路径 | 改动 |
|---|---|
| [core/store.py](file:///d:/dev/inves_duckdb/core/store.py) | `Store` 改兼容门面：新增 `backend` 可选参数；传入 backend 时委托（`.conn` 返回**只读** read_conn，新增 `write_conn()`）；未传 backend 时维持 legacy 本地模式（root/db_path，读写语义不变，保测试与 CLI）；REQ-003 拦截/unsafe/cold_scan/L1 逻辑保留，经 backend 委托执行 |
| [core/__init__.py](file:///d:/dev/inves_duckdb/core/__init__.py) | 导出 StoreBackend/CaseStore/StoreFactory |
| 生产路径 11 处无参 `Store()` | 改为显式工厂/参数：CLI 与 MCP 属本地单案件模式，用 `StoreFactory.for_local(pack="default", mode=...)`（mode 按读/写用途选择）；core 内部两处（disposal.py、ontology.py:314）改为接收外部传入 store/连接，无上下文时用 for_local 显式构造 |
| [scripts/init_duckdb.py](file:///d:/dev/inves_duckdb/scripts/init_duckdb.py)、[scripts/incremental.py](file:///d:/dev/inves_duckdb/scripts/incremental.py)、[scripts/export_dashboard.py](file:///d:/dev/inves_duckdb/scripts/export_dashboard.py) | 直连收口：走 Store/工厂，路径显式传入，不依赖模块级 `DB` 常量 |
| [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) | 注册 6 个新测试组：storeback/metastore/casesnap/versionswitch/taskqueue/apibase |
| [.gitignore](file:///d:/dev/inves_duckdb/.gitignore) | 新增 `cases/`、`meta/`（案件数据与元数据库为运行产物） |

---

## 三、实施步骤（依赖顺序）

### 阶段 A：Store 收口（W-001/002/003）

1. 新建 `core/store_backend.py`：ABC + CaseStore + CrossCaseStore 骨架 + StoreFactory（for_case 读版本指针拼 `cases/{cid}/v{N}.duckdb`；for_local 兼容现有 `data/investigation.duckdb` 布局）。
2. 改 `core/store.py` 为门面（backend 委托 + legacy 双通道），保 `query(unsafe/reason/operator/max_rows)` 签名语义不变。
3. 逐个替换生产路径 11 处无参 `Store()`（每替换一处跑相关测试组）：core 内部 2 处 → skills 3 处 → scripts 5 处 → run_all/run_demo/run_with_invoker → mcp_server 8 处。
4. 收口 3 处生产直连。
5. 写 `tests/test_store_backend.py` 并注册；跑 grep 验收（AC-1：非 test/benchmark 无 `Store()`；生产路径仅 core/store*.py 出现 `duckdb.connect`）。
6. 全量回归（92 组 + mcp 69 项）必须全绿。

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

19. `server/app/main.py` + deps + auth：token 会话（secrets 模块生成，存 session 表，超时可配）；登录失败统一文案；`/api/auth/login`、`/api/auth/logout`、`/api/auth/me`。
20. routers/cases：`GET /api/cases`（仅本租户）、`POST /api/cases`（建案+快照）、`GET /api/cases/{cid}`（含 data_version）。
21. routers/tasks：`POST /api/tasks`（入队，幂等键，202）、`GET /api/tasks`、`GET /api/tasks/{tid}`、`GET /api/tasks/{tid}/events`（SSE 轮询版）、`POST /api/tasks/{tid}/cancel`。
22. 统一响应信封 `{ok,data,data_version}` / `{ok,error:{code,message}}` 与错误码表（backend_api.md 4.3 子集：401/403/404/409/422/503）。
23. `tests/test_api_base.py`：TestClient 端到端（建案→BUILD→SSE→版本切换可读）；红线断言：无 token 401、跨租户访问 404、operator 不取请求体。
24. WSL venv 安装 fastapi/uvicorn/httpx（清华镜像）；跑 `python -m server.run_worker` 与 uvicorn 手工冒烟一次。

### 阶段 G：收口

25. 全量回归：`run_tests.py` 全部组（92+6=98 组）+ `mcp_client_test` 69 项全绿。
26. 更新 backend_api.md 里程碑状态（M1 已落地项）；提交 git（排除 cases/、meta/ 数据产物）。

---

## 四、依赖与注意事项

- **门面只读的兼容性红线**：`Store(backend=...)` 的 `.conn` 才默认只读；legacy `Store(root,db_path)` 保持读写（测试 `:memory:` 与本地 CLI 依赖此语义）。若把 legacy 也改只读，build_ontology 等 101 处调用点会大面积失效——这是本计划最大的回归风险点，靠"每改一处即回归"控制。
- **core/ontology.py:314 与 core/disposal.py:140 的内部 Store()**：优先改为参数注入；确无上下文的兜底用 for_local（mode 按用途），不得保留无参。
- **MCP 不能坏**：mcp_server 8 处改 for_local 后，69 项 mcp_client_test 是硬验收。
- **BUILD 新版本策略**：MVP 用"复制 vN → 重建语义层"而非从零建库（冷层 Parquet 相对路径、meta_unsafe_query 等附属表随之保留）；v1 由建案时从模板初始化（init_duckdb 建表逻辑收口后复用）。
- **SQLite 并发**：WAL 单写者，N 个 Worker 并发写同一 meta 库靠 busy_timeout 串行化；任务表写事务短（状态更新），不构成瓶颈。
- **load_pack 快照复用**：快照目录命名 `ontology_snapshot` 使其符合 `base_dir/pack` 装载约定，loader 零改动。
- **跨平台**：版本文件复制/删除不依赖 POSIX unlink 语义；Windows/WSL 双环境下路径用 pathlib。
- **依赖最小化**：密码哈希用标准库 hashlib.pbkdf2_hmac；不引入 sqlalchemy（SQL 直写 + Repository 封装即可，与项目"声明是数据、实现是代码"风格一致）。

## 五、验证

- 6 个新测试组全绿并注册到 GROUPS；
- 既有 92 组 + MCP 69 项全绿（阶段 A/F 后各跑一次全量）；
- grep 验收：`Store()` 非 test/benchmark 命中 0；生产路径 `duckdb.connect` 仅命中 core/store.py 与 core/store_backend.py；
- 端到端冒烟：uvicorn 起 API + run_worker 起 Worker，经 HTTP 完成"登录→建案→入队 BUILD→SSE 进度→版本切换→只读查询到新数据"；
- 隔离验收：构造 A/B 两案件，写入 A 的数据在 B 查不到（W-001 AC-2）。

## 六、风险与处置

| 风险 | 处置 |
|---|---|
| 门面只读改造引发大面积回归 | legacy 通道保读写语义；生产站点逐个替换+逐点回归；阶段 A 结束设全量回归门禁 |
| BUILD 复制大文件耗时随库增长 | MVP 接受（案件级文件、构建本就是长任务异步化）；M2 视实测改为"冷层重建+delta"，接口不变 |
| Worker 崩溃留半成品版本 | 成功才切指针（失败 vN 完好）；孤儿扫描 M1 留钩子记日志，完整清理在 M2（W-009） |
| SQLite 元数据在极端并发下锁超时 | busy_timeout + 写事务极短；AC-2 并发测试验证；Postgres 切换路径已由 Repository 隔离 |
| FastAPI 新依赖在内网环境安装 | 仅 3 个纯 Python 包（fastapi/uvicorn/httpx），WSL venv 清华镜像安装；requirements 写入 server/ |
| M1 范围蔓延（业务端点诱惑） | 严格只做 auth/cases/tasks 三类端点；线索/仪表盘/处置一律 M2/M3 |
