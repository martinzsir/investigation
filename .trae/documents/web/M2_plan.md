# M2 可信首页里程碑实施计划（REQ-W-008/009/018/023/024 + D1 骨架）

**日期** 2026-09-07 ｜ **状态** 已评审通过（2026-09-07，决策点 D-M2-1/2/3 均按推荐项确认；待实施）
**关联** [backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)（批次表：M2=批 4；D1 决策；2.4 写路径；第五部分目录）、[M1_plan.md](file:///d:/dev/inves_duckdb/.trae/documents/web/M1_plan.md)（已实施，2026-09-07 验证全绿）、[req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)（W-008/009/018/023/024 AC 原文）
**范围** 批 4 五项：W-008 版本延迟回收（含 AC-5 归档压实）、W-009 孤儿扫描、W-018 治理仪表盘、W-023 审计链查看与自检、W-024 操作者身份绑定（含 platform_audit 平台审计表与 `/api/audit/events`）；外加 D1 的 **state.sqlite 骨架与迁移方案细化**（决策 D1 明文"在 M2 里程碑细化"）。
**不含** 处置/线索/数据接入端点（M3）、`core/store.py` 门面化（推 M3，见决策 D-M2-2）、ActionExecutor/AuditChain **state 写路径接线**（M3，与处置端点同批，M2 只交骨架+方案）、W-022 异常通道端点（M4 批 6）、跨案件 ATTACH（M5）、Postgres 实现（Repository 方言预留不实现）。

**最小影响红线（本计划首要原则，继承 M1）**：M2 对 server 外存量代码的改动目标为 **core/ 仅 1 处追加式只读方法**（`core/audit.py` 增 `timeline()`，决策 D-M2-1）+ `run_tests.py` 注册 + 文档状态更新。`core/store.py`、`core/action_executor.py`、11 处无参 `Store()`、3 处脚本直连、MCP 全部零改动。

---

## 一、仓库研究结论（M1 交付现状与 M2 依赖锚点）

1. **读者引用计数是进程内存 dict**（[backend.py:210,241,255-268](file:///d:/dev/inves_duckdb/server/app/store/backend.py#L210)）：`StoreFactory._readers` 仅本进程可见。回收在 Worker 进程执行，**跨进程读者计数必须落 meta**——M2 引入 `case_reader_lease` 表（租约），API 进程请求作用域登记/注销，内存 dict 降级为进程内快速路径。这是 W-008 的核心设计点。
2. **meta 层缺口**（[repo_sqlite.py](file:///d:/dev/inves_duckdb/server/app/meta/repo_sqlite.py) 建表清单）：现有 meta_users/meta_sessions/cases/case_pack_snapshots/case_version/tasks 六表；**无版本历史表**（`set_version/current_version` 只有当前指针，无 `pending_reclaim` 状态位）、**无读者租约表**、**无 platform_audit 表**（backend_api.md 2.5 列为核心表，M1 未建）、**无 ops_events 表**（W-009 AC-5 扫描结果进健康度的载体）。M2 四表齐补，全在 `server/app/meta/` 内。
3. **孤儿扫描钩子未实际存在**：M1_plan 实施记录声称"预留孤儿扫描钩子"，但 [run_worker.py](file:///d:/dev/inves_duckdb/server/run_worker.py) 与 worker/pool.py、worker/tasks.py 中 grep `orphan|孤儿|scan` 无命中——**以代码为准，W-009 从零新建** `worker/orphan_scan.py` 并在 `run_worker.main()` 启动序列挂载。
4. **仪表盘 core 只读面已齐**（边界纪律"routers 不写 SQL"可守住）：健康度 `RunHealth.summary()/health_section()/rows()`（[run_health.py:124,149,184](file:///d:/dev/inves_duckdb/core/run_health.py#L124)）；线索待办 `DisposalBoard.by_status()`（[disposal.py:52](file:///d:/dev/inves_duckdb/core/disposal.py#L52)）；实体裁决待办 `ReviewQueue.pending()`（[review.py:128](file:///d:/dev/inves_duckdb/core/review.py#L128)）；规则指标/推翻率 `list_metrics()`（[metrics.py:165](file:///d:/dev/inves_duckdb/core/metrics.py#L165)）；覆盖缺口经 `run_diagnostic` 诊断行（REQ-G-008/009 dimecoverage 既有产物，detail 已含具体维度名，W-018 AC-4 透传即可）。**server 侧组装器只编排聚合，不新增 core 写面**。
5. **审计链读面缺口唯一**：[core/audit.py](file:///d:/dev/inves_duckdb/core/audit.py) 已有 `chain_verify()`（bool）、`chain_integrity()`（明细 dict）、`root_hash()`、`count()`，**无按行 listing/筛选/分页的只读方法**——W-023 时间线端点（operator/from/to/action/clue_id 筛选 + 分页）无现成读面。按"routers 不写 SQL"纪律，须在 core 增一个追加式 `timeline()` 方法（决策 D-M2-1，M2 对 core 的唯一触点）。
6. **空链语义冲突（W-023 AC-4）**：[core/audit.py:232-234,280-285](file:///d:/dev/inves_duckdb/core/audit.py#L232) 的 REQ-G-025 语义="链全空且确无处置（纯查询运行）→ 保持 chain_ok=True"（auditinteg 组 9 条测试锚定）；而 W-023 AC-4 红线="空链（0 条）不得返回 chain_ok=true"。**裁决（决策 D-M2-3）：core 语义不动，红线在 server 端点包装层强制**——`/audit/verify` 对 `actual_count==0` 一律返回 `chain_ok=false` + `empty_chain=true`（Web 场景不存在"纯查询运行"的空案件）。CLI/MCP 路径零感知。
7. **SSE 不是 DuckDB 读者**：M1 SSE 进度流数据源是 meta task 表轮询（[sse.py](file:///d:/dev/inves_duckdb/server/app/sse.py)），不持分析库连接——租约只需覆盖请求作用域只读连接（deps.py yield+close 处登记/注销），无长连接续租问题。
8. **测试基建**：run_tests.py GROUPS 现 98 组（92 存量 + M1 5 组 + e2e）；MCP 69 项须保持绿。M2 新增 5 组后 103 组。
9. **环境**：WSL venv `/root/.venvs/inves` 已补齐 fastapi/uvicorn/httpx（2026-09-07 清华 403 换阿里云镜像装）；M2 无新依赖（sqlite3/pathlib 均标准库）。

---

## 二、文件与模块

### 新增

| 路径 | 内容 |
|---|---|
| `server/app/worker/reclaim.py` | W-008 版本回收器：周期扫描 `case_version_history` 中 `pending_reclaim` 且无活跃租约的版本 → meta 单事务内二次校验租约 → 删除文件（AC-4 空间实际释放）→ 标 `reclaimed`；回收动作记 ops_events |
| `server/app/worker/orphan_scan.py` | W-009 孤儿扫描：启动时 + 周期执行；判定白名单=版本历史有 active/pending_reclaim 记录或等于当前指针的**一律不动**；仅"无历史记录且非当前版本"的 vN.duckdb 移入 `cases/{cid}/.quarantine/`（AC-2 隔离区非直删）；隔离区 TTL 7 天清理（AC-3）；结果记 ops_events（AC-5 进健康度）；扫描全程只读案件目录不碰连接（AC-4） |
| `server/app/store/state_store.py` | D1 骨架：`StateStore`（per-case `cases/{cid}/state.sqlite`，WAL、busy_timeout=5000）；schema=audit_chain（同构 DuckDB 表结构）+ action_request + clue_disposal_status + review_decision；提供 `open()/import_from_duckdb()`（迁移演练用，幂等）；**M2 不接任何写路径**（决策 D-M2-2） |
| `server/app/routers/dashboard.py` | W-018：`GET /api/cases/{cid}/dashboard`（健康度横幅+双覆盖+待办计数+ops 摘要）、`GET /api/cases/{cid}/diagnostics?kind=&severity=`、`GET /api/cases/{cid}/diagnostics/{did}`（MVP：诊断行本体+关联 ID；跳过记录深链 M4 细化） |
| `server/app/routers/audit.py` | W-023/024：`GET /api/cases/{cid}/audit`（时间线筛选分页）、`POST /api/cases/{cid}/audit/verify`（自检+空链红线包装 D-M2-3）、`GET /api/audit/events?tenant=`（平台审计，仅管理员） |
| `server/app/dashboard.py` | 仪表盘组装器：单读连接内编排 core 只读面（研究结论 4）+ ops_events，产出信封结构；不含 SQL |
| `tests/test_version_reclaim.py` | W-008 AC-1~5：切换后标 pending_reclaim；有租约不删；租约清零 N 周期内删；文件实删（磁盘释放断言=文件不存在+目录大小下降）；ARCHIVED 压实仅留最终版 |
| `tests/test_orphan_scan.py` | W-009 AC-1~5：构造孤儿文件（模拟强杀：写文件无历史记录）→ 扫描发现；移隔离区；TTL 清理；扫描不碰正常案件读写；结果进 ops_events |
| `tests/test_dashboard.py` | W-018 AC-1~4,6：首屏健康度 healthy/degraded+计数；诊断类别全覆盖（零命中/跳过/覆盖缺口/版本锚定/脏值）；双覆盖并列；缺口文案含具体维度名；诊断可下钻 |
| `tests/test_audit_view.py` | W-023 AC-1~6 + W-024 AC-1~5：时间线字段齐全可筛选；篡改检出；**空链 verify 返回 chain_ok=false+empty_chain=true（红线）**；处置事件与非待处置线索数交叉比对；operator 不取请求体；operator≠会话主体拒绝；审计 operator 与登录记录一一对应 |
| `tests/test_state_store.py` | D1 骨架：建库 WAL 模式断言；schema 与 DuckDB audit_chain 同构；`import_from_duckdb` 幂等迁移演练（行数+root_hash 一致）；**断言无任何 server 写路径接线**（grep 门禁：state_store 外无 import） |

### 修改（存量仅 3 处，均为追加性质）

| 路径 | 改动 |
|---|---|
| [core/audit.py](file:///d:/dev/inves_duckdb/core/audit.py) | **追加唯一新方法 `timeline()`**（决策 D-M2-1）：只读 listing + operator/时间/线索/动作筛选 + limit/offset，返回 `{items,total}`；不改 `append/chain_verify/chain_integrity/root_hash` 任何既有函数；auditinteg 组原样全绿 |
| [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) | 注册 5 个新测试组：reclaim/orphanscan/dashboard/auditview/statestore（纯追加） |
| [server/app/meta/repo_sqlite.py](file:///d:/dev/inves_duckdb/server/app/meta/repo_sqlite.py) | 四张新表 + 配套方法：`case_version_history`（version/status active→pending_reclaim→reclaimed/时间戳）、`case_reader_lease`（case_id/version/lease_id/pid/acquired_at/expires_at，TTL 兜底崩溃读者）、`ops_events`（kind/case_id/payload，孤儿扫描+回收动作）、`platform_audit`（tenant/operator/event/detail，登录/登出/鉴权失败）；`meta_users` 加 `is_admin` 列（ALTER ADD COLUMN，缺省 0）；`set_version()` 同步写历史表（既有调用方零改动） |

**明确不改**（最小影响决策）：

- `core/store.py` **不做门面化**：门面化触发条件是处置端点需要 store 协议（M1_plan 第 65 行），处置端点在 M3——M2 范围内无消费方，门面化随处置端点同批推 M3（决策 D-M2-2）。
- `core/action_executor.py` / `AuditChain` 写路径 **不做 state 接线**：M2 只交 state_store.py 骨架+迁移方案；接线与处置端点（唯一写消费者）同批 M3，避免"无消费者的 core 改动"。
- `core/audit.py` 既有函数语义零改动（空链 REQ-G-025 语义保留在 core，红线在 API 包装层，决策 D-M2-3）。
- 11 处无参 `Store()`、3 处脚本直连、MCP 8 处：全部原样（W-023 审计读端点经案件读连接取 `AuditChain` 实例，不构造 `Store`）。
- W-022 `/api/cases/{cid}/anomalies`：M4 批 6，M2 仪表盘**不含**异常通道卡片（防范围蔓延）。

---

## 三、实施步骤（依赖顺序）

### 阶段 A：版本回收与读者租约（W-008）

1. meta 四表落地（见修改表）+ `set_version` 写历史；既有 `current_version/set_version` 签名不变。
2. `StoreFactory` 读者登记改造：`open_read()` 时在 meta 写租约（lease_id=uuid4，TTL 60s），`release()` 删租约；进程内 `_readers` dict 保留为同进程快速路径；deps.py 请求作用域依赖在 yield 前后接登记/注销（SSE 不涉及）。
3. `worker/reclaim.py`：周期（默认 60s，`SUNZI_RECLAIM_INTERVAL` 可配）扫描 pending_reclaim → 单事务二次校验无活跃租约 → 删文件 → 标 reclaimed → 记 ops_events。
4. ARCHIVE 压实任务（W-008 AC-5）：`worker/tasks.py` 注册 `ARCHIVE` 处理器——ARCHIVED 案件删除历史版本文件仅留当前版本（复用 BUILD 的复制/删除模式）；案件状态机 `ARCHIVED` 迁移已在 meta 状态机中（M1 `assert_case_transition`），无需改。
5. 写 `tests/test_version_reclaim.py` 并注册 reclaim 组。

### 阶段 B：孤儿扫描（W-009）

6. `worker/orphan_scan.py`：启动时执行一次 + 随回收周期执行；白名单判定（版本历史/当前指针）→ 孤儿移 `cases/{cid}/.quarantine/`（带时间戳目录）→ ops_events 记录（数量/路径）→ 隔离区超过 7 天（`SUNZI_ORPHAN_TTL_DAYS`）的清理。
7. `run_worker.py` 启动序列挂载：`pool.start()` 前调用 `orphan_scan.run(repo, factory)`（一次），异常只记日志不阻塞 Worker。
8. 写 `tests/test_orphan_scan.py` 并注册 orphanscan 组。

### 阶段 C：审计查看与自检（W-023）

9. `core/audit.py` 追加 `timeline()`（唯一 core 触点，决策 D-M2-1）；补 2~3 条单测进 auditview 组（不新建 core 测试文件，跟随 server 侧组）。
10. `routers/audit.py`：时间线端点（筛选参数透传 timeline()，分页信封 `{items,total,page,page_size}`）；verify 端点调 `chain_integrity()` + **包装层空链红线**（`actual==0 → chain_ok=false, empty_chain=true`，D-M2-3）+ 处置事件数与非待处置线索数交叉比对（`chain_integrity.disposal_events` vs `DisposalBoard.by_status` 非待处置计数）。
11. 写 `tests/test_audit_view.py`（W-023 部分）。

### 阶段 D：operator 绑定与平台审计（W-024）

12. `meta_users` 加 `is_admin`；auth 路由埋点：登录成功/登出/鉴权失败（401/403）写 `platform_audit`（operator 取会话主体，事件含 ts/tenant/ip 摘要）。
13. `GET /api/audit/events?tenant=`：仅 `is_admin=1` 或 `role=system` 可访问，否则 403；跨租户查询需管理员权限（沿用 M1 租户 membership 模式）。
14. W-024 红线断言（进 auditview 组）：请求体 operator 字段被忽略（M1 deps 已实现，补断言）；operator≠会话主体 → 403（REQ-009 内核校验经 API 触达）；审计链 operator 值与 platform_audit 登录主体字符串一一对应（同值断言）。

### 阶段 E：治理仪表盘（W-018）

15. `app/dashboard.py` 组装器：单读连接内依次取 `RunHealth.summary()`（横幅+计数）、run_diagnostic 诊断行（rows+kind/severity 过滤）、双覆盖（REQ-G-008/009 既有产物：声明覆盖+实证缺口独立报警，detail 透传维度名）、`DisposalBoard.by_status` 待办计数、`ReviewQueue.pending` 计数、ops_events 摘要（孤儿/回收）。
16. `routers/dashboard.py` 三端点挂载 `/api/v1`；信封/错误码沿用 M1（404=跨租户案件不可见）。
17. 写 `tests/test_dashboard.py` 并注册 dashboard 组。

### 阶段 F：D1 state.sqlite 骨架与迁移方案（决策 D1"在 M2 细化"）

18. `store/state_store.py`：StateStore（WAL/busy_timeout/与 DuckDB audit_chain 同构 schema）+ `import_from_duckdb()` 幂等迁移（读案件 DuckDB audit_chain 全量行 → 写 state.sqlite → root_hash 与行数比对断言）。
19. 迁移与双写过渡期方案成文（本文件文末附录 A）：M3 处置端点上线时 ActionExecutor 经注入式 `chain_backend` 写 state.sqlite；过渡期双写（DuckDB+state 各一条，读以 state 为准）；既有本地 CLI/MCP 保持 DuckDB 单写不变；M5 案件包导出时 `state → audit/chain.csv`（ADR 5.2 目录不变）。
20. 写 `tests/test_state_store.py` 并注册 statestore 组。

### 阶段 G：收口

21. 全量回归：`run_tests.py` 103 组 + `mcp_client_test` 69 项全绿。
22. 更新 backend_api.md 里程碑状态（M2 已落地项）；提交 git（排除 cases/、meta/ 数据产物——`.gitignore` 已覆盖）。

---

## 四、依赖与注意事项

- **租约是回收的正确性基础**：M1 进程内 `_readers` 跨进程不可见（研究结论 1），回收判定一律以 `case_reader_lease` 表为准；TTL（60s）兜底 API 进程崩溃的僵尸租约——僵尸期间旧版本晚删一个周期，安全方向偏保守。
- **回收竞态**：租约校验与删除在 meta 单事务内完成，删文件前二次确认；文件删除失败（Windows 句柄占用）仅告警不重试删除，留待下周期（幂等）。
- **孤儿判定白名单从严**：有版本历史记录（任何状态）或等于当前指针的文件永不入隔离区；M1 之前手工放置的版本文件会被判孤儿——部署说明中提示先跑一次 `--dry-run`（orphan_scan 支持，只记 ops_events 不移动）。
- **空链红线只改 API 契约不改 core**（D-M2-3）：auditinteg 组（REQ-G-025"纯查询运行空链=完整"）与 W-023 AC-4（Web 案件空链=不完整）是两个合法场景：CLI 纯查询运行 vs 平台案件必须有过 BUILD/处置留痕。裁决依据写入 auditview 组测试注释。
- **`timeline()` 追加式纪律**：不改任何既有函数签名/行为；`chain_integrity()` 的 health.record 副作用原样（M2 verify 端点复用它，诊断落 run_diagnostic 与仪表盘横幅自然联通）。
- **仪表盘读放大**：组装器单请求单连接单遍取数（研究结论 4 各读面均为 O(诊断行数)），rows 默认 limit 200 可配；分页/筛选在 server 组装层内存过滤（诊断行规模 ≤ 千级，不构成索引需求）。
- **平台审计与案件审计分离**：platform_audit 只记平台事件（登录/登出/鉴权失败；跨案件查询记录 M5 再接），案件内操作仍走案件审计链——不混表。
- **state.sqlite 位置**：`cases/{cid}/state.sqlite`，已被 `.gitignore` `/cases/` 覆盖；BUILD 任务继续不触碰它（决策 D1 原文，M3 接线时同样遵守）。

## 五、验证

- 5 个新测试组全绿并注册到 GROUPS（103 组）；
- 既有 98 组 + MCP 69 项全绿（阶段 C 后与阶段 G 各跑一次全量）；
- grep 门禁沿用并扩展（auditview/statestore 组内硬断言）：`server/` 内 `Store()` 命中 0；`server/app/` 除 `store/` 外 `duckdb.connect` 命中 0；**新增**：`sqlite3.connect` 仅出现在 `meta/repo_sqlite.py` 与 `store/state_store.py`；state_store 的 import 仅出现在 store/ 与 tests/（无写路径接线）；
- 存量改动清单核对：`git diff --stat` 中存量文件仅 `core/audit.py`（+timeline）、`run_tests.py`（+5 行注册）、`backend_api.md`（里程碑状态）；
- 端到端冒烟：建案 → BUILD v1 → dashboard 首屏有健康度 → BUILD v2 → 旧 v1 标 pending_reclaim → 持读连接期间 v1 不删 → 关闭后一个回收周期内删除 → 空案件 verify 返回 chain_ok=false → ARCHIVE 后仅剩当前版本文件。

## 六、风险与处置

| 风险 | 处置 |
|---|---|
| 跨进程读者计数不可靠导致误删活跃版本 | 租约落 meta 为唯一事实源；删除前单事务二次校验；TTL 兜底僵尸租约（偏保守方向） |
| Windows 文件句柄占用导致删除失败 | 失败仅告警记 ops_events，不重试不阻塞，下周期幂等重扫 |
| 孤儿扫描误删手工/迁移版本 | 白名单=版本历史任何记录+当前指针；`--dry-run` 模式；隔离区 7 天可人工捞回 |
| 空链红线与 REQ-G-025 测试冲突 | core 不动、API 包装层裁决（D-M2-3）；auditinteg 组必须原样全绿作为回归锚 |
| timeline() 被 tempting 扩写成写面或改既有函数 | 决策 D-M2-1 明文"仅追加只读"；code review 对照 git diff 逐行核对 |
| D1 接线诱惑（M2 就改 ActionExecutor） | 决策 D-M2-2：M2 只交骨架+方案；接线与处置端点同批 M3（有消费者才动 core） |
| W-022/异常通道与仪表盘范围蔓延 | `/anomalies` 端点明确 M4；仪表盘不含异常卡片 |
| 仪表盘诊断下钻深度不足（跳过记录） | MVP 只回诊断行+关联 ID；跳过记录深链随 M4 数据治理页细化（范围裁剪记入实施记录） |

## 七、决策点（2026-09-07 评审已确认，均采纳推荐项）

| # | 决策 | 确认结论 | 落点 |
|---|---|---|---|
| D-M2-1 | W-023 时间线读面放哪 | **core/audit.py 追加只读 `timeline()`**（守"routers 不写 SQL"，core 净增 1 个只读方法） | 阶段 C 步骤 9 |
| D-M2-2 | D1 state 接线批次 | **M2 骨架+迁移方案，M3 接线**（无消费者的 core 改动不做） | 阶段 F + 附录 A |
| D-M2-3 | W-023 AC-4 空链红线落点 | **API 包装层强制**（core REQ-G-025 语义保留，auditinteg 零回归） | 阶段 C 步骤 10 |

---

## 附录 A：D1 迁移与双写过渡期方案（M2 细化交付物，阶段 F 产出）

- **载体**：per-case `cases/{cid}/state.sqlite`（WAL），表结构 audit_chain 与 DuckDB 版**逐列同构**（seq/event_id/case_id/ontology_version/rule_version/function_version/params_hash/source_row_ids/operator/before_state/after_state/prev_hash/signature），签名算法复用 `_compute_signature` 不变——迁移后 chain_verify 逐条可验。
- **M3 接线方式**：`AuditChain` 构造参数扩一个可选 `backend`（"duckdb"缺省/"sqlite"），append/verify/timeline 按 backend 分派；ActionExecutor 由 server 注入 StateStore 适配出的 backend；本地 CLI/MCP 不传 backend，行为零变化。
- **双写过渡期**（M3 上线后至迁移完成）：处置写入同时落 DuckDB 与 state.sqlite（同一事务边界外两次 append，失败即告警不静默）；读路径（timeline/verify）以 state.sqlite 为准；迁移工具 `import_from_duckdb()` 以案件为单位一次性执行（幂等，root_hash 断言），迁移完的案件停 DuckDB 写。
- **BUILD 不触碰 state.sqlite**（决策 D1 原文）：决策与处置是"人对结论的操作"，不随分析版本重建丢失。
- **导出**（M5）：案件包导出时 `state.sqlite → audit/chain.csv`，ADR 5.2 目录结构不变；chain_ok=false 橙色告警放行逻辑随 M5 案件包批次实现。

## 八、实施记录（2026-09-07 实施完成回填）

**测试数**：新增 5 组 47 条测试——reclaim 13 / orphanscan 10 / auditview 10 / dashboard 8 / statestore 6；GROUPS 注册至 103 组；MCP 端到端 69/69 通过；全量回归 103 组全绿（含 e2e）。既有锚组 auditinteg（REQ-G-025 空链语义）/ dimecoverage / apibase / audit 原样全绿。

**core 触点核对**（对照 git diff，与计划偏差 2 处，均为追加式只读、零改既有函数）：

| 文件 | 改动 | 计划内/外 |
|---|---|---|
| core/audit.py | +`readonly()` + `timeline()`（计划写的是 +timeline 一处，readonly 为其前置——DuckDB read-only 拒绝一切 CREATE，`AuditChain(conn)` 无法在只读连接实例化） | 计划内（D-M2-1 的实现必需拆两步） |
| core/run_health.py | +`RunHealth.readonly()`（同因：RunHealth(conn) 建表 DDL 在只读连接抛 InvalidInputException；W-018 健康度横幅读面必需） | **计划外**（同类追加只读，实测驱动） |
| core/disposal.py | +`status_counts()` 模块级只读聚合 | **计划外**（计划假设"DisposalBoard.by_status 待办计数"可用，实际 Web 案件库无内存线索对象，持久化读面仅 clue_disposal_status 表） |
| run_tests.py | +5 组注册 | 计划内 |

其余既有文件零改动（core/store.py 门面化推 M3、ActionExecutor/写路径零接线、11 处无参 Store() / MCP 全部原样）。

**与计划的其他偏差**：

1. **实体裁决待办卡片降级**（W-018 AC-5 一半）：`ReviewQueue.pending()` 是管道运行期内存构造，needs_review 候选无持久化表——仪表盘 `todo.review` 输出 `available=false` + 说明，M3 处置端点接线时补真实读面。
2. **verify 端点 health.record 副作用**：只读路径经 `AuditChain.readonly` → health=None → NullRunHealth 空操作（计划预期"诊断落 run_diagnostic 与仪表盘联通"在只读连接上不成立；行为安全，无写泄漏）。
3. **SERVICE_VERSION 保持 "M1"**：升版需连带改 tests/test_api_base.py 的断言（M2 计划的存量改动清单不含该文件），随 M3 批次一并更新。
4. **W-024 埋点事件命名**：login / login_failure / logout / auth_failure（端点 401，含 missing_token、session_invalid、user_invalid 三档 reason）/ authz_failure（端点 403，非管理员访问 /audit/events）。
5. **孤儿扫描挂载点**：run_worker 启动序列单次执行 + `--orphan-dry-run` / `--orphan-ttl-days` 参数；周期随回收器间隔（`--reclaim-interval`）由 reclaim 侧承担（计划原文"随回收周期执行"由部署参数合并实现）。

**grep 门禁扩展落地**：auditview 组（server/ 无 core Store 实例化；store/ 外无 duckdb.connect；sqlite3.connect 仅 repo_sqlite.py+state_store.py）+ statestore 组（state_store 的 import 仅出现在 store/ 与 tests/——无写路径接线硬断言）。

**端到端冒烟**：见 test_version_reclaim / test_orphan_scan / test_audit_view / test_dashboard 的 TestClient 用例链（建案 → BUILD → 仪表盘/审计 → 切版本 → 回收）。
