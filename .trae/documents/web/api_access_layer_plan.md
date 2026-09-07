# 后端 API 访问层实施计划（Web / Electron 双端）

**版本** v1.0 ｜ **日期** 2026-09-07 ｜ **状态** 决策已确认（D5~D10），随 M1 实施
**关联** [backend_api.md](backend_api.md)（D1~D4 后端基线；本文件补 D5~D10）、[M1_plan.md](M1_plan.md)（地基里程碑）、[ui_imp.md](ui_imp.md)（前端选型）、孙武侦查官_UI开发规范_v1.1（「API契约」Sheet = 本层的前端责任清单）

---

## 一、定位与范围

**"后端 API 访问层" = 客户端侧统一消费层（TypeScript）**，是 UI 与后端 API 之间的唯一出口：信封解包、错误码映射、token 管理、幂等键、SSE 进度流、分页/上传/下载约定、TanStack Query 缓存编排。它不是 FastAPI 服务端本身（服务端是 M1 的产物），而是 M1 的第一个消费方。

**在范围**：

- `HttpTransport` 传输抽象与两实现（Web fetch / Electron IPC 桥）
- ApiClient（响应信封、错误码表、Bearer 注入、幂等键、401 钩子）
- SSE 进度流（fetch + ReadableStream，Last-Event-ID 断线重放）
- endpoints 分组模块（按 backend_api.md 第三部分 A~K；M1 仅 auth/cases/tasks）
- OpenAPI 契约 → TS 类型生成 + MSW mock（契约先行）
- TanStack Query hooks 与 data_version 失效闭环
- Electron main/preload 桥（token 加密存储、SSE 转发、文件流式收发）

**不在范围**：FastAPI 服务端实现（M1_plan）、UI 组件与 26 页面（ui_v1.3）、内核业务逻辑（`core/` 零改动，除一行枚举，见 3.1）。

---

## 二、现状盘点

| 项 | 现状 | 对本计划的含义 |
|---|---|---|
| `server/` 服务端 | 不存在（M1_plan 状态"待批准"） | 访问层按契约先行，MSW 兜底，不阻塞 |
| 前端工程 | 不存在（ui_imp.md 已定 React 19 + Vite + TS + TanStack Query） | 访问层是前端工程的第一块地基 |
| 内核能力面 | Gateway / FunctionExecutor / ActionExecutor / AccessContext / PolicyEngine / AuditChain 均已完备 | routers 薄编排即可，内核不为 HTTP 做改造 |
| 契约文档 | backend_api.md 约 60 端点 + 信封/错误码/SSE/幂等已定义；UI 规范 v1.1「API契约」Sheet 已定前端责任 | 访问层职责边界清晰，缺项见第三节增补 |

---

## 三、对现有代码的调整（最小修改原则）

### 3.1 内核 `core/`：唯一一行级改动

