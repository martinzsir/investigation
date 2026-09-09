# MVP-5 · 全量 实施方案（后端契约核对版）

> 版本 v1.0 ｜ 日期 2026-09-09
> 依据：`MVP迭代计划.md` v1.5（MVP-5 第 17–18 周）· `前端需求清单.md` v1.8（FE-P-010/011/015/016/017/023b/025、FE-C-020/021/022、FE-T-008/014）· `前端需求分组排序.md` v1.5
> 核对方式：逐行核对 `server/app/routers/` 实际路由代码与 `server/app/*_view.py`、`server/app/worker/`、`server/app/store/backend_cross.py`（非 backend_api.md 文本）
> 状态：**七页端点代码全部已实现（清单 v1.8 的 ⛔ 待补标注已过时）；但有 6 处缺口/不一致 + 3 项依赖决策需用户确认（第五节），确认前不开工**

---

## 一、总体结论

**后端就绪度：MVP-5 七页所需端点在代码中全部存在**（M5/M6 批次已落地），需求清单 v1.8 中 4 处 ⛔ 标注（graph、hypotheses、settings/*、cases/{cid}/summary）均已实现：

- 知识图谱：`GET /cases/{cid}/graph` ✅（[graph.py](file:///d:/dev/inves_duckdb/server/app/routers/graph.py#L18-L43)，未 BUILD 返 `available:false` 空图不 500）
- 庙算工作台：`GET /cases/{cid}/hypotheses` ✅（[research.py](file:///d:/dev/inves_duckdb/server/app/routers/research.py#L47-L69)，派生口径 `derived:true`）
- 跨案件查询：`POST /cross-case/query` + `GET /cross-case/history` ✅（[cross_case.py](file:///d:/dev/inves_duckdb/server/app/routers/cross_case.py#L81-L148)，全有或全无鉴权在 ATTACH 前）
- 案件包：export/download/verify/import 四端点 ✅（[package.py](file:///d:/dev/inves_duckdb/server/app/routers/package.py#L38-L127)）
- 代码逃生舱：generate/stats ✅（[escape_hatch.py](file:///d:/dev/inves_duckdb/server/app/routers/escape_hatch.py#L32-L70)）
- 案件门户：`GET /cases`、`POST /cases`、`GET /cases/{cid}`、**`GET /cases/{cid}/summary`（清单标 ⛔，已实现）**、`POST /cases/{cid}/archive` ✅（[cases.py](file:///d:/dev/inves_duckdb/server/app/routers/cases.py#L52-L142)）
- 系统设置：**`GET/PUT /settings/queue|resources`、`GET /settings/health`（清单标 ⛔ 全部待补，均已实现）**，另有 policies-thresholds/snapshots/features 三个配置中心端点 ✅（[settings.py](file:///d:/dev/inves_duckdb/server/app/routers/settings.py#L112-L210)）

通用事实（沿用 MVP-4 结论）：

- 全局前缀 `/api/v1`，响应信封 `ok({...}, data_version=...)`；写权限门槛配置类为 admin（`is_admin=1` 或 role=system），案件类写操作多为登录即可（archive 需 clearance≥2）。
- 角色 rank：见习 0 / 正兵 1 / 偏将 2 / 主办 3 / human 4；**`is_admin` 是独立于 role/clearance 的平台管理员标志**（[models.py](file:///d:/dev/inves_duckdb/server/app/meta/models.py#L61)）。
- 前端侧栏 nav key 已预留：`miaosuan`/`graph`（研判中心组）、`cross-case`/`package`/`escape`（高级工具组），当前均落 `/c/:section` 占位页（[sections.ts](file:///d:/dev/inves_duckdb/frontend/src/nav/sections.ts#L23-L69)）；平台页 `/cases`、`/settings` 当前为 PlaceholderView（[router/index.ts](file:///d:/dev/inves_duckdb/frontend/src/router/index.ts#L29-L32)）。
- 前端访问层无 graph/hypotheses/crossCase/package/escapeHatch/settings 封装；[cases.ts](file:///d:/dev/inves_duckdb/frontend/src/api/endpoints/cases.ts) 仅有 `list()`/`dashboard()`，缺 create/summary/archive。
- FE-C-020 PolicyGate 组件已就位（[PolicyGate.vue](file:///d:/dev/inves_duckdb/frontend/src/components/common/PolicyGate.vue)）；**FE-C-021 OntologyVersionTag、FE-C-022 DualTrackCompare 组件尚不存在**，本轮新建。
- 依赖现状：`package.json` **无 @antv/g6、无 echarts、无 @playwright/test**（[package.json](file:///d:/dev/inves_duckdb/frontend/package.json)）；transport 已支持 FormData（multipart）但**不支持二进制 blob 下载**（[fetch.transport.ts](file:///d:/dev/inves_duckdb/frontend/src/api/transport/fetch.transport.ts#L63-L69) 恒走 `res.json()`）。

---

## 二、七页 + E2E 端点对照

| 页面/测试 | 端点（实际路由，前缀 `/api/v1`） | 状态 | 代码位置 |
|---|---|---|---|
| FE-P-023b 案件门户 | `GET /cases`（裸数组，**无 status/q 参数**）· `POST /cases` · `GET /cases/{cid}` · `GET /cases/{cid}/summary` · `POST /cases/{cid}/archive` 🔒（202） | ✅ 全在 | [cases.py](file:///d:/dev/inves_duckdb/server/app/routers/cases.py#L52-L142) |
| FE-P-010 知识图谱 | `GET /cases/{cid}/graph?node_limit=300&edge_limit=500` | ✅ | [graph.py](file:///d:/dev/inves_duckdb/server/app/routers/graph.py#L18-L43) · [graph_view.py](file:///d:/dev/inves_duckdb/server/app/graph_view.py) |
| FE-P-011 庙算工作台 | `GET /cases/{cid}/hypotheses` | ✅ | [research.py](file:///d:/dev/inves_duckdb/server/app/routers/research.py#L47-L69) · [hypotheses_view.py](file:///d:/dev/inves_duckdb/server/app/hypotheses_view.py) |
| FE-P-015 跨案件查询 | `POST /cross-case/query` 🔒 · `GET /cross-case/history?limit=50` | ✅（history 分页协议不一致，见决策 C1） | [cross_case.py](file:///d:/dev/inves_duckdb/server/app/routers/cross_case.py#L81-L148) · [backend_cross.py](file:///d:/dev/inves_duckdb/server/app/store/backend_cross.py) |
| FE-P-016 案件包 | `POST /cases/{cid}/package/export` ⚡ → `{task_id}` · `GET /packages/{task_id}/download`（**zip 二进制流**）· `POST /packages/verify`（multipart）· `POST /packages/import`（multipart + 表单字段）⚡ | ✅（verify 响应缺七步明细/敏感文件清单，见决策 A1） | [package.py](file:///d:/dev/inves_duckdb/server/app/routers/package.py#L38-L127) · [worker/package.py](file:///d:/dev/inves_duckdb/server/app/worker/package.py#L79-L173) |
| FE-P-017 代码逃生舱 | `POST /escape-hatch/generate` · `GET /escape-hatch/stats` | ✅ | [escape_hatch.py](file:///d:/dev/inves_duckdb/server/app/routers/escape_hatch.py#L32-L70) · [worker/escape_hatch.py](file:///d:/dev/inves_duckdb/server/app/worker/escape_hatch.py) |
| FE-P-025 系统设置 | `GET/PUT /settings/queue`（**GET 也要 admin**）· `GET/PUT /settings/resources`（GET 也要 admin；storage_root 只读透出）· `GET /settings/health`（登录可读）· 附 `GET/PUT /settings/policies-thresholds`、`GET /settings/snapshots`、`GET/PUT /settings/features`（均 admin） | ✅（非管理员 403 与"只读态"需求有差距，见决策 B2） | [settings.py](file:///d:/dev/inves_duckdb/server/app/routers/settings.py#L112-L210) |
| FE-T-008 E2E | 登录 → 门户 → 仪表盘 → 线索详情 → 状态迁移 → 审计链（全链路真实端点均已存在） | ⚠️ Playwright 依赖离线可行性待确认（决策 D3） | — |

---

## 三、以后端为准的契约要点（前端类型按此定义）

### 1. 案件门户（cases）

- `GET /cases` → **裸数组** `CaseDto[]`（非 `{items,total}`），**无 status/q/page 查询参数**；CaseDto 字段：`{id, tenant_id, name, status, pack_id, pack_snapshot_at, created_at, created_by}`（[deps.py](file:///d:/dev/inves_duckdb/server/app/deps.py#L104-L107)）。
- `POST /cases` body：`{case_id: 1..64, name: 1..128, pack_id: "default"}`；重复 409（CONFLICT）、pack 不存在 404；operator 取会话；返 case_dto + `data_version:0`。**无角色门槛（登录即可建案）**。
- `GET /cases/{cid}/summary` → `{case: CaseDto, data_version, todos: {clues_pending, review_pending, anomalies_pending}, health: {chain_ok, degraded, diagnostics_warn}, recent_tasks: TaskDto[≤5]}`（[portal_view.py](file:///d:/dev/inves_duckdb/server/app/portal_view.py#L57-L68)）。未 BUILD 案件诊断计数为 0、chain_ok 视 state.sqlite 是否存在。
- `POST /cases/{cid}/archive` body `{reason?: string}` → 202 task_dto；**clearance≥2（require_analyst）**；已封存 409、非法迁移 409；幂等键 `archive:{cid}`。
- 案件状态机四态：`待建案 / 侦查中 / 已结案 / 已封存`（[models.py](file:///d:/dev/inves_duckdb/server/app/meta/models.py#L11-L24)）。

### 2. 知识图谱（graph）

- Query：`node_limit`（默认 300，1–1000）、`edge_limit`（默认 500，1–3000）。
- 响应：`{available: bool, truncated: {nodes: bool, edges: bool, dropped_edges: int}, nodes: [{id, label, type, type_title, jian: string[]}], edges: [{source, target, label, type}]}`。
- 节点 id 域：`"<object>:<pk>"`（如 `person:123`）；节点按**度数 top-N 采样**（非全量），边端点缺失补 id-only 节点、超上限计 `dropped_edges`。
- 未 BUILD/无实体表 → `available:false` 空结构（不 500）。

### 3. 庙算工作台（hypotheses）

- 无查询参数。响应：`{available: bool, derived: true, coverage: {declared: Card[], empirical: Card[]}, heatmap: {jians: ["因","内","反","死","生"], levels: ["观察","线索","确认"], counts: number[3][5]}, candidates: Candidate[≤20], restricted: [{clue_id, reason}]}`。
- Card：`{dimension: string|null, covered, total, missing: string[], reason, severity, created_at}`；双轨 = 声明覆盖（`miaosuan:dimension`）vs 实证覆盖（`miaosuan:dimension:empirical`）。
- Candidate（候补池）：`{clue_id, title, jian_types, level, priority_score, reason}` —— 仅待查线索 top20，**不含任何升格字段、不改变交叉等级**（后端派生口径保证，红线二）。
- restricted：内间线索对无权角色只给 `{clue_id, reason:"内间线索·权限不足"}`，不泄露内容。
- 无产物线索 → `available:false`。

### 4. 跨案件查询（cross-case）

- `POST /cross-case/query` body：`{case_ids: string[2..50 且不重复], sql: string(≥1), reason: string(1..500), max_rows?: 1..10000（默认 1000）, timeout_ms?: 100..600000（默认 30000）}`。
- 鉴权：**租户级全有或全无**——逐案校验 `tenant_id` 归属，任一不符整体 **403（ATTACH 之前拒绝，不读数据）**，被拒案件列表进 ops 审计；403 文案含 denied 列表（消息文本，**无结构化 denied 字段**）。
- SQL 纪律：首词白名单 `SELECT/WITH/PRAGMA`，READ_ONLY ATTACH 双保险，违禁 400；结果强制 `LIMIT max_rows` 包裹。
- **ATTACH 别名方案：每个案件库挂为 `case_<case_id>`**（如 `case_c1.obj_person`）；**后端不自动给结果行加来源案件列**——"结果每行标来源案件"必须由 SQL 本身保证（模板查询用 `UNION ALL + 字面量 source_case` 列）。
- 响应：`{rows: dict[], total: len(rows), case_ids: 已授权列表}`。
- `GET /cross-case/history?limit=50` → `{items: [{id, ts, case_ids[], sql, reason, result_rows}], total}`；**只返回本 operator 的记录**；limit **无上限校验、无 page/page_size**（决策 C1）。
- 审计落点：ops_events（`cross_case_query` / `cross_case_denied`），非案件审计链。

### 5. 案件包（package）

- `POST /cases/{cid}/package/export`：**无 body**（无 reason 字段；导出人=会话 operator，落 task.created_by + ops 事件）→ `{task_id}`（注意：**不是 task_dto**）；任务类型 EXPORT，走任务中心 SSE。
- `GET /packages/{task_id}/download`：**FileResponse zip 二进制流**（非 JSON 信封，文件名 `{cid}_package.zip`）；任务不存在/非 EXPORT/未完成分别 404/400/404；跨租户 404。
- `POST /packages/verify`：multipart 单字段 `file`（zip），同步校验 → `{ok: bool, errors: string[], chain_ok: bool, file_count: number}`。**响应无七步逐项状态、无敏感文件清单**（决策 A1）；`chain_ok=false` 不阻断（橙色告警语义）。
- `POST /packages/import`：multipart `file` + **表单字段** `case_id`、`name`（非 JSON body）；服务端先预校验，`ok=false` → 422；case_id 已存在 → **409**；通过后入队 IMPORT_PACKAGE 任务 → `{task_id}`。
- 敏感文件口径：`case_knowledge.json` 标 sensitive（manifest 内 `files[*].sensitive` 布尔，[worker/package.py](file:///d:/dev/inves_duckdb/server/app/worker/package.py#L43)）；导出含 13 声明文件 + 压实 DuckDB + state.sqlite + artifacts + README + manifest（逐文件 SHA-256）。

### 6. 代码逃生舱（escape-hatch）

- `POST /escape-hatch/generate` body：`{ext_type: "function"|"value_type"|"clean_rule"|"side_effect", name: 1..64, description?: 0..500}` → `{files: [{path, content}], registration_points: string[]}`；非法 ext_type 400。**仅生成文本，不写盘、不注册、不执行**；触发落 ops 统计。
- `GET /escape-hatch/stats` → `{items: [{ext_type, count}], total}`（全局聚合最近 1000 条事件，无分页、无租户过滤）。
- **无角色门槛**（登录即可生成，P2 页面）。

### 7. 系统设置（settings）

- 写 body 统一为 `{reason: string(必填，空则 400), values: {键: 值}}`；键白名单 + 区间/enum 校验，白名单外 400；写仅 admin（`is_admin=1` 或 role=system），非 admin 403 并落 `authz_failure` 平台审计。
- **GET queue / resources / policies-thresholds / snapshots / features 同样要求 admin——非管理员请求直接 403，拿不到数据**（决策 B2）；唯 `GET /settings/health` 登录即可读。
- queue 白名单：`max_workers`(int 1–16，默认 2)、`poll_interval_ms`(int ≥10，默认 100)。
- resources 白名单：`max_rows_default`(1–100000，默认 1000)、`query_timeout_ms`(100–600000，默认 30000)；`storage_root` GET 透出、**永不可经 API 改写**。
- health 响应：`{meta_ok, queue: {pending, running}, worker: {pool_alive: false(恒 false，API 进程不内嵌 Worker), max_workers, poll_interval_ms, note}, versions: {backend: "M6", ontology_default}}`。
- features 白名单仅 `ui_density`("compact"|"comfortable")；policies-thresholds：`cross_level_min_sources`(≥1)、`cross_level_min_clues`(≥2)、`stale_days`(1–3650)；**红线键（llm_enabled 等）永不在白名单**（后端硬拒，与 FE-T-011 呼应）。
- 平台值仅作新建案件默认，生效值以案件快照为准（响应 note 明示）。

### 8. 身份字段缺口

- `GET /auth/me` 与 login 响应仅 `{operator, role, clearance, tenant_id}`，**无 is_admin**（[auth.py](file:///d:/dev/inves_duckdb/server/app/routers/auth.py#L49-L76)）——前端判定平台管理员只能靠 settings 端点 403 探测（决策 A2）。

---

## 四、前端实施方案

### 4.1 新增/扩展 API 封装（`frontend/src/api/endpoints/`）

| 文件 | 覆盖端点 | 备注 |
|---|---|---|
| `cases.ts`（扩展） | cases list（已有）/ create / summary / archive | 新增 create/summary/archive 三方法 |
| `graph.ts`（新） | graph GET（node_limit/edge_limit 参数） | noteDataVersion |
| `research.ts`（新） | hypotheses GET | profiles 已在 `profile.ts`，不重复 |
| `crossCase.ts`（新） | cross-case query POST / history GET | 超时分层用 `cross`（60s） |
| `packageCase.ts`（新） | export POST / download（**blob**）/ verify（FormData）/ import（FormData） | download 走 transport raw 模式 |
| `escapeHatch.ts`（新） | generate POST / stats GET | — |
| `settings.ts`（新） | queue/resources/health GET/PUT + thresholds/snapshots/features GET | PUT 带 idempotencyAction + reason |

**transport 改造（前端内部，无后端依赖）**：`TransportRequest` 增 `responseType?: 'json'|'blob'`，fetch 实现按此返回 `Blob`（zip 下载用），ipc.transport 空实现同步补接口签名；ApiClient 增 `getBlob()` 薄封装。MSW 侧 download handler 返回 `HttpResponse` 二进制。

### 4.2 新增页面与路由

| 页面 | 路由 | 关键交互 |
|---|---|---|
| `PortalView.vue`（替换 `/cases` 占位，平台页） | `/cases` | 案件卡片网格（状态标签 + 待办三计数 + chain 健康点）；状态筛选 tab + 搜索框（客户端过滤）；**"筛选无结果"与"真空态（引导新建）"三态分离**；新建案件对话框（case_id/name）；卡片"进入案件"设当前案件跳 `/c/overview`；归档操作（clearance≥2 + reason，ConfirmDialog） |
| `GraphView.vue`（新） | `/c/graph` | 深色研判模式；G6 v5 画布（力导/辐射两种布局切换）；node_limit/edge_limit 调节条（受后端区间约束）；truncated 横幅（"已按度数采样，丢弃边 N 条"）；节点点击跳线索/实体详情；**G6 动态 import 失败或 available:false 时降级关系表格 + EmptyState（禁空白）** |
| `MiaoSuanView.vue`（新） | `/c/miaosuan` | 顶部 DualTrackCompare（声明 vs 实证覆盖双轨条）；五间×三级 CSS 热力矩阵（accent 单色渐变，零依赖）；候补池虚线隔离区（candidate 卡片，点击跳线索详情）；restricted 灰显条目（只显 clue_id + 原因）；**候补区视觉与正式区严格隔离（FE-T-014）** |
| `CrossCaseView.vue`（新） | `/c/cross-case` | 案件多选（从 `/cases` 拉授权列表）+ PermissionGate N/M 计数（"6/9 有权"）；**M<N 时执行按钮锁 + 无权案件 chips 可一键移除**；reason 必填文本域；模板选择器（跨案同对象查询模板，自动生成 `case_<id>.obj_*` UNION ALL 并注入 `source_case` 字面量列）+ 自由 SQL 文本域（等宽字体，**不引 Monaco**）；max_rows/timeout_ms 高级折叠项；结果 DataTable（动态列，source_case 列固定首列着色）；下方 history 列表 |
| `PackageView.vue`（新） | `/c/package` | 两个 tab：**导出**（选案件 → 预检 chain_ok 橙色告警横幅（取 summary.health.chain_ok）+ 敏感文件提示（case_knowledge.json）→ ConfirmDialog 二次确认 → EXPORT 任务 SSE → 完成后下载按钮（blob））；**导入**（上传 zip → verify 七步清单 + chain_ok 橙警 + 敏感文件红框 → 填 case_id/name → import 任务 SSE）；导出/导入历史复用任务中心（task_type 过滤） |
| `EscapeHatchView.vue`（新） | `/c/escape` | 左：ext_type 四类选择 + name/description 表单 → 生成代码桩（多文件 tab 展示，含注册点说明，只读代码块 + 复制按钮）；右：stats CSS 条形图（四类计数）；空态"暂无需扩展的能力" |
| `SettingsView.vue`（替换 `/settings` 占位，平台页） | `/settings` | 三组卡片：队列（max_workers/poll_interval_ms）、资源（max_rows_default/query_timeout_ms/storage_root 只读）、健康度（meta_ok/队列积压/worker note/版本）；非 admin：queue/resources 渲染 🔒 锁定面板"仅平台管理员可查看（后端 403）"，health 正常展示；外观偏好（主题/密度/字号）走 FE-C-028 `sw.pref.*` 本地；"审计合规"开关 🔒 锁定不可关（静态展示）；admin 编辑走 ConfigConfirmDialog + reason 必填 |

路由注册：7 条具体路由插在 `/c/:section` catch-all 之前；`/cases`、`/settings` 替换 PlaceholderView 引用。

### 4.3 新增 domain 纯函数（`frontend/src/domain/`）

- `portal.ts`：状态筛选 + q 搜索谓词、空态/无结果/有数据三态判定、待办计数聚合、建案表单校验（case_id 格式）。
- `graphModel.ts`：节点/边类型、truncated 摘要文案、度数排序展示、降级表格数据派生（edges → 关系行）。
- `miaoSuan.ts`：双轨覆盖 diff（declared vs empirical 缺口合并）、热力色阶（counts → accent 单色 5 档）、**候补池隔离纯函数（FE-T-014：候选数据注入前后交叉等级快照不变断言）**、restricted 归类。
- `crossCase.ts`：N/M 授权计算（selected ∩ cases 列表）、全有或全无闸门（可执行布尔）、SQL 模板构建器（case_<id> 别名 + UNION ALL + source_case 字面量列）、reason 非空校验、max_rows/timeout_ms 区间钳制。
- `packageFlow.ts`：verify 结果 → 七步清单派生（决策 A1 口径）、chain_ok 橙警判定、敏感文件红框名单、导出/导入任务状态机。
- `escapeHatch.ts`：ext_type 四枚举元数据（中文名/说明/注册点数量）、stats 条形最大值归一、空态判定。
- `settingsModel.ts`：admin 闸门（is_admin/403 双路径，决策 A2）、白名单键元数据（区间/enum/单位）、values diff + reason 必填校验。

### 4.4 新增组件（`frontend/src/components/`）

- `research/DualTrackCompare.vue`（FE-C-022）：声明 vs 实证双轨对比条，缺口数高亮。
- `common/OntologyVersionTag.vue`（FE-C-021）：版本号 + 🔒 锁定态（门户/案件包复用）。
- `research/GraphCanvas.vue`：G6 v5 动态 import 包装；加载失败 emit `fallback` 触发表格降级。
- `research/HeatGrid.vue`：3×5 CSS 热力矩阵（零依赖，accent 单色渐变）。
- `tools/StepChecklist.vue`：案件包七步校验清单（pass/warn/fail 三态）。
- 复用：PolicyGate（跨案 N/M）、DataTable、EmptyState（三态）、MetricCard、ConfigConfirmDialog、TaskProgressCard/SSE、Stepper、StatusBadge、MaskedField。

### 4.5 MSW（`frontend/mocks/handlers.ts`，补约 16 个 handler）

- cases：POST create（含 409 重复态）、GET summary（造 todos/health/recent_tasks，含 chain_ok=false 态）、POST archive（202）。
- graph：造 available:true 含 truncated 的图 + available:false 空图两态。
- hypotheses：造双轨缺口 + heatmap counts + candidates + restricted（含内间 restricted 条目）。
- cross-case：query 造全有/403 全无两态（403 文案含 denied 列表）；history 造分页记录。
- package：export 返 task_id；download 返二进制 zip（HttpResponse blob）；verify 造 ok / chain_ok=false / errors 三态；import 202。
- escape-hatch：generate 返代码桩结构；stats 造四类计数 + 空态。
- settings：queue/resources/health 按角色造 200/403 两态；PUT 校验 reason 缺失 400。
- 红线纪律：MSW 只造数据态，**全有或全无拦截、候补不改等级、非 admin 锁定等红线交互不得 mock 成"永远通过"**。

### 4.6 技术债偿还

- 台账"简化版审计链（MVP-1 → MVP-5）：无维度切换器"：AuditChainView 增补**案件级/线索级维度切换器**（segmented control，线索级带 clue_id 入 query）+ 节点详情列切换（[AuditChainView.vue](file:///d:/dev/inves_duckdb/frontend/src/views/AuditChainView.vue#L111-L115) 现有 clue_id 输入框升级）。
- G6 图谱为"五可砍"最后一项，本轮补齐；ECharts 交互图表经决策 D2 后以 CSS 零依赖方式闭合（heatmap/条形）。

---

## 五、★ 待确认决策项（确认前不开工）

### A. 后端缺口（前端无法单独闭合）

| # | 议题 | 现状 | 选项 |
|---|---|---|---|
| **A1** | 案件包 verify 响应**无七步逐项状态、无敏感文件清单** | `POST /packages/verify` 仅返 `{ok, errors[], chain_ok, file_count}`（[package.py L83-L88](file:///d:/dev/inves_duckdb/server/app/routers/package.py#L83-L88)）；manifest 内有 `files[*].sensitive` 与七步过程但不透出。需求要"校验清单七步 + 敏感文件红框" | **（推荐）后端扩响应**：`steps: [{key, label, status: pass\|warn\|fail, detail?}]`（format/hash/declarations/schema/chain/duckdb/manifest 七步）+ `sensitive_files: string[]`（从 manifest 提取）；**（备选）前端按 errors 字符串关键词归类**——通过步状态不可知、敏感文件名单拿不到，红框只能静态提示"包内可能含 case_knowledge.json"，体验打折 |
| **A2** | `/auth/me` 与 login 响应**无 is_admin** | 系统设置页管理员判定无事实源；非 admin GET queue/resources 直接 403（连只读数据都没有，与需求"非管理员只读态明示原因"存在差距） | **（推荐）后端两响应增 `is_admin: boolean`**（小改，[auth.py](file:///d:/dev/inves_duckdb/server/app/routers/auth.py) + MeInfo/SessionInfo 类型）；前端据此直接渲染锁定面板，不靠 403 探测；**（备选）前端保持 403 探测**：进入设置页并发试拉 queue，403 即锁定态——多一次失败请求、且无法区分"非管理员"与"服务异常" |
| **A3** | 门户待办汇总的取数方式 | 无批量汇总端点；summary 是逐案件端点 | **（推荐默认）前端 Promise.all 并发 N 次 summary**（单机版案件数量级为十量级，N+1 可接受）；**（备选）后端加 `GET /cases/summaries` 批量端点**——若预期案件数上百再排 |

### B. 前后端参数不一致（以后端为准，请确认）

| # | 议题 | 清单 v1.8 写法 | 后端实际 | 建议口径 |
|---|---|---|---|---|
| **B1** | 案件列表筛选/搜索 | `GET /cases?status=&q=` | **无查询参数**，返裸数组（[cases.py L52-L56](file:///d:/dev/inves_duckdb/server/app/routers/cases.py#L52-L56)） | **以后端为准：status 筛选 + q 搜索全部前端客户端做**（案件数十量级，无性能问题）；不新增后端参数 |
| **B2** | 系统设置非管理员形态 | "非管理员**只读态**明示原因" | queue/resources 等 **GET 也要求 admin，非 admin 直接 403**（[settings.py L112-L131](file:///d:/dev/inves_duckdb/server/app/routers/settings.py#L112-L131)）；仅 health 登录可读 | **以后端为准**：非管理员看到 🔒 锁定面板（"仅平台管理员可查看"）+ health 组正常展示，不展示队列/资源数值。这与"只读可见数据"不同——请确认接受此形态（或走 A2 后维持 403 现状） |
| **B3** | 跨案件查询入参 | 清单仅列 case 选择 + reason | body 另有 `max_rows`（默认 1000，上限 10000）、`timeout_ms`（默认 30s，上限 600s） | **以后端为准**：默认值直接生效，前端在"高级选项"折叠区暴露（受同样区间钳制）；SQL 为必填自由文本（后端首词白名单兜底），前端提供模板不限制自由书写 |

### C. 分页缺口（前端有列表分析需求、后端无分页协议）

| # | 列表 | 后端现状 | 建议 |
|---|---|---|---|
| **C1** | 跨案件查询历史 `GET /cross-case/history` | `?limit=50`，**无 page/page_size、limit 无上限校验**，返 `{items,total}`（[cross_case.py L124-L148](file:///d:/dev/inves_duckdb/server/app/routers/cross_case.py#L124-L148)） | **（推荐）后端对齐全站协议补 page/page_size**（与 tasks/quarantine/clues 一致，limit 保留兼容或直接替换）；**（备选）前端单页"加载更多（limit 递增）"**——与 DataTable 跳页/`?page=N` 分享协议不一致，仅本页特殊 |
| **C2** | 案件列表 `GET /cases` | 裸数组、无分页 | 数量级小（单机十量级），**前端客户端筛选+渲染，不分页**；请确认 |
| **C3** | `GET /settings/snapshots`（admin 快照列表） | `{items,total}` 无分页 | 数量级小（每案至多几条），客户端渲染；本轮该端点仅在设置页只读折叠区使用 |

> 不需分页（聚合/采样/单文档，已核）：graph（度数 top-N 采样 + truncated 标记）、hypotheses（candidates 固定 top20 + 3×5 矩阵）、escape-hatch/stats（四类聚合）、settings/health（单对象）、verify 结果（单报告）。

### D. 依赖与离线环境（FE-I-001 断网可安装红线）

| # | 议题 | 现状 | 选项 |
|---|---|---|---|
| **D1** | **G6 v5 图谱依赖** | package.json 无 @antv/g6；图谱页核心交互依赖它 | **（推荐）装 `@antv/g6` v5**：需先验证内网 npm 镜像可装（`pnpm install` 断网成功是 MVP-0 红线）；**G6 动态 import 失败自动降级关系表格**作为兜底，装不上也不阻塞其余六页。请确认镜像可用性 / 是否授权引入 |
| **D2** | ECharts（庙算热力矩阵 + 逃生舱条形图） | package.json 无 echarts；清单写"ECharts heatmap/条形" | **（推荐）不引 echarts**：3×5 热力矩阵与四类条形用 CSS 网格 + accent 单色渐变实现（视觉语义等价、零离线风险）；若坚持 echarts 需同步验证镜像（~1MB+ zrender 依赖链） |
| **D3** | **Playwright E2E（FE-T-008）** | 无 @playwright/test；Chromium 二进制约 150MB 需单独下载 | 三选一请确认：**(a)** 内网镜像可装 Playwright + Chromium → 写 `e2e/` 目录 spec，对接本机 uvicorn（WSL）跑真实后端主流程；**(b)** 装包但浏览器二进制预装/离线拷贝；**(c)** 本轮以 vitest + happy-dom 全链路集成测试替代（覆盖登录→门户→仪表盘→详情→迁移→审计链的组件编排），FE-T-008 Playwright 形式后补——不等价，需明示接受 |

---

## 六、默认项处理（无异议按此执行）

- 跨案件"结果每行标来源案件"：**模板查询自动注入 `source_case` 字面量列**（UNION ALL 各 `case_<id>` 子查询）；自由 SQL 模式在编辑器旁提示"自由 SQL 需自行 select 来源列"，结果表无 source_case 列时不伪造。
- 跨案件权限预判：前端以 `GET /cases`（租户过滤后列表）为授权全集做 N/M 计数；后端 403 为最终防线（denied 列表仅在错误文案中，前端不解析文案、直接提示"存在无权案件，请移除后重试"）。
- 案件包导出 chain 告警：导出前取 `summary.health.chain_ok`，false 时橙色横幅但允许继续（与导入侧 chain_ok 语义一致）。
- 导出/导入进度：复用任务中心 SSE（TaskProgressCard），不另造轮询；历史经 `GET /tasks?task_type=EXPORT|IMPORT_PACKAGE` 查。
- settings 的 policies-thresholds/snapshots/features 三端点：本轮在系统设置页做 **admin 可见只读折叠区**（阈值白名单只读展示 + 快照列表），不做编辑（编辑阈值属配置中心后续批次）；features.ui_density 与本地偏好的关系以后端 note 明示"平台默认值"。
- 系统设置页不触碰 llm_enabled 等红线键（后端白名单本就不含），UI 上以 🔒 锁定行展示"审计/LLM 红线配置不可关闭"。
- 图谱节点点击下钻：节点 id 域 `obj:pk`，按 type 路由到既有线索/实体视图（无实体详情页则跳线索列表带过滤参数）。
- 代码逃生舱代码展示：等宽字体只读代码块 + 逐文件复制按钮，不做在线编辑器、不做"下载文件"（仅文本，符合不写盘纪律）。
- 案件门户为平台页（不入侧栏），入口：全局条案件选择器"全部案件"、登录后默认落点可改为 `/cases`（保持 `/c/overview` 重定向不变更，仅在选择器增加入口）。

---

## 七、实施顺序（4 段）

1. **访问层 + transport 改造**：cases 扩展 + 6 个新 endpoint 文件 + transport blob 模式 + ipc 空实现签名 + 16 个 MSW handler；domain 7 个纯函数随 TDD 先行。
2. **七页两批落地**：
   - 批 A（平台 + 高价值闭环）：案件门户（23b）→ 跨案件查询（015，含 PermissionGate N/M）→ 案件包（016，含 transport blob）；
   - 批 B（研判与工具）：庙算工作台（011，FE-C-022）→ 知识图谱（010，G6 + 降级）→ 代码逃生舱（017）→ 系统设置（025）。
3. **技术债 + E2E**：审计链维度切换器；FE-T-008 按 D3 决策落地。
4. **全量验证**：vitest 全绿 + `vue-tsc + vite build`；后端 `python run_tests.py`（若 A1/A2 后端改动则含新增用例：verify steps 字段、/auth/me is_admin）；MSW 七页走查 + 真实后端（WSL uvicorn）冒烟。

### 本轮测试清单

| 编号 | 内容 | 落点 |
|---|---|---|
| FE-T-014 | **红线二：候补池不改等级** | miaoSuan 纯函数：清空缺省等级快照 → 注入 candidates → 等级不变；候补区虚线隔离 class 断言 |
| 新增 | 跨案件全有或全无 | N/M 计数、M<N 执行按钮 disabled、移除无权后可执行；reason 必填拦截 |
| 新增 | 案件包留痕与告警 | 导出二次确认 + chain_ok=false 橙警不阻断；verify fail 态清单；下载走 blob |
| 新增 | 设置 fail-closed | 非 admin queue/resources 锁定面板不渲染数值；reason 缺失 PUT 拒绝；红线键锁定行 |
| 新增 | 门户三态 | 真空态（引导新建）/ 筛选无结果 / 有数据可区分 |
| FE-T-008 | E2E 主流程 | 按决策 D3 形式落地 |

---

## 八、边界与纪律

- 不写自由 SQL：跨案件页 SQL 由**用户在页面输入**（后端首词白名单 + READ_ONLY + 审计兜底），前端代码自身不拼业务 SQL；模板生成器只产出参数化的跨案 UNION ALL 骨架。
- 不 Mock 红线：全有或全无、候补不改等级、非 admin 锁定、chain 告警一律直连真实契约；MSW 仅造数据态。
- 写操作全部经后端端点 + ConfirmDialog/ConfigConfirmDialog + reason（settings 写 reason 后端强制）；导出/导入/归档走任务体系，前端不自造状态。
- 后端改动（若 A1/A2/C1 获批）遵守既有范式：verify 扩字段不破坏既有 `ok/errors/chain_ok` 消费方；is_admin 为增量字段；history 分页与 tasks 协议同构。
- G6/Playwright 引入前先验离线安装；任何依赖装不上时降级路径必须可用（图谱表格降级、E2E 集成测试替代），**不以"等依赖"阻塞其余页面交付**。
