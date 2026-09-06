# 孙武侦查官 Web 平台 · 后端运行方式与 API 设计

**版本** v1.0 ｜ **日期** 2026-09-07
**关联** [req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)（REQ-W-001~031）、[adr.md](file:///d:/dev/inves_duckdb/.trae/documents/web/adr.md)（ADR-001）、[ui_v1.3.md](file:///d:/dev/inves_duckdb/.trae/documents/web/ui_v1.3.md)（26 界面）、[ui_imp.md](file:///d:/dev/inves_duckdb/.trae/documents/web/ui_imp.md)（前端选型）
**范围** 后端进程拓扑、读写路径、任务与状态机、约 60 个 REST/SSE 端点契约、横切约定、错误码、项目结构
**定位** 本文是 ADR-001 的下游补充。ADR 已定存储架构（案件级版本化文件 + 写队列串行 + 只读并发，spike H1-H7 已验证）与元数据层选型；ADR 附录 B 明确声明"未覆盖 API 设计、认证授权体系"，本文补齐这部分。

---

# 第一部分：现状与缺口

## 1.1 已具备的能力面（内核侧）

后端不是从零开始，现有内核已提供可编排的能力：

| 能力 | 现有载体 | Web 化方式 |
|---|---|---|
| 语义层只读查询 | `OntologyReadGateway`（[core/gateway.py](file:///d:/dev/inves_duckdb/core/gateway.py)） | API 直接调用，PolicyEngine 遮蔽后出参 |
| 只读函数计算 | `FunctionExecutor` + functions.json 白名单（[core/functions.py](file:///d:/dev/inves_duckdb/core/functions.py)） | 规则工坊/庙算端点薄封装 |
| 写操作唯一入口 | `ActionExecutor` 四步校验（[core/action_executor.py](file:///d:/dev/inves_duckdb/core/action_executor.py)） | 处置/裁决端点一律经它 |
| 身份与权限 | `AccessContext`（frozen，operator 必填，isolated 拒网）+ PolicyEngine（[core/access.py](file:///d:/dev/inves_duckdb/core/access.py)、[core/policy.py](file:///d:/dev/inves_duckdb/core/policy.py)） | 会话 → AccessContext 映射 |
| 审计链 | `AuditChain`（[core/audit.py](file:///d:/dev/inves_duckdb/core/audit.py)） | 审计端点读链 + verify |
| 健康度/诊断 | run_health / compliance / sensitive_scan / data_freshness / unit_scan | 仪表盘端点聚合 |
| 人审队列 | `ReviewQueue` / proposal（[core/review.py](file:///d:/dev/inves_duckdb/core/review.py)） | 实体裁决端点 |
| 线索/处置 | DisposalBoard 状态机（5 动作，file 为 human 终态） | 线索详情/处置看板端点 |
| 任务化雏形 | MCP 13 工具（scan_anomaly / run_pipeline / clue_transition…，[scripts/mcp_server.py](file:///d:/dev/inves_duckdb/scripts/mcp_server.py)） | 能力清单参照，API 是其 HTTP 化 |

**禁令沿用**（AGENTS.md 三条禁令在 API 层同样有效）：API 处理函数不得自写业务 SQL（走 Gateway/Function）；不得把原始明细搬进响应体（只要溯源 ID 与聚合，明细分页且经遮蔽）；不得下定性结论、不得置"已立案"（file 动作的状态机强制）。

## 1.2 开工前必须定的三件事

1. **进程拓扑**：API 进程与 Worker 进程分置（见第二部分）。
2. **高频小写通道**：处置动作、人审裁决、审计链追加是"短而频"的写；版本化文件模型是为"长而稀"的 BUILD/SCAN 设计的——ADR 2.7 把审计链放在 DuckDB，但 spike H1 证明读者持句柄时写者打不开同一文件，每次处置都复制一个 GB 级 DuckDB 新版本不现实。**这是 ADR 的真实缺口**，本文给出补充方案（2.4 节）。
3. **API 契约**：26 个页面到端点的映射（第三部分）。

---

# 第二部分：后端运行方式

## 2.1 进程拓扑（单机私有化部署）

```
┌────────────────────────────────────────────────────────────┐
│  Nginx：托管 SPA dist/（全部资源本地化，无 CDN）              │
│         + 反向代理 /api（HTTP）、/sse（SSE，关闭缓冲）         │
└──────────────┬───────────────────────────┬─────────────────┘
               │                           │
      ┌────────▼─────────┐        ┌────────▼─────────┐
      │  API 进程 ×1~4    │        │  Worker 进程 ×N   │
      │  FastAPI/uvicorn │        │  （独立进程）      │
      │  · 同步短请求     │  入队   │  · 轮询任务表     │
      │  · 读=只读连接    │───────▶│  · 案件级 FIFO    │
      │    请求作用域     │        │  · 全局并发上限 N  │
      │  · 写=只入队      │◀───────│  · 唯一数据面写者  │
      │  · 不跑长任务     │ 进度表  │  · 启动扫孤儿文件  │
      └────────┬─────────┘        └────────┬─────────┘
               │                           │
      ┌────────▼───────────────────────────▼─────────────┐
      │ 元数据库（平台级，单实例）：                         │
      │   MVP：SQLite-WAL；生产：Postgres                  │
      │   用户/租户/会话/案件登记/任务表/版本指针/平台审计    │
      └───────────────────────────────────────────────────┘
      ┌───────────────────────────────────────────────────┐
      │ 案件数据目录 cases/{cid}/：                         │
      │   v{N}.duckdb        不可变版本文件（BUILD/SCAN 产出）│
      │   cold/*.parquet     L3 冷层                        │
      │   state.sqlite       业务状态库（审计/决策/处置，2.4）│
      │   ontology_snapshot/ 建案时锁定的 13 声明文件快照     │
      └───────────────────────────────────────────────────┘
```

### 关键决策

- **Worker 独立进程**，不放在 API 进程内。理由：
  1. 崩溃隔离——Worker 被 kill -9 不影响查询服务（REQ-W-009 孤儿扫描在 Worker 启动时执行）；
  2. 长任务（秒~分钟）不占 API 事件循环；
  3. 可独立重启、独立扩缩（N 可配）。
  **API 永远不执行 BUILD/SCAN/IMPORT/EXPORT，只入队。**
- **框架选 FastAPI**：pydantic 模型与 ontology 声明 JSON 校验同构；SSE 原生支持；自动 OpenAPI 契约（前端可直接生成 TS client）；DuckDB 为同步驱动，读查询经 `anyio.to_thread` 线程池执行，不阻塞事件循环。
- **只读连接请求作用域**：请求结束即关闭归还（ADR 3.4 硬要求——读者不排空，写者永远拿不到锁）。用依赖注入管理连接生命周期；跨案件 ATTACH 连接按案件组合缓存复用。
- **Nginx 职责**：静态托管 + `/api`、`/sse` 反代；SSE 需 `proxy_buffering off`、`proxy_read_timeout` 拉长。

## 2.2 读路径（高频、毫秒级）

```
请求进入
  → 会话鉴权中间件：解析 token → 用户/角色/密级/租户
      （operator 强制取会话；请求体中的 operator 字段一律忽略，REQ-W-024）
  → 租户 + 案件 membership 校验：在打开任何案件文件之前完成
      （REQ-W-030 AC-2：跨租户直接构造 case_id 访问被拒；
        REQ-W-027 AC-2：跨案件部分授权在 ATTACH 前整体拒绝）
  → store_factory.for_case(cid, mode="read", access=ctx)
      按版本指针打开当前 vN（read_only=True）
  → OntologyReadGateway / FunctionExecutor（只读，禁止自写 SQL）
  → PolicyEngine 字段遮蔽（如 310****1234）在序列化前完成
  → 响应返回，连接关闭
```

空态/无权限态区分（ui_v1.3 通用五态）：**无权限不得伪装成空态**——403 与"资源存在但为空"是两种响应，前端不得渲染为相同视觉。

## 2.3 数据面写路径（长任务：IMPORT/BUILD/SCAN/EXPORT/ARCHIVE）

```
API 端：
  校验 → 计算 idempotency_key → INSERT task 表
      （(case_id, task_type, idempotency_key) 唯一约束兜底幂等，
        冲突时返回既有任务，不消耗 Worker，REQ-W-012 AC-4）
  → 返回 202 + task_id

Worker 端（两级队列，ADR 2.3）：
  每轮：
    1. 运行中任务数 ≥ N → 等待
    2. 找出所有"有 PENDING 且无 RUNNING"的案件
    3. 按 created_at 取最老的 PENDING 任务派发（老任务优先，防饿死）
  执行：
    → 写新版本文件 cases/{cid}/v{N+1}.duckdb（不动 vN）
    → 成功：元数据层原子切版本指针；旧版本标记 pending_reclaim
    → 失败：删半成品，vN 完好；语义性错误（缺列/权限/声明非法）不重试，
            瞬时故障指数退避重试（delay = base × 2^retry_count，≤ max_retries）
  进度：
    → 写 task.progress_pct / progress_stage / progress_detail
    → 阶段边界回报 + 最小间隔 500ms 节流
    → SSE 通道：MVP 由 SSE 端点轮询 task 表（500ms）；上 Postgres 后换 LISTEN/NOTIFY
```

任务状态机（ADR 2.4）：

```
PENDING → RUNNING → SUCCEEDED
             │    ↘ FAILED（retry_count < max_retries → 回 PENDING）
             └─→ CANCELLED
```

版本回收（REQ-W-008/009）：切换后旧版本标 `pending_reclaim`，读者引用计数归零后删除（跨平台一致 + 空间实际回收 + 崩溃可恢复，**不**依赖"unlink 后句柄可读"的 POSIX 行为）；Worker 启动时扫描孤儿版本文件（有文件无任务记录），移入隔离区保留 7 天后清理，扫描结果进健康度。

## 2.4 业务面写路径（ADR 补充项）⭐

处置（verify/reset/exclude/confirm/file）、人审裁决、知识包断言维护这类写**短而频**，走"复制 DuckDB 新版本"不可行。建议载体分离：

| 数据 | 载体 | 写者 | 理由 |
|---|---|---|---|
| 分析数据（obj_*/lnk_*、线索产物、Parquet 冷层） | 版本化 v{N}.duckdb，仅 BUILD/SCAN 产版本 | Worker 长任务 | 不可变、可整体导出、spike H1-H4 已验证 |
| **审计链、决策对象（obj_decision/lnk_decision_for）、处置状态、人审裁决结果、知识包断言** | **per-case `cases/{cid}/state.sqlite`（WAL 模式）** | Worker（经同一案件 FIFO 队列） | SQLite WAL 原生支持"多读者 + 单写不互斥"；文件 KB~MB 级，复制/导出廉价；与 BUILD 任务同队列串行，无锁竞争 |
| 平台事件（登录/登出/鉴权失败/跨案件查询记录） | 元数据库平台审计表 | API 进程 | 跨案件事件不属于任何单一案件 |

设计要点：

- 业务写**仍然入队**（API 不直接写 state.sqlite），复用任务表的幂等、审计、operator 绑定机制；但这类任务走"快速通道"（秒级完成，不产 DuckDB 版本）。
- ActionExecutor 增加 state 后端适配：现状审计链写 DuckDB `audit_chain` 表（[core/audit.py](file:///d:/dev/inves_duckdb/core/audit.py)），需迁移为写 state.sqlite 或双写；**案件包导出时审计链从 state.sqlite 导出为 audit/chain.csv**（ADR 5.2 目录结构不变）。
- BUILD 新版本时不触碰 state.sqlite——决策与处置是"人对结论的操作"，不随分析版本重建而丢失（与现有"语义层重建不清决策"原则一致）。

> **决策 D1（2026-09-07 已定）**：采纳 per-case `state.sqlite`（WAL）方案。此项偏离 ADR 2.7"审计链载体为 DuckDB"的表述，经评审确认——不解决它，处置/裁决端点在版本化架构下没有可行写路径。落地要求：
> - ActionExecutor/AuditChain 增加 state 后端适配（写 state.sqlite，BUILD 新版本不触碰它）；
> - 案件包导出时审计链从 state.sqlite 导出为 `audit/chain.csv`，ADR 5.2 目录结构不变；
> - 现有 DuckDB `audit_chain` 表的数据迁移与双写过渡期方案在 M2 里程碑细化。

## 2.5 元数据层（REQ-W-004/005/030）

- **MVP 用 SQLite-WAL**（单机私有化、零运维、免 Docker）——**决策 D2（2026-09-07 已定）**；生产切 Postgres（任务表 SQL 已按 PG 方言写就，见 ADR 2.2，BIGSERIAL/JSONB/TIMESTAMPTZ 在 SQLite 上以 INTEGER/TEXT/TEXT 映射，经 Repository 层隔离方言）。
- 元数据库不可用时返回明确 5xx 错误，**不得静默降级为本地文件存储**（REQ-W-004 AC-4）。
- 核心表：`tenant`、`user`、`session`、`case`（含 tenant_id、snapshot_version、当前版本指针、状态）、`task`（ADR 2.2 结构）、`version_pointer`、`platform_audit`、`case_membership`。
- 建案即锁快照：复制 13 个声明文件至 `cases/{cid}/ontology_snapshot/`，记录 snapshot_version；案件所有分析操作读快照而非共享本体（REQ-W-005）；快照文件篡改可经哈希校验发现。

---

# 第三部分：API 接口清单

**约定**：

- 基址 `/api`；案件作用域路径为 `/api/cases/{cid}/...`，每请求过租户 + 密级校验。
- 🔒 = 写操作（operator 强制取会话，经 ActionExecutor/任务队列）；⚡ = 异步任务（返回 202 + task_id，进度走 SSE）；其余为同步只读。
- pack 作用域路径 `/api/packs/{pack}/...`：平台共享 pack 对所有租户可见，租户级 pack 仅本租户可见（REQ-W-030 AC-6）。

## A. 认证与平台（页面 23a/25；W-024/030）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 账号 + 密码 + 双因子；失败统一报错（红线八：不泄露账号存在性）；成功记平台审计 |
| POST | `/api/auth/logout` | 记平台审计 |
| GET | `/api/auth/me` | 当前用户：operator/role/clearance/tenant/可访问案件 |
| GET | `/api/settings/queue` | Worker 并发上限、max_retries、队列积压 |
| PUT | `/api/settings/queue` 🔒 | 仅管理员；非管理员返回 403 + 只读原因（页面 25 只读态） |
| GET | `/api/settings/resources` | max_rows、查询超时、存储限额与占用 |
| PUT | `/api/settings/resources` 🔒 | 同上，管理员限定 |
| GET | `/api/settings/health` | 队列积压、孤儿版本数、元数据层状态、Worker 心跳 |

## B. 案件与上下文（页面 23b；W-005/030）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cases?status=&q=` | 案件门户：仅本租户；卡片含 case_name、snapshot_version、状态、待办计数 |
| POST | `/api/cases` 🔒 | 建案：选 pack → 锁定快照 → 初始化案件目录 |
| GET | `/api/cases/{cid}` | 案件上下文：当前版本指针、状态、pack 快照版本、数据新鲜度 |
| GET | `/api/cases/{cid}/summary` | 跨案件待办汇总：待处置/待裁决/超期/异常线索计数 |
| POST | `/api/cases/{cid}/archive` 🔒⚡ | 归档：版本压实仅留最终版（REQ-W-008 AC-5） |

## C. 任务中心（页面 24；W-006~009）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` 🔒 | 入队 IMPORT/BUILD/SCAN/EXPORT/RESCAN/ARCHIVE；body 含 case_id/task_type/params/reason；幂等键服务端补全；202 + task_id |
| GET | `/api/tasks?case_id=&status=&type=&page=` | 五统计卡（全部/运行中/待处理/失败/今日完成）+ 运行中区 + 历史表（含版本变化、丢弃行数） |
| GET | `/api/tasks/{tid}` | 任务详情：状态机、progress、error_code/error_message、result |
| GET | `/api/tasks/{tid}/events` | **SSE** 进度流：stage/pct/stage_label/detail（ADR 2.5 结构），断线重连从 Last-Event-ID 续 |
| POST | `/api/tasks/{tid}/cancel` 🔒 | PENDING/RUNNING → CANCELLED |

## D. 治理仪表盘（页面 01；W-018/022）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cases/{cid}/dashboard` | 健康度横幅（healthy/degraded + warning/info 计数）、声明覆盖/实证覆盖双环、线索待办、实体裁决待办 |
| GET | `/api/cases/{cid}/diagnostics?kind=&severity=` | 全部诊断类别（零命中/跳过/覆盖缺口/版本锚定/脏值/缺列/新鲜度/单位/合规/敏感…） |
| GET | `/api/cases/{cid}/diagnostics/{did}` | 下钻：关联规则/数据源/跳过记录 |
| GET | `/api/cases/{cid}/anomalies` | 异常线索通道：按主体聚合、级别恒"待核实"、携带 diagnostic_ids；**不参与五间交叉等级计算**（REQ-W-022 AC-4 红线） |

## E. 数据接入（页面 02/07/08；W-010/011/012）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cases/{cid}/uploads` | multipart 上传 CSV/Excel/Parquet/JSON/SQLite → 返回 upload_id、文件名、内容哈希（SHA-256）、行数 |
| POST | `/api/cases/{cid}/uploads/{uid}/analyze` | 自动识别列、类型推断、数据元匹配建议、可选属性缺列预判 |
| GET | `/api/cases/{cid}/datasources` | 已注册数据源与列映射 |
| PUT | `/api/cases/{cid}/datasources/{dsid}` 🔒 | 保存映射：源列→已声明属性、清洗规则挂接；生成 bindings 经同一 loader 强校验（未声明属性硬失败） |
| POST | `/api/cases/{cid}/import` 🔒⚡ | 幂等导入：指纹=文件名+内容哈希+行数；重复在任务创建阶段拦截；内容同名不同放行 |
| GET | `/api/cases/{cid}/profiles` | 数据画像：属性画像表、书写变体、扣分明细 |
| GET | `/api/cases/{cid}/de-recommendations` | 接入建议：待核实红条、四要素卡片、证据面板 |
| POST | `/api/cases/{cid}/de-recommendations/{rid}/decide` 🔒 | 采纳/驳回建议，记审计 |

## F. 数据治理配置（页面 18-22；REQ-D-001~022）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/packs/{pack}/data-elements` | 数据元标准分类树 + 列表 + 代码表 |
| PUT | `/api/packs/{pack}/data-elements` 🔒 | 保存经 loader 校验 |
| GET | `/api/packs/{pack}/etl-pipeline` | ETL 管道：清洗链、on_cast_error 三态、null_policy、dedup_key、复合列定义 |
| PUT | `/api/packs/{pack}/etl-pipeline` 🔒 | 同上 |
| POST | `/api/packs/{pack}/etl-pipeline/validate` | 1:1 映射校验：冲突列定位（页面 20 阻断态），不写盘 |
| POST | `/api/cases/{cid}/quality-checks` 🔒⚡ | 触发合规/敏感扫描/新鲜度/单位检查 |
| GET | `/api/cases/{cid}/quality-checks/latest` | 质量总览 + 值域合规表 + 敏感扫描 + 新鲜度/单位 |
| GET | `/api/cases/{cid}/quarantine?reason=` | 隔离区：四类统计条 + 隔离行数据（cast_error/null_value/dedup/其他）；零隔离显式返回空统计（页面 22 零隔离态） |
| GET | `/api/cases/{cid}/clean-trace` | 清洗留痕、clean_stats |

## G. 模型与规则配置（页面 03/12/13/14/17；W-013~017/025/031）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/packs/{pack}/objects` | 对象类型列表（pk/kind/name_property/properties） |
| PUT | `/api/packs/{pack}/objects` 🔒 | 模型设计器保存：值类型仅 TYPE_SQL 5 种、kind=entity/event 提示、经 loader 强校验 |
| GET/PUT | `/api/packs/{pack}/links` 🔒 | 链接类型：from_obj/to_obj 必须引用已声明对象、间类仅五间 |
| POST | `/api/packs/{pack}/validate` | 声明全包校验（未知名硬失败，不放宽） |
| GET | `/api/packs/{pack}/rules` | 规则列表：rule_text + function + params 双轨、启用状态 |
| PUT | `/api/packs/{pack}/rules/{rid}` 🔒 | 编辑/启停/调参；function 仅可选 functions 白名单；string 参数必须 enum 白名单；调阈值可触发增量 RESCAN |
| GET | `/api/packs/{pack}/functions` | 函数目录（name/签名/参数类型/枚举） |
| POST | `/api/packs/{pack}/rules/draft` 🔒 | LLM 辅助写作：**只允许返回 rule_text**；响应含 function/params 字段一律拦截告警（W-025 AC-1）；产物默认"待核实"；脱敏在 LLM 调用前执行 |
| GET | `/api/packs/{pack}/policies` | 对象/链接策略 + 字段遮蔽矩阵 |
| PUT | `/api/packs/{pack}/policies` 🔒 | 保存即生效（无需重建语义层）；未声明对象=拒绝（fail-closed，矩阵格子不留白） |
| GET/PUT | `/api/packs/{pack}/views` 🔒 | 角色视图：引用列必须已声明；视图不绕过 PolicyEngine |
| GET | `/api/packs/{pack}/knowledge` | 断言列表 + valid_until + 敏感地点白名单 |
| POST/PUT | `/api/packs/{pack}/knowledge` 🔒 | 断言增改/停用；过期断言扫描自动排除；变更进审计；导出时标 sensitive |
| POST | `/api/escape-hatch/generate` 🔒 | 代码桩生成：函数签名 + 输入输出契约 + 测试骨架 + 注册点说明（四类扩展：py Function/值类型/清洗规则/Action 副作用） |
| GET | `/api/escape-hatch/stats` | 逃生舱触发统计（哪类需求反复出现，飞轮报表） |

## H. 研判（页面 04/05/09/10/11；W-019/020/021）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cases/{cid}/clues?level=&dimension=&jian=&subject=&status=&page=&page_size=` | 线索列表：筛选/排序/分页，**只读已产出结果，不触发重扫**（W-019 AC-6）；含优先级分数与排序依据 |
| GET | `/api/cases/{cid}/clues/{clue_id}` | 详情：五间横条、source_rows 溯源（回溯 Parquet 行 ID）、merged_from 合并来源 |
| GET | `/api/cases/{cid}/clues/suppressed` | 被抑制记录（suppressed_log，不删除仅移出主列表） |
| POST | `/api/cases/{cid}/clues/{clue_id}/actions` 🔒 | 处置动作 verify/reset/exclude/confirm/file：全部经 ActionExecutor 四步校验；file 须 human 角色 + legal_basis + 仅"已固证"可迁移；operator 占位名（system/ai/assistant/agent:*）拒绝；落持久审计链 |
| GET | `/api/cases/{cid}/disposal/board` | 处置看板：五泳道分组、停留天数、超期标记 |
| GET | `/api/cases/{cid}/review/queue` | 人审队列：needs_review=True 候选全量（重名/组织层级/绰号映射） |
| GET | `/api/cases/{cid}/review/{rid}/evidence` | 实体裁决：双方属性三列对比、差异行、相似度依据 |
| POST | `/api/cases/{cid}/review/{rid}/decision` 🔒 | 合并/驳回 + 理由，进审计；驳回后不重复出现；**系统任何模式下不自动合并 needs_review 候选**（W-021 AC-5 红线） |
| GET | `/api/cases/{cid}/graph` | 知识图谱：节点/边/类型分色/双轨徽章；Ladybug 不可用时返回降级标志（前端渲染关系表格） |
| GET | `/api/cases/{cid}/hypotheses` | 庙算工作台：假设列表、五间热力矩阵、候补池（虚线隔离）、受限假设灰显原因 |

## I. 审计（页面 06；W-023/024）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cases/{cid}/audit?operator=&from=&to=&action=&clue_id=&page=` | 审计链时间线：操作人/状态迁移/法定依据/本体版本号；只读，不可改不可删 |
| POST | `/api/cases/{cid}/audit/verify` | 完整性自检：chain_ok/expected_count/actual_count/broken_links；**空链（0 条）不得返回 chain_ok=true**（W-023 AC-4 红线）；处置事件数与非待处置线索数交叉比对 |
| GET | `/api/audit/events?tenant=` | 平台审计：登录/登出/鉴权失败/跨案件查询（管理员） |

## J. 跨案件（页面 15；W-026/027）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cross-case/query` 🔒 | body：case_ids[]、sql/function 调用、reason；**全有或全无鉴权——任一案件无权限，整体在 ATTACH 前拒绝**；ATTACH 一律 READ_ONLY；强制 max_rows 上限与超时；禁 DDL/DML；查询本身进审计（案件列表 + 原文 + reason） |
| GET | `/api/cross-case/history` | 本用户跨案件查询记录 |

## K. 案件包（页面 16；W-028/029）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cases/{cid}/package/export` 🔒⚡ | 导出任务：版本压实 → 审计链校验冻结 → 打 manifest（逐文件 SHA-256）→ README；chain_ok=false 时橙色告警但允许继续且醒目标记；case_knowledge.json 标 sensitive |
| GET | `/api/packages/{task_id}/download` | 导出完成后下载包 |
| POST | `/api/packages/verify` | 上传包校验：format → 逐文件 SHA-256 → 13 声明齐全且 schema_version 一致 → 审计链 root_hash → DuckDB 只读打开；任一不符即失败 |
| POST | `/api/packages/import` 🔒⚡ | 校验通过后 init_pack(from_pack=) 导入；导入后案件可正常查询 |

**合计约 60 个端点。**

---

# 第四部分：横切约定

## 4.1 鉴权与会话

- 认证：Authorization Bearer token；会话存元数据库，支持超时失效（REQ-W-030 AC-4）；超时后任何案件数据请求 401。
- **operator 绑定**（REQ-W-024）：operator/role/clearance/tenant 一律取自服务端会话，请求体中的 operator 字段忽略；非 system 会话 operator 与 AccessContext 不一致直接拒绝；审计链 operator 与登录记录可一一对应。
-  AccessContext 构造：`AccessContext(operator=会话用户, role=会话角色, clearance=会话密级, network="web")`；LLM 相关路径 network 判定不变（isolated 拒网默认）。

## 4.2 响应信封

```json
// 成功
{ "ok": true, "data": { ... }, "data_version": 7 }
// 失败
{ "ok": false, "error": { "code": "PERMISSION_DENIED", "message": "…", "detail": { ... } } }
// 列表
{ "ok": true, "data": { "items": [ ... ], "total": 42, "page": 1, "page_size": 20 } }
```

- `data_version`：案件读响应携带当前 DuckDB 版本号；前端 SSE 收到版本切换事件后使 TanStack Query 缓存失效重取。
- HTTP 状态：200 成功 / 202 已入队 / 400 参数错误 / 401 未认证 / 403 无权限（含 fail-closed 拒绝）/ 404 不存在 / 409 冲突（幂等键冲突返回既有任务）/ 422 声明校验失败 / 503 元数据层不可用。

## 4.3 错误码表

| code | HTTP | 含义 |
|---|---|---|
| `UNAUTHENTICATED` | 401 | 无 token / 会话超时 |
| `AUTH_FAILED` | 401 | 登录失败（统一文案，不区分账号/密码/双因子） |
| `PERMISSION_DENIED` | 403 | 角色/密级/租户/案件 membership 拒绝（fail-closed） |
| `CROSS_CASE_PARTIAL_DENIED` | 403 | 跨案件部分授权，整体拒绝（ATTACH 前） |
| `NOT_FOUND` | 404 | 案件/线索/任务不存在（跨租户探测同样返回 404，不返回 403，避免存在性泄漏） |
| `IDEMPOTENCY_CONFLICT` | 409 | 幂等键命中，返回既有任务 |
| `STATE_MACHINE_REJECTED` | 409 | 处置状态迁移非法（如非"已固证"→file） |
| `DECLARATION_INVALID` | 422 | 声明校验失败（未知名/缺必填列/枚举非法），附定位 |
| `COLUMN_MISSING_REQUIRED` | 422 | 必填属性缺列硬失败 |
| `OPERATOR_MISMATCH` | 403 | operator 与会话不一致/占位名 |
| `LEGAL_BASIS_REQUIRED` | 422 | file 动作缺法定依据 |
| `METADATA_STORE_UNAVAILABLE` | 503 | 元数据层不可用（不静默降级） |
| `TASK_FAILED` | 200（任务内） | 任务失败，error_code/error_message 见任务详情 |
| `NO_MATCHING_FUNCTION` | 422 | 无匹配函数（不生成自由 SQL） |

## 4.4 幂等

- 所有 🔒 与 🔒⚡ 端点支持 `Idempotency-Key` 请求头；任务类以 `(case_id, task_type, idempotency_key)` 数据库唯一约束兜底。
- 导入指纹 = 文件名 + 内容 SHA-256 + 行数；BUILD 天然幂等可重跑；EXPORT/ARCHIVE 天然幂等。

## 4.5 遮蔽与数据最小化

- 字段遮蔽（如手机号 310****1234）在 API 序列化前由 PolicyEngine 完成，**原始敏感列不进入响应体**。
- 列表接口默认不返回明细：线索列表只给聚合字段，溯源明细在详情端点分页获取。
- LLM 辅助端点：脱敏闸门在调用 LLM 前执行，敏感字段不出境；LLM 提案永不自动生效。

## 4.6 SSE 约定

- 端点：`GET /api/tasks/{tid}/events`，`Content-Type: text/event-stream`。
- 事件结构：`event: progress` / `event: succeeded` / `event: failed`；`data` 为 ADR 2.5 的进度 JSON；支持 `Last-Event-ID` 断线重放。
- Nginx 需关闭缓冲。MVP 服务端 500ms 轮询 task 表推送，Postgres 阶段升级为 NOTIFY 触发。

---

# 第五部分：建议的后端项目结构

```
server/
├── app/
│   ├── main.py                  # FastAPI 入口、中间件、路由挂载
│   ├── deps.py                  # 依赖注入：会话、AccessContext、只读连接（请求作用域）
│   ├── auth/                    # 登录/会话/token/双因子
│   ├── meta/                    # 元数据层 Repository（SQLite/Postgres 方言隔离）
│   │   ├── models.py            # tenant/user/session/case/task/version_pointer/audit
│   │   └── repo_sqlite.py / repo_postgres.py
│   ├── store/
│   │   ├── factory.py           # store_factory.for_case / for_cross_case（REQ-W-003）
│   │   ├── backend_case.py      # CaseStore(mode=read/write)
│   │   ├── backend_cross.py     # CrossCaseStore（ATTACH READ_ONLY，write 抛 UnsupportedOperation）
│   │   └── state_store.py       # per-case state.sqlite（审计/决策/处置，2.4）
│   ├── worker/
│   │   ├── pool.py              # 两级队列调度（案件 FIFO + 全局并发 N）
│   │   ├── tasks/               # import_task / build_task / scan_task / export_task / archive_task
│   │   ├── retry.py             # 指数退避、语义错误不重试
│   │   ├── reclaim.py           # 版本延迟回收
│   │   └── orphan_scan.py       # 启动孤儿文件扫描（REQ-W-009）
│   ├── routers/                 # 按第三部分 A~K 分组
│   │   ├── auth.py cases.py tasks.py dashboard.py ingest.py
│   │   ├── governance.py model_rules.py research.py audit.py
│   │   ├── cross_case.py packages.py settings.py
│   ├── schemas/                 # pydantic 请求/响应模型（与 OpenAPI 契约同源）
│   └── sse.py                   # 进度事件推送
├── tests/                       # API 层测试：红线用例优先（空链/全有或全无/operator 绑定/needs_review）
└── pyproject.toml
```

**边界纪律**：routers 只做"鉴权 → 构造 AccessContext → 调内核（Gateway/FunctionExecutor/ActionExecutor）或入队 → 遮蔽 → 序列化"，不含业务逻辑、不写 SQL；业务逻辑全部留在 `core/`。

---

# 第六部分：与实施批次的关系

API 契约可先行冻结，但端点上线依赖地基顺序（req.md 实施路径）：

| 后端里程碑 | 依赖批次 | 可交付端点 |
|---|---|---|
| M1 地基 | 批 1-3（W-001/002/003/004/005/006/007） | 认证、案件门户、任务中心、元数据/版本指针/队列 |
| M2 可信首页 | 批 4（W-008/009/018/023/024） | 治理仪表盘、审计链与自检、operator 绑定 |
| M3 研判主流程 | 批 5（W-010/012/014/019/020/025） | 线索列表/详情/处置、数据接入、规则工坊、LLM 守卫 |
| M4 配置与人审 | 批 6（W-011/013/015/016/017/021/022） | 模型设计器、权限遮蔽、知识包、视图、实体裁决、异常通道、治理页 18-22 |
| M5 高级能力 | 批 7-9（W-026→027、028→029、031） | 跨案件查询、案件包导入导出、代码逃生舱 |

**顺序硬约束**：S5（版本化文件）先于跨案件 ATTACH（spike H7：无版本化时 ATTACH 锁死写）。

---

# 第七部分：决策记录

以下四项已于 **2026-09-07** 评审确认，作为后端开发基线：

| 编号 | 决策点 | 结论 | 落地影响 |
|---|---|---|---|
| D1 | 审计链/决策/处置状态载体 | **采纳 per-case `state.sqlite`（WAL）**，偏离 ADR 2.7"DuckDB 承载审计链"表述 | ActionExecutor/AuditChain 增加 state 后端；导出时 state → audit/chain.csv；DuckDB 旧链数据迁移方案 M2 细化（见 2.4） |
| D2 | 元数据层 MVP 选型 | **SQLite-WAL 起步**，Repository 层隔离方言，Postgres 为生产切换项 | meta/ 目录双实现：repo_sqlite.py / repo_postgres.py（见 2.5、第五部分） |
| D3 | Worker 与 API 部署形态 | **私有化单机同机、进程分置**；Worker 并发数 N 可配 | 交付物含独立启动入口（api / worker 两个命令）与 systemd/进程编排配置；Worker 崩溃不影响 API |
| D4 | SSE 进度源 | **MVP 轮询 task 表（500ms 节流）**，Postgres 阶段升级 LISTEN/NOTIFY | sse.py 先实现轮询版，接口形态不变，后续无感切换（见 2.3、4.6） |

---

*本文为后端开发输入文档，不涉及真实办案数据。端点清单与 REQ-W-001~031、ui_v1.3 的 26 界面一一对应，新增界面时须同步增补端点。*