| 位置 | 改动 | 决策 |
|---|---|---|
| [core/access.py](file:///d:/Dev/investigation/core/access.py)（`NETWORKS = ("local", "isolated")`） | 增加 `"web"`：`NETWORKS = ("local", "isolated", "web")`；`can_llm_call()` 语义同 `local`（Web 会话允许 LLM 辅助，但 LLM 脱敏闸门不变） | **D5** |

理由：backend_api.md §4.1 要求 API 会话构造 `AccessContext(..., network="web")`，当前枚举外取值会 `ValueError` 硬失败。加 `"web"` 后审计链可区分请求来自本地 CLI/MCP 还是 Web 会话。落地时补 access/policy 相关测试组用例。

**其余内核全部零改动**：只读查询走 Gateway、计算走 FunctionExecutor、写走 ActionExecutor、遮蔽走 PolicyEngine——routers 只做"鉴权 → 构造 AccessContext → 调内核/入队 → 遮蔽 → 序列化"（backend_api.md 第五部分边界纪律）。

### 3.2 M1 服务端纳入项（M1 阶段 F 一并实现，均为几行级）

| # | 增补 | 内容 | 决策/来源 |
|---|---|---|---|
| S1 | SSE 与普通端点同一鉴权 | auth 中间件天然覆盖 `/events`；**不**为 SSE 开 Cookie/query-token 通道（query token 会落入 Nginx 日志） | D7 / UI 规范 P1-2 |
| S2 | CORS 中间件 | 环境变量白名单 `SUNZI_CORS_ORIGINS` 驱动，**默认关闭**；Web 同源经 Nginx 不触发，Electron 走 IPC 桥不触发；白名单仅供 dev server 与调试 | D6 |
| S3 | 基址 `/api/v1` | 路由前缀发布即冻结；endpoint 清单中 `/api/...` 为省略写法 | D8 / P1-6 |
| S4 | 错误码补 `DEGRADED_WRITE_REJECTED` (409) | 降级运行时写/导出/立案一律 API 侧拒绝（UI 禁用而 API 不禁等于没禁） | UI 规范 P1-1 |
| S5 | `GET /api/v1/health` 免认证探针 | 返回版本、元数据层状态、Worker 最近心跳；供 Electron 连接探测与 Nginx 健康检查 | Electron 就绪探测 |
| S6 | 幂等重试语义入契约 | Worker 内部重试沿用原 task_id、递增 retry_count，**不走幂等通道**；仅客户端主动重发带 `Idempotency-Key` | UI 规范 P0-2 |

> M1_plan 已有的响应信封、operator 强制取会话、无 token 401、跨租户 404、`(case_id,task_type,idempotency_key)` UNIQUE 等红线与本层需求完全对齐，不动。

---

## 四、双端运行形态

```
形态一 · Web（私有化主力交付）
  浏览器 SPA(dist/) ──同源 /api/v1──▶ Nginx ──▶ uvicorn(API) ──▶ Worker
  · 无 CORS；SSE 路径 proxy_buffering off、proxy_read_timeout 拉长
  · token：内存(Zustand) + sessionStorage（重载不丢、关页即失）

形态二 · Electron 瘦客户端（建议先做）
  Renderer(同一套 React 代码) ──IPC──▶ Main(Node) ──fetch(Bearer)──▶ 局域网/远程 API
  · token 仅存 main 进程 safeStorage；renderer 不持 token、不发 HTTP
  · CSP 保持最严（renderer 无 connect-src 网络权限）；无 CORS 依赖
  · base URL 由设置页配置（远程服务器地址）

形态三 · Electron 本地 sidecar（后期可选）
  Main ──spawn──▶ python -m uvicorn + python -m server.run_worker（127.0.0.1 随机端口）
  · 访问层零感知——仅 base URL 变为 http://127.0.0.1:{port}，health 探针等待就绪
  · 打包 Python 环境复杂，推迟为可选项；访问层不为它写任何特判
```

---

## 五、决策记录（D5~D10，2026-09-07 已确认）

| 编号 | 决策点 | 结论 | 落地影响 |
|---|---|---|---|
| D5 | AccessContext network 枚举 | 内核新增 `"web"`（LLM 语义同 `local`，审计区分来源） | core/access.py 一行 + 测试；API 会话 `network="web"` |
| D6 | Electron 通信架构 | **IPC 桥**：renderer 不持 token、不发 HTTP，main 进程转发；Web 用 fetch；两实现同一 `HttpTransport` 接口，业务代码零 `if (isElectron)` | 访问层 transport 双实现；CORS 默认关闭仅白名单调试；CSP 最严 |
| D7 | 认证与 SSE 载体 | **全程 Bearer（含 SSE）**；客户端用 fetch + ReadableStream 消费 SSE（`@microsoft/fetch-event-source`），**弃用原生 EventSource**；不引入 Cookie/CSRF | 服务端 auth 中间件零特殊处理；前端 SSE 统一走 transport.stream |
| D8 | API 版本前缀 | 基址 `/api/v1` 发布即冻结 | M1 路由前缀；清单 `/api/...` 为省略写法 |
| D9 | token 存储 | Web：内存 + sessionStorage；Electron：main 进程 safeStorage 加密 | 分端实现，ApiClient 只向 transport 取 token，不感知位置 |
| D10 | 契约先行 | FastAPI OpenAPI → `openapi-typescript` 生成 TS 类型 + MSW mock handler | 访问层与 M1 **并行开发**，M1 骨架出 OpenAPI 后替换手写类型 |

---

## 六、访问层架构

### 6.1 目录结构（前端工程 `web/`，选型依 ui_imp.md）

```
web/src/api/
├── transport/
│   ├── types.ts            # HttpTransport 接口：request / stream / upload / download
│   ├── fetch.transport.ts  # 形态一：fetch + ReadableStream SSE；baseURL 相对 /api/v1
│   └── ipc.transport.ts    # 形态二/三：window.sunzi.* 的薄封装（实现在 Electron main）
├── client.ts               # ApiClient：信封解包 {ok,data,data_version}、Bearer/幂等头注入、401 钩子
├── errors.ts               # ApiError(code,message,detail,httpStatus) + 错误码→UI 处理映射（唯一事实源）
├── sse.ts                  # taskEvents(tid): AsyncIterable<ProgressEvent>，自动带 Last-Event-ID 重放
├── idempotency.ts          # 逻辑动作→UUID 键生成与复用（ConfirmDialog 统一接入）
├── endpoints/              # 按 backend_api.md A~K 分组；M1 仅 auth.ts / cases.ts / tasks.ts
├── types/generated.ts      # openapi-typescript 生成物（不手改）
└── react/
    ├── query-keys.ts       # TanStack Query key 工厂 + data_version 失效策略
    └── hooks/              # useLogin / useCases / useCase / useTasks / useTaskEvents …（M1 子集）

electron/（形态二/三，后期立壳；renderer 代码零改动）
├── preload.ts              # contextBridge 暴露 window.sunzi.{request,stream,upload,download,onStreamEvent}
└── main/
    ├── ipc-handlers.ts     # undici fetch 转发；token 仅存 main
    ├── token-store.ts      # safeStorage 加密读写
    ├── sse-bridge.ts       # main 解析 SSE → webContents.send（按 task_id 通道，支持退订）
    └── sidecar.ts          # （形态三）spawn/健康探测/端口回收
```

### 6.2 分层纪律

UI 组件 → `react/hooks` → `endpoints` → `ApiClient` → `HttpTransport` 接口。
- 任何一层不得出现 `if (isElectron)` 分支；启动时按 `window.sunzi` 是否存在选择 transport 实现。
- hooks 以下不含业务判断；五态/门禁/遮蔽渲染仍在 UI 组件层。
- 访问层不缓存任何遮蔽字段明文（UI 规范 5.3）；列表只取聚合字段，明细走详情端点。

### 6.3 机制清单（与 UI 规范 v1.1「API契约」Sheet 逐条对应）

| 机制 | 实现要点 |
|---|---|
| 响应信封 | 成功解包取 `data`；`data_version` 交 query-keys 层；失败抛 `ApiError(code)` |
| 错误码映射 | 401 `UNAUTHENTICATED`→清会话跳 `/login`（保留来源路径）；`AUTH_FAILED`→固定文案；404→统一"不存在或无权访问"；409 `IDEMPOTENCY_CONFLICT`→跳既有任务不重复提交；`DEGRADED_WRITE_REJECTED`→降级横幅并禁用写入口；ErrorBoundary 以此表为唯一事实源 |
| SSE | `stage/pct/stage_label/detail` 四字段映射 TaskProgressCard；断线重连带 `Last-Event-ID`，进度不跳变；>3s 显示阶段文案 |
| 幂等键 | 写操作经 ConfirmDialog 触发时生成 UUID 并随逻辑动作复用（防抖/防重复提交）；导入指纹 = 文件名 + 内容 SHA-256 + 行数，重复提示"已存在，将跳过" |
| 鉴权 | 请求体**绝不传 operator**（服务端取会话，传了也被忽略）；role/clearance 来自 `/api/auth/me` 驱动路由守卫与 PermissionGate |
| 分页 | 统一 `{items,total,page,page_size}`，DataTable 分页器组件消费 |
| 上传 | multipart；Web 走 FormData，Electron 走 IPC 传**文件路径**由 main 流式发送（涉密大文件不进 renderer 内存）；返回 upload_id + SHA-256 + 行数 |
| 下载 | Web：fetch 取 blob → objectURL（M5 案件包）；Electron：main 流式落盘 + 保存对话框；下载审计由服务端补（P1-4，M5 前） |
| 版本闭环 | 读响应 `data_version` 入缓存标签；SSE 收到版本切换事件 → 该案件相关 Query 全部失效重取 |

---

## 七、实施阶段与验收

| 阶段 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| **0 契约补丁**（本次已完成文档） | D5~D10、S1~S6 落入 backend_api.md / M1_plan.md | — | 三份文档一致，评审通过 |
| **1 M1 服务端纳入项** | 随 M1 阶段 F：S1~S6 + access.py 加 `"web"` 枚举及测试 | M1 阶段 A~E | TestClient：SSE 带 Bearer 200 / 不带 401；`/api/v1/health` 免认证 200；CORS 默认拒跨域、白名单放行；降级写返回 409 `DEGRADED_WRITE_REJECTED` |
| **2 前端访问层**（与 M1 **并行**） | Vite+React+TS 脚手架；transport 接口 + fetch 实现；client/errors/sse/idempotency；auth/cases/tasks 三模块；OpenAPI 类型生成 + MSW；Vitest 单测 | OpenAPI 契约（M1 骨架产出前用 MSW） | MSW 下全绿：信封解包、401 跳转、409 跳既有任务、SSE 重连进度不跳、幂等键复用、遮蔽明文不入缓存 |
| **3 M1 端到端** | uvicorn + run_worker 真实 HTTP 冒烟：登录→建案→入队 BUILD→SSE 进度→原子切版本→Query 自动刷新 | 阶段 1+2 | 与 M1_plan test_api_base 同链路经真实 HTTP 走通 |
| **4 Electron 桥** | electron main/preload 壳；IPC transport；safeStorage；base URL 设置页；sse-bridge；上传/下载通道 | 阶段 2 接口冻结 | **renderer 零改动**切换 transport；同一套 hooks 在 Electron 内通过；token 在 renderer 内存中不可见 |
| **5 红线 E2E** | Playwright(Web) + Electron 冒烟：无 token 401 跳登录、幂等双提只生一任务、SSE 杀线重连、降级态禁写、跨租户 404、遮蔽值不出现在任何客户端缓存 | 阶段 3/4 | 红线用例全绿 |

**最大进度收益**：阶段 2 与 M1 并行——契约先冻结、MSW 兜底，访问层不阻塞于服务端。

---

## 八、风险与处置

| 风险 | 处置 |
|---|---|
| fetch + ReadableStream 解析 SSE 的兼容性 | 内网 Chromium 内核可控（Electron 同内核）；直接用成熟库 `@microsoft/fetch-event-source`，不手写 SSE 解析 |
| Electron sidecar 打包 Python 环境复杂 | 形态二（瘦客户端）先行；sidecar 推迟为可选；访问层不为它写特判，仅 base URL 不同 |
| OpenAPI 类型生成依赖 M1 骨架 | M1 阶段 F 一出即生成替换；此前手写 types + MSW，契约以 backend_api.md 为准 |
| 访问层范围蔓延成业务层 | 分层纪律 + 代码审查：hooks 以下无业务判断；五态/门禁在组件层 |
| renderer 直连 HTTP 的诱惑（调试方便） | IPC 桥为唯一交付形态；renderer 直连仅在 dev 白名单 CORS 下临时使用，不进生产构建 |
| Windows/WSL 双环境 | 访问层为纯 TS，无平台分支；sidecar 进程管理是唯一平台相关点，集中在 main/sidecar.ts |

---

*本文档为 API 访问层的开发基线。新增端点或变更信封/错误码/SSE 结构时，backend_api.md 与本文件须同步更新；Electron 桥接口（`window.sunzi.*`）冻结后变更须经评审。*
