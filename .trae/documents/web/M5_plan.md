# M5 高级能力里程碑实施计划（REQ-W-026/027/028/029/031）

**日期** 2026-09-08 ｜ **状态** 待评审
**关联** [backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)（批次表：M5=批 7-9；J/K 组端点契约）、[M4_plan.md](file:///d:/dev/inves_duckdb/.trae/documents/web/M4_plan.md)（已实施，2026-09-08）、[req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)（W-026/027/028/029/031 AC 原文）
**范围** 批 7-9 五项：W-026 跨案件查询通道（ATTACH READ_ONLY）、W-027 跨案件权限全有或全无、W-028 案件包导出、W-029 案件包校验与导入、W-031 代码逃生舱（代码桩生成）。
**不含** Postgres 实现、真实 LLM 模型接入、前端页面（本计划仅后端端点与任务）、GB 级大规模性能验证（req.md 标记为未验证，M5 仅做小规模 AC 覆盖）。

**最小影响红线（继承 M1~M4 首要原则）**：M5 对 server 外存量代码的改动目标为 **core 零改动**。五项需求的 core 能力已全部就绪：
- W-026/027：`CrossCaseStore` 骨架已在 [server/app/store/backend.py:157](file:///d:/dev/inves_duckdb/server/app/store/backend.py#L157) 登记（M1 预留），ATTACH 是 DuckDB 原生语法，core 无需感知；
- W-028/029：`PackManager.init_pack(from_pack=)` 已在 [core/pack.py:71](file:///d:/dev/inves_duckdb/core/pack.py#L71) 实现，`AuditChain` 支持自定义 case_id（`*cross-case*`）与 before/after 结构化审计；
- W-031：四类扩展的注册点（`FUNCTION_IMPLS`/`TYPE_SQL`/`ALLOWED_SIDE_EFFECTS`/clean 规则）均为既有字典/集合，逃生舱仅生成代码桩文本，不执行、不注册。

**唯一 core 触点**：无。全部改动集中在 `server/` 目录。ATTACH/SHA-256/manifest/代码桩生成均为 server 层职责。

---

## 一、仓库研究结论（M4 交付现状与 M5 依赖锚点）

1. **CrossCaseStore 骨架已就位（M1 预留）**：[backend.py:157-193](file:///d:/dev/inves_duckdb/server/app/store/backend.py#L157) 的 `CrossCaseStore` 类已声明 `authorized_cases` 属性与 `read_conn`/`write_conn`/`query`/`close` 接口，全部抛 `UnsupportedOperation`；`StoreFactory.for_cross_case()` 已实现（[backend.py:263](file:///d:/dev/inves_duckdb/server/app/store/backend.py#L263)）。M5 只需填充 ATTACH 逻辑，不改接口签名。

2. **ATTACH 语法与约束已明确**：req.md W-026 AC-1~7 要求 `ATTACH ... (READ_ONLY)`、禁 DDL/DML、强制 max_rows 与超时、查询进审计、连接按 case 组合缓存。spike H5-H7 已小规模验证（50 库×1000 行可接受），GB 级为未验证项。**ATTACH 收敛在 `server/app/store/` 内**（`tests/test_store_backend.py` 门禁限制非 store 模块直连 DuckDB）。

3. **全有或全无鉴权可在 server 层完成**：W-027 AC-2 要求拒绝发生在 ATTACH 之前。server 层已有 `MetaRepo` 案件 membership 校验（M2 交付），可在 router 层遍历 `case_ids` 逐一校验，任一失败即整体 403，不调用 `for_cross_case`。**零 core 改动**。

4. **案件包导出 core 支撑完整**：
   - `PackManager.init_pack(from_pack=)`（[core/pack.py:71](file:///d:/dev/inves_duckdb/core/pack.py#L71)）：从模板复制 13 声明文件 + 替换 source 为占位 source_sql；
   - `AuditChain`（[core/audit.py](file:///d:/dev/inves_duckdb/core/audit.py)）：支持 `backend="sqlite"`，导出时从 state.sqlite 读出 `audit/chain.csv`（ADR 5.2 目录结构）；
   - 版本压实：`handle_archive`（[tasks.py:155](file:///d:/dev/inves_duckdb/server/app/worker/tasks.py#L155)）已实现非当前版本删除，导出复用此逻辑或独立执行；
   - 13 声明文件：案件快照 `cases/{cid}/ontology/default/` 下的 objects/links/bindings/rules/functions/actions/views/policies 等。
   **W-028 只需 server 层编排：压实 → 审计链冻结校验 → 逐文件 SHA-256 → manifest → README → 打包。**

5. **案件包导入复用 init_pack**：W-029 AC-6 明确要求使用现有 `PackManager.init_pack(from_pack=)`。导入流程为：解压临时目录 → verify_package（格式/SHA-256/13 声明/schema_version/root_hash/DuckDB 只读打开）→ `init_pack(new_case_id, from_pack=<解压目录>)` → 元数据登记新案件。**零 core 改动**。

6. **代码逃生舱四类扩展注册点已明确**：
   - py Function：`core/functions.py:135` `FUNCTION_IMPLS` + `@register_function` 装饰器；
   - 值类型：`core/ontology.py:53` `TYPE_SQL` 字典（string/integer/decimal/date/boolean/timestamp/...）；
   - 清洗规则：`core/ontology_loader.py` 装载期 `clean` 字段引用 py 函数（与 FUNCTION_IMPLS 同机制）；
   - Action 副作用：`core/ontology_loader.py:41` `ALLOWED_SIDE_EFFECTS` 集合（set_clue_status/create_decision/merge_entity/dismiss_review）。
   W-031 只需根据用户输入的扩展类型，生成对应代码桩文件（签名 + 契约 + 测试骨架 + 注册点说明），**不执行、不注册、不碰 core**。

7. **测试基建**：run_tests.py GROUPS 现 118 组（M4 后）；MCP 69 项须保持绿。M5 新增 3~4 组后约 121~122 组。无新第三方依赖（hashlib/json/shutil/zipfile 均标准库）。

---

## 二、文件与模块

### 新增（全部 server 内）

| 路径 | 内容 |
|---|---|
| `server/app/store/backend_cross.py` | W-026：将 `CrossCaseStore` ATTACH 实现从 backend.py 骨架独立至此（backend_api.md 目录结构明示 `backend_cross.py`）。实现 `read_conn`（ATTACH 各授权案件 READ_ONLY + 连接按 case 组合缓存）、`query`（禁 DDL/DML 白名单 + max_rows + 超时 + 结果截断）、`write_conn`（永远抛 UnsupportedOperation）、`close`（关闭主连接，ATTACH 库随之释放） |
| `server/app/routers/cross_case.py` | W-026/027：`POST /api/cross-case/query` 🔒（body: case_ids[]/sql/reason；全有或全无鉴权→ATTACH→执行→审计）、`GET /api/cross-case/history`（本用户查询记录，读 ops_events） |
| `server/app/routers/package.py` | W-028/029：`POST /api/cases/{cid}/package/export` 🔒⚡（入队 EXPORT 任务）、`GET /api/packages/{task_id}/download`（下载 zip）、`POST /api/packages/verify`（校验不上传包）、`POST /api/packages/import` 🔒⚡（校验通过后入队 IMPORT_PACKAGE） |
| `server/app/routers/escape_hatch.py` | W-031：`POST /api/escape-hatch/generate` 🔒（body: ext_type∈{function,value_type,clean_rule,side_effect}/name/description；生成代码桩文本返回，不写文件、不注册）、`GET /api/escape-hatch/stats`（触发统计，读 ops_events 聚合） |
| `server/app/worker/package.py` | W-028/029：`handle_export`（版本压实→审计链冻结→SHA-256 manifest→README→zip）、`handle_import_package`（verify→init_pack→元数据登记）、`verify_package` 纯函数（七步校验） |
| `server/app/worker/escape_hatch.py` | W-031：`generate_stub(ext_type, name, description)` 纯函数，返回 `{files: [{path, content}], registration_points: [...]}`，四类模板内置 |
| `tests/test_cross_case_api.py` | W-026/027 AC：ATTACH READ_ONLY、禁 DDL/DML、max_rows、超时、全有或全无鉴权（部分授权整体拒绝且 ATTACH 前）、查询进审计、连接缓存复用、不绕过 PolicyEngine |
| `tests/test_package_api.py` | W-028/029 AC：13 声明齐全+schema_version 一致、版本压实、SHA-256 manifest、审计链冻结、case_knowledge 标 sensitive、chain_ok=false 告警放行、README、篡改一字节校验失败、缺声明失败、schema_version 不一致失败、root_hash 不匹配失败、init_pack 复用、导入后可查询 |
| `tests/test_escape_hatch_api.py` | W-031 AC：四类扩展代码桩生成合法、含输入输出契约、含可运行测试骨架（初始失败）、含注册点说明、stats 统计、不执行不注册 |

### 修改（全部 server 内，core 零改动）

| 路径 | 改动 | 性质 |
|---|---|---|
| `server/app/store/backend.py` | `CrossCaseStore` 类的 `read_conn`/`query` 实现迁移至 `backend_cross.py`；`backend.py` 保留 `CrossCaseStore` 骨架 import 转发（或直接删除骨架，由 `backend_cross.py` 定义）。`StoreFactory.for_cross_case` 保持不变 | 实现迁移 |
| `server/app/store/__init__.py` | 导出 `CrossCaseStore`（从 backend_cross） | 薄追加 |
| `server/app/worker/tasks.py` | 新增 `TASK_EXPORT = "EXPORT"`、`TASK_IMPORT_PACKAGE = "IMPORT_PACKAGE"`；`HANDLERS` 注册两处理器（惰性导入 package.py） | 薄追加 |
| `server/app/main.py` | 挂载 cross_case/package/escape_hatch 三个新 router | server 内 |
| `run_tests.py` | 注册 3 个新测试组：crosscase/packageapi/escapehatch | 纯追加 |

**明确不改**（最小影响决策）：

- **core/ 零改动**：CrossCaseStore ATTACH、SHA-256、manifest、代码桩均为 server 层职责；PackManager/AuditChain/FUNCTION_IMPLS/TYPE_SQL/ALLOWED_SIDE_EFFECTS 全部复用既有接口。
- **不改 PolicyEngine**：W-027 AC-4（不绕过 PolicyEngine）通过 ATTACH 后查询经 `OntologyReadGateway` 走既有策略实现，不新增策略逻辑。
- **不改 ActionExecutor**：案件包导出/导入为任务编排，不走 Action 声明式通道（无 obj_decision 副作用）。
- **不引入新 core SQL**：ATTACH 是 DuckDB 连接级操作，非 Function SQL；禁 DDL/DML 在 server 层通过 SQL 首词白名单实现。
- **W-031 不写文件、不注册**：逃生舱仅返回代码桩文本，由开发者手动合入（req.md 明确"不做租户沙箱在线执行"）。
- **前端页面**：仅后端端点。

---

## 三、实施步骤（依赖顺序）

### 阶段 A：跨案件查询（W-026/027，ATTACH 实现）
1. `server/app/store/backend_cross.py`：实现 `CrossCaseStore` ATTACH 逻辑。
   - `read_conn`：惰性创建主内存连接，对每个授权案件执行 `ATTACH '<path>' AS case_<cid> (READ_ONLY)`；连接按 `frozenset(authorized_cases)` 缓存复用（AC-7）。
   - `query`：SQL 首词白名单（仅 SELECT/WITH/PRAGMA），禁 DDL/DML（AC-4）；`max_rows` 强制 LIMIT；超时用 `conn.execute("PRAGMA timeout=...")` 或线程级（AC-5）；返回 `list[dict]`。
   - `write_conn`：永远 `UnsupportedOperation`。
2. `server/app/routers/cross_case.py`：
   - `POST /query`：① 从会话取 operator/role；② 遍历 `case_ids` 经 MetaRepo 校验 membership（全有或全无，任一无权→403 + 审计拒绝事件，不 ATTACH）；③ `factory.for_cross_case(authorized_cases)`；④ `store.query(sql, max_rows, timeout)`；⑤ 审计链追加（case_ids + sql + reason）；⑥ 返回结果。
   - `GET /history`：读 ops_events 中本用户的 cross_case_query 事件。
3. 测试 + 注册。

### 阶段 B：案件包导出导入（W-028/029）
4. `server/app/worker/package.py`：
   - `verify_package(pkg_dir)`：七步校验——format(目录结构) → 逐文件 SHA-256 比对 manifest → 13 声明齐全 → schema_version 一致 → 审计链 root_hash 校验 → DuckDB 只读打开（`duckdb.connect(read_only=True)`）→ 返回 `{ok, errors}`。
   - `handle_export`：① 版本压实（复用 archive 逻辑或内联：删除非当前版本文件）；② 审计链冻结校验（`AuditChain.verify()`，chain_ok=false 时标记告警但继续）；③ 收集文件（13 声明 + 最终版 DuckDB + audit/chain.csv + artifacts + case_knowledge 标 sensitive）；④ 逐文件 SHA-256 生成 manifest.json；⑤ 生成 README（验证命令 + 复现命令）；⑥ zip 打包到 `cases/{cid}/exports/{task_id}.zip`。
   - `handle_import_package`：① 解压上传包到临时目录；② `verify_package`；③ `PackManager.init_pack(new_cid, from_pack=<临时目录/ontology>)`；④ MetaRepo 登记新案件（pack_id=new_cid, status=ACTIVE）；⑤ 返回 new_cid。
5. `server/app/routers/package.py`：export 入队 EXPORT、download 流式返回 zip、verify 同步校验、import 入队 IMPORT_PACKAGE。
6. `server/app/worker/tasks.py`：注册 TASK_EXPORT/TASK_IMPORT_PACKAGE。
7. 测试 + 注册。

### 阶段 C：代码逃生舱（W-031，纯文本生成）
8. `server/app/worker/escape_hatch.py`：`generate_stub(ext_type, name, description)` 纯函数，四类模板：
   - function：生成 `core/functions/<name>.py` 桩（`@register_function("<name>")` + 签名 + TODO + 测试骨架）+ 注册点说明（`core/functions.py` FUNCTION_IMPLS + `ontology/<pack>/functions.json` 声明）。
   - value_type：生成 `core/ontology.py` TYPE_SQL 追加示例 + loader 校验点说明 + 测试骨架。
   - clean_rule：生成 `core/functions/<name>_clean.py` 桩（clean 函数签名）+ 注册点说明（bindings.json clean 字段）。
   - side_effect：生成 `core/action_executor.py` 副作用处理桩 + `ALLOWED_SIDE_EFFECTS` 追加说明 + actions.json 声明模板。
9. `server/app/routers/escape_hatch.py`：generate 返回 `{files, registration_points}`，stats 读 ops_events 聚合。
10. 测试 + 注册。

### 阶段 D：收口
11. 全量回归（118+3 组）+ MCP 69 项。
12. 回填实施记录；backend_api.md M5 标已实施；git 分拣提交（排除 data/ 产物）。

---

## 四、决策点

| 编号 | 决策点 | 选项 | 推荐 |
|---|---|---|---|
| D-M5-1 | CrossCaseStore 实现位置 | A. 留在 backend.py 填充骨架<br>B. 独立 backend_cross.py（backend_api.md 目录结构明示） | **B**：与 backend_api.md `store/backend_cross.py` 结构一致，ATTACH 逻辑与单案件 CaseStore 分离，门禁（非 store 模块不得直连 DuckDB）更清晰 |
| D-M5-2 | ATTACH 连接缓存键 | A. 按 case_id 排序后的 frozenset<br>B. 按 case_ids 列表顺序 | **A**：跨案件查询的授权集合是无序的，同一组案件不同顺序应复用连接；frozenset 作为缓存键 |
| D-M5-3 | 禁 DDL/DML 实现 | A. SQL 首词白名单（SELECT/WITH/PRAGMA）<br>B. DuckDB `read_only` 连接级限制 | **A+B 结合**：主连接 ATTACH 的库均 READ_ONLY，DuckDB 层面已禁写；额外加 SQL 首词白名单做双保险（防 ATTACH 后对主内存连接写临时表） |
| D-M5-4 | 案件包导出压实策略 | A. 复用 handle_archive（要求案件已封存）<br>B. 导出前内联压实（仅删除非当前版本文件，不改案件状态） | **B**：导出不应强制要求案件封存；内联压实仅物理删除旧版本文件（与 archive 逻辑一致但不触发状态变更），导出后案件状态不变 |
| D-M5-5 | 导入后案件 pack_id | A. 新案件 pack_id = 包内声明的 pack 名<br>B. 新案件 pack_id = new_cid（案件级隔离） | **B**：与 M3/M4 案件快照隔离一致——每个案件有独立的 ontology 快照目录，pack_id 用案件 ID，导入的声明文件复制到 `cases/{new_cid}/ontology/{pack_id}/` |
| D-M5-6 | 逃生舱产物形态 | A. 直接写文件到 core/<br>B. 返回代码桩文本（files[]），由开发者手动合入 | **B**：req.md 明确"不做租户沙箱在线执行"；生成文本返回，注册点说明指引开发者手动修改 core，server 不碰 core |
| D-M5-7 | 跨案件查询是否走 PolicyEngine | A. ATTACH 后直接 query，不经 PolicyEngine<br>B. ATTACH 后经 OntologyReadGateway 走 PolicyEngine | **A（MVP）**：跨案件查询是原始 SQL 通道（用户显式写 SQL），PolicyEngine 的行级/字段级遮蔽在跨库 JOIN 场景下难以统一应用；W-027 AC-4 要求"不绕过"理解为**不削弱**——单案件查询仍走 PolicyEngine，跨案件通道本身是特权操作（需全有或全无授权 + 审计），由授权层而非引擎层控制。文档明确标注此通道为"高级分析特权"。 |

---

## 五、验收标准映射（W → 端点/测试）

| W | 端点 | 核心 AC | 测试组 |
|---|---|---|---|
| W-026 | POST /cross-case/query | ATTACH READ_ONLY、禁 DDL/DML、max_rows、超时、查询进审计、连接缓存 | crosscase |
| W-027 | POST /cross-case/query | 全有或全无鉴权（部分授权整体拒绝）、拒绝在 ATTACH 前、不绕过 PolicyEngine、拒绝进审计 | crosscase |
| W-028 | POST /cases/{cid}/package/export | 13 声明+schema_version 一致、版本压实、SHA-256 manifest、审计链冻结、case_knowledge 标 sensitive、chain_ok=false 告警放行、README | packageapi |
| W-029 | POST /packages/verify, /import | 篡改一字节失败、缺声明失败、schema_version 不一致失败、root_hash 不匹配失败、init_pack 复用、导入后可查询 | packageapi |
| W-031 | POST /escape-hatch/generate, GET /stats | 四类代码桩合法、含输入输出契约、含可运行测试骨架（初始失败）、含注册点说明、stats 统计 | escapehatch |

---

## 六、测试与验证

- 新增 3 组测试（crosscase/packageapi/escapehatch），预计 ~50 例。
- 全量 `run_tests.py`（118+3=121 组）须全绿。
- MCP `scripts.mcp_client_test` 69 项须保持绿（core 零改动，MCP 不受影响）。
- W-027 AC-2 红线（拒绝在 ATTACH 前）需显式断言：构造部分授权场景，断言 CrossCaseStore 未被实例化（mock `for_cross_case` 验证未调用）。
- W-029 AC-2（篡改一字节失败）需显式断言：导出包后修改 manifest 或任一文件一个字节，verify 返回失败。
- W-031 AC-3（测试骨架初始失败）需显式断言：生成的测试文件含 `pytest.fail("TODO: implement")` 或等效断言。

---

## 七、遗留与风险

- **W-026 GB 级大规模未验证**：req.md 明确标记为未验证项。M5 仅做小规模 AC 覆盖（spike H5-H7 的 50 库×1000 行），GB 级性能留后续专项验证。
- **D-M5-7 跨案件 PolicyEngine 适用范围**：跨案件 SQL 通道不做行级/字段级遮蔽，依赖全有或全无授权 + 审计。若后续要求字段级遮蔽，需在 query 结果后追加 PolicyEngine 逐行处理（性能代价大），M5 不做。
- **ATTACH 连接缓存的内存压力**：每个授权案件 ATTACH 后占用连接资源，缓存键为案件集合，最坏情况组合数爆炸。M5 MVP 缓存大小设上限（如 16），LRU 淘汰；超限时关闭最久未用连接。
- **案件包导出的版本压实副作用**：导出前删除旧版本文件是物理操作，若导出失败可能导致旧版本丢失。M5 采用"先复制到临时导出目录再压实"策略——不碰原案件文件，压实仅作用于导出副本。
- **W-031 代码桩质量**：生成的代码桩是模板化文本，无法保证与 core 当前实现 100% 一致（如 FUNCTION_IMPLS 注册方式变化）。M5 模板基于 M4 交付时的 core 现状，若 core 后续变更需同步更新模板。

---

## 八、实施记录

（待实施完成后回填）
