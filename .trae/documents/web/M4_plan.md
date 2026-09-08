# M4 配置与人审里程碑实施计划（REQ-W-011/013/015/016/017/021/022）

**日期** 2026-09-08 ｜ **状态** 待评审
**关联** [backend_api.md](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md)（批次表：M4=批 6；G/H 组端点契约）、[M3_plan.md](file:///d:/dev/inves_duckdb/.trae/documents/web/M3_plan.md)（已实施，2026-09-08）、[req.md](file:///d:/dev/inves_duckdb/.trae/documents/web/req.md)（W-011/013/015/016/017/021/022 AC 原文）
**范围** 批 6 七项：W-011 可选属性缺列降级（数据治理页）、W-013 对象模型设计器、W-015 权限与字段遮蔽配置、W-016 知识包维护、W-017 角色视图配置、W-021 人审队列与实体裁决、W-022 异常线索通道查看。
**不含** 跨案件 ATTACH（M5）、案件包导入导出（M5）、Postgres 实现、代码逃生舱（W-031，M5）、真实 LLM 模型接入（M3 仅守卫层）、前端页面（本计划仅后端端点与任务）。

**最小影响红线（继承 M1/M2/M3 首要原则）**：M4 对 server 外存量代码的改动目标为 **core 零或极少量可选追加**。七项中六项（W-011/013/015/016/017/022）core 能力已就绪，可纯 Web 实现；仅 **W-021 实体裁决**需 core 补实体合并/驳回执行函数（纯追加，不改既有 entity 解析逻辑）。沿用 M3 范式：配置类读写走「临时副本 + load_pack 全量校验 + os.replace 原子落盘」，不碰 core 引擎；写动作走 ActionExecutor 声明式 Action；core 不 import server。

---

## 一、仓库研究结论（M3 交付现状与 M4 依赖锚点）

1. **配置类 core 支撑已完整**：`core/ontology.py`（ObjectSpec/LinkSpec）、`core/policy.py`（PolicyEngine 对象级/链接级/属性级遮蔽，fail-closed）、`core/views.py`（Object Views 按角色投影，REQ-046，不绕过 PolicyEngine）、`core/ontology_loader.py`（13 声明文件全量校验，未知名/版本不符硬失败）、`case_knowledge.json` 装载（r5knowledge 组通过）。**M4 配置类端点（W-013/015/016/017）只需 Web 读写这些 JSON + 复用 loader 校验，零 core 改动。**

2. **W-011 缺列降级 core 已实现**：`core/ontology_loader.py` 编译期对源列缺失走 `source_column_missing` 警告降级（脏值 `source_value_cast_failed`），不中断 build；`dirtydate`/`misscol` 测试组已通过。M4 只需 Web 端在数据治理页展示这些诊断（经 M2 已交付的 `GET /cases/{cid}/diagnostics` 端点）。**零 core 改动。**

3. **W-022 异常通道 core 已实现**：`core/anomaly_channel.py` 完整实现（`emit_anomaly_clues`/`partition`/`non_anomaly`），`anomalychannel` 组通过；红线 AC-4（不参与五间交叉等级）已在 core 硬编码。M4 只需 `GET /cases/{cid}/anomalies` 读端点。**零 core 改动。**

4. **W-021 人审队列读面已有、执行面缺失**：`core/entity.py` 有 `needs_review` 标志与 `review_candidates()` 方法（[entity.py:291-293](file:///d:/dev/inves_duckdb/core/entity.py#L291)），可读候选；但**无 merge/reject 执行函数**（grep `merge_entit|reject_review` 无匹配），`ontology/default/actions.json` 也未声明裁决动作（仅有 file 动作）。M4 需：① core 追加 `merge_entities`/`reject_review` 函数（纯追加，不改解析逻辑）；② actions.json 声明 `review_merge`/`review_reject` 动作；③ Web 三端点（队列/证据/裁决）。

5. **配置写入目标应案件快照化（D-M4-1）**：M3 rule_workshop 已确立「规则写案件快照 `cases/{cid}/ontology/default/rules.json`」模式（多租户隔离、loader 校验、原子落盘）。backend_api.md G 组端点写作 `/api/packs/{pack}/...`（共享包级），但 M3 实际落地为 `/api/cases/{cid}/rules`（案件级）。**M4 配置类（objects/links/policies/views/knowledge）应统一案件快照级**，与 M3 一致——每个案件建案时锁定的 13 声明文件快照，编辑写快照内对应 JSON。这是 M4 关键决策点（见第四节 D-M4-1）。

6. **写路径接线已就绪（M3 交付）**：`ActionExecutor(sink=StateSink)` 走 state.sqlite；W-021 裁决写（合并/驳回）复用此通道，落 `state.review_decision` + 审计链，不产 DuckDB 版本。

7. **遮蔽执行已在序列化前完成（M2）**：[backend_api.md:359](file:///d:/dev/inves_duckdb/.trae/documents/web/backend_api.md#L359) 字段遮蔽由 PolicyEngine 在 API 序列化前完成。W-015 配置端点只需读写 policies.json，生效由既有 PolicyEngine 保证（AC-6 无需重建语义层）。

8. **测试基建**：run_tests.py GROUPS 现 111 组（M3 后）；MCP 69 项须保持绿。M4 新增 4~5 组后约 115~116 组。无新第三方依赖（pandas/sqlalchemy 已在 venv）。

---

## 二、文件与模块

### 新增（全部 server 内）

| 路径 | 内容 |
|---|---|
| `server/app/routers/model_designer.py` | W-013：`GET /cases/{cid}/objects`、`PUT /cases/{cid}/objects` 🔒、`GET/PUT /cases/{cid}/links` 🔒、`POST /cases/{cid}/validate`。值类型白名单（TYPE_SQL 5 种）、kind=entity/event 提示、from_obj/to_obj 引用校验、间类五间校验——全部经 `load_pack` 临时副本强校验后原子落案件快照 objects.json/links.json |
| `server/app/routers/access_config.py` | W-015：`GET /cases/{cid}/policies`、`PUT /cases/{cid}/policies` 🔒。对象级（角色+min_clearance）、字段级遮蔽（default/allow_roles/mask）、链接级策略；fail-closed 未声明=拒绝；保存即生效（PolicyEngine 读时执行，不重建语义层） |
| `server/app/routers/knowledge.py` | W-016：`GET /cases/{cid}/knowledge`、`POST/PUT /cases/{cid}/knowledge` 🔒。断言（主体/关系/客体/valid_until/来源）增改停用；过期断言扫描自动排除（core 既有）；变更进审计链；导出标 sensitive（M5） |
| `server/app/routers/views.py` | W-017：`GET/PUT /cases/{cid}/views` 🔒。视图定义（对象+列+角色）→ 生成 views.json；引用列必须已声明；视图唯一读入口 `OntologyReadGateway.view(name)`，不绕过 PolicyEngine |
| `server/app/routers/anomaly.py` | W-022：`GET /cases/{cid}/anomalies`。读 `anomaly_channel` 产物（同构 findings，is_anomaly=True），按主体聚合，级别恒"待核实"、needs_human_review=True、携带 diagnostic_ids；**不混入正常线索流**（AC-4，core partition 已保证，Web 仅分区展示） |
| `server/app/routers/review.py` | W-021：`GET /cases/{cid}/review/queue`（needs_review 候选全量）、`GET /cases/{cid}/review/{rid}/evidence`（双方属性三列对比+差异行+相似度依据）、`POST /cases/{cid}/review/{rid}/decision` 🔒（合并/驳回+理由，入队 DISPOSE 复用写通道，落 state.review_decision + 审计链；驳回后不重复出现；AC-5 红线：永不自动合并） |
| `server/app/routers/data_governance.py` | W-011：`GET /cases/{cid}/governance/missing-columns`（聚合 diagnostics 中 source_column_missing/source_value_cast_failed，按对象/属性分组展示降级警告）。纯读，复用 M2 diagnostics 端点数据源 |
| `tests/test_model_designer.py` | W-013 AC-1~6：新建对象过 loader；值类型仅 5 种；kind 语义提示；链接端点引用校验；间类仅五间；产物与手写 JSON 等价 |
| `tests/test_access_config.py` | W-015 AC-1~6：对象级配置；字段遮蔽配置过 loader；未声明=拒绝；不同角色同对象结果不同；mask 类型白名单；保存即生效 |
| `tests/test_knowledge_api.py` | W-016 AC-1~5：断言增改停用过 loader；过期自动排除；零硬编码；白名单可被规则读；变更进审计 |
| `tests/test_views_api.py` | W-017 AC-1~5：视图生成合法 views.json；物化 v_<name> 不复制数据；读入口唯一；不绕过 PolicyEngine；引用列必须已声明 |
| `tests/test_anomaly_api.py` | W-022 AC-1~6：异常与正常分区；级别恒待核实；携带 diagnostic_ids；不参与五间交叉（等级不变）；按主体聚合；同构进审计 |
| `tests/test_review_api.py` | W-021 AC-1~6：needs_review 候选全量入队；相似度依据+属性对比；裁决进审计；驳回不重复；永不自动合并（红线）；演示模式 auto-accept 重名场景关闭/告警 |
| `tests/test_data_governance.py` | W-011 AC-1~5：缺列降级警告可检索；必填缺列仍硬失败；其余属性正常物化；降级后可查询缺列为 NULL；不崩溃 |

### 修改（core 仅 W-021 追加 + server 既有文件）

| 路径 | 改动 | 性质 |
|---|---|---|
| `core/entity.py` | 追加 `merge_entities(primary, alias)` / `reject_review(candidate_id, reason)` 纯函数：合并将 alias 实体的链接/属性归并到 primary 并标 alias 为已合并；驳回将候选标 dismissed 不重复入队。**不改既有 `review_candidates()`/解析逻辑**；签名追加，缺省行为不变 | 纯追加函数 |
| `ontology/default/actions.json` | 声明 `review_merge`（side_effects: merge_entity + create_decision）、`review_reject`（side_effects: dismiss_review + create_decision）两个动作；角色要求正兵+。**声明式，非 core 代码** | 声明追加 |
| `core/action_executor.py` | 若 `_validate()` 已覆盖新动作参数校验则零改动；若需对 review_merge 特判（如双方必须同类型），追加最小校验分支，不改既有动作校验 | 可能零改动 |
| `server/app/worker/tasks.py` | W-021 裁决复用 DISPOSE 快速通道（action=review_merge/review_reject），不新增任务类型；`handle_dispose` 已有五动作分派，追加两动作映射到 `merge_entities`/`reject_review` | 薄追加 |
| `server/app/main.py` | 挂载 model_designer/access_config/knowledge/views/anomaly/review/data_governance 七个新 router | server 内 |
| `run_tests.py` | 注册 7 个新测试组：modeldesigner/accessconfig/knowledgeapi/viewsapi/anomalyapi/reviewapi/datagov | 纯追加 |

**明确不改**（最小影响决策）：

- **W-011 不改 core 降级逻辑**：dirtydate/misscol 组已覆盖，M4 仅 Web 展示诊断。
- **W-013/015/016/017 不改 core 引擎**：PolicyEngine/views/loader 全部复用，配置变更仅写 JSON。
- **W-022 不改 core 异常通道**：anomaly_channel 产物直接消费，分区展示在 Web。
- **不改既有 entity 解析/聚类逻辑**：W-021 只追加合并/驳回执行函数，`needs_review` 判定与候选产出逻辑零改动。
- **不引入新 core SQL**：所有写走 ActionExecutor 声明式 Action + StateSink。
- **W-031 代码逃生舱**：M5，本批不做。
- **前端页面**：仅后端端点。

---

## 三、实施步骤（依赖顺序）

### 阶段 A：配置类读写（W-013/015/016/017，复用 rule_workshop 范式）
1. 抽公共 `snapshot_config_router` helper：临时副本 → 改目标 JSON → `load_pack` 全量校验 → `os.replace` 原子落盘 → 记 ops_events。
2. model_designer.py（objects/links/validate）。
3. access_config.py（policies）。
4. knowledge.py（case_knowledge）。
5. views.py（views）。
6. 四组测试 + 注册。

### 阶段 B：异常通道与数据治理（W-022/011，纯读）
7. anomaly.py（读 anomaly_channel 产物，分区展示）。
8. data_governance.py（聚合 diagnostics 缺列降级警告）。
9. 两组测试 + 注册。

### 阶段 C：人审裁决（W-021，唯一 core 触点）
10. core/entity.py 追加 `merge_entities`/`reject_review`。
11. actions.json 声明 review_merge/review_reject。
12. review.py 三端点（queue/evidence/decision），裁决入队 DISPOSE。
13. worker/dispose.py 追加两动作分派。
14. 测试 + 注册。

### 阶段 D：收口
15. 全量回归（111+7 组）+ MCP 69 项。
16. 回填实施记录；backend_api.md M4 标已实施；git 分拣提交。

---

## 四、决策点

| 编号 | 决策点 | 选项 | 推荐 |
|---|---|---|---|
| D-M4-1 | 配置写入作用域 | A. 共享包级 `/api/packs/{pack}/...`（backend_api.md 原文）<br>B. 案件快照级 `/api/cases/{cid}/...`（M3 rule_workshop 模式） | **B**：与 M3 一致，多租户隔离，每个案件快照独立；共享包级改动会影响所有案件，违反案件隔离原则。backend_api.md G 组路径在实施时调整为案件级 |
| D-M4-2 | W-021 裁决写通道 | A. 新建 REVIEW 任务类型<br>B. 复用 DISPOSE 快速通道（action=review_merge/review_reject） | **B**：裁决是短频写，与处置同性质，复用 state.sqlite 写通道与审计链，不新增任务类型 |
| D-M4-3 | W-021 合并语义 | A. 物理删除 alias 实体<br>B. 软合并：alias 标 merged_into=primary_id，链接归并，保留可溯源 | **B**：审计可溯源、可回滚，符合"不下定性结论"原则（合并是人审裁定，留痕） |
| D-M4-4 | W-011 数据治理页 | A. 新建专属端点<br>B. 复用 M2 diagnostics 端点，前端聚合 | **A（轻量）**：新建 `GET /cases/{cid}/governance/missing-columns` 做服务端聚合（前端零计算），底层仍读 diagnostics，不重复存储 |

---

## 五、验收标准映射（W → 端点/测试）

| W | 端点 | 核心 AC | 测试组 |
|---|---|---|---|
| W-011 | GET /cases/{cid}/governance/missing-columns | 缺列降级警告可检索、必填缺列仍硬失败、降级后可查询 | datagov |
| W-013 | GET/PUT /cases/{cid}/objects, /links, /validate | 值类型 5 种、kind 提示、链接端点引用校验、间类五间、与手写 JSON 等价 | modeldesigner |
| W-015 | GET/PUT /cases/{cid}/policies | 对象级+字段遮蔽、未声明=拒绝、不同角色结果不同、保存即生效 | accessconfig |
| W-016 | GET/POST/PUT /cases/{cid}/knowledge | 断言增改停用、过期自动排除、零硬编码、变更进审计 | knowledgeapi |
| W-017 | GET/PUT /cases/{cid}/views | 视图合法、物化不复制数据、入口唯一、不绕过 PolicyEngine、引用列已声明 | viewsapi |
| W-021 | GET /review/queue, /review/{rid}/evidence, POST /review/{rid}/decision | 候选全量、属性对比、裁决进审计、驳回不重复、永不自动合并（红线） | reviewapi |
| W-022 | GET /cases/{cid}/anomalies | 分区展示、级别待核实、diagnostic_ids、不参与五间交叉、按主体聚合 | anomalyapi |

---

## 六、测试与验证

- 新增 7 组测试（modeldesigner/accessconfig/knowledgeapi/viewsapi/anomalyapi/reviewapi/datagov），预计 ~70 例。
- 全量 `run_tests.py`（111+7=118 组）须全绿。
- MCP `scripts.mcp_client_test` 69 项须保持绿（core entity 追加函数不影响 MCP 既有工具）。
- W-021 红线 AC-5（永不自动合并）需显式断言：构造 needs_review 候选，断言不经 POST decision 不发生合并。

---

## 七、遗留与风险

- **W-021 合并后链接归并的并发安全**：合并写 state.sqlite，经案件 FIFO 队列串行，无并发问题；但合并后若 alias 实体仍被旧线索引用，需线索侧按需 lazy 解析（entity.py 既有 alias 机制）。
- **W-015 遮蔽实时预览**：PolicyEngine 若不支持单行预览，需追加只读 `preview_mask(row, role)` 方法（纯追加，不改既有遮蔽逻辑）——实施阶段核对。
- **W-013 模型设计器删除对象**：删除对象会破坏已声明链接/规则引用，M4 MVP 仅支持新增/编辑，删除留 M5（需级联校验）。
- **案件快照与共享包的同步**：M4 写案件快照，共享 `ontology/default/` 作为模板不变；新建案件时复制模板（W-005 既有机制）。

---

## 八、实施记录

（2025-01 实施完成回填）

### 阶段 A：配置类读写 W-013/015/016/017 — ✅ 26 例全绿

**新增**：
- `server/app/snapshot_config.py`：共享助手 `snapshot_paths`/`require_analyst`/`atomic_write_json`/`validate_snapshot`/`save_config_json`
- `server/app/routers/model_designer.py`：GET/PUT `/cases/{cid}/objects`、`/links`、POST `/validate`
- `server/app/routers/access_config.py`：GET/PUT `/cases/{cid}/policies`
- `server/app/routers/knowledge.py`：GET/PUT/POST `/cases/{cid}/knowledge`
- `server/app/routers/views.py`：GET/PUT `/cases/{cid}/views`

**修改**：`server/app/main.py`（挂载 4 router）、`run_tests.py`（注册 modeldesigner/accessconfig/knowledgeapi/viewsapi）

**测试**：`test_model_designer.py`(10)、`test_access_config.py`(5)、`test_knowledge_api.py`(6)、`test_views_api.py`(5)

**偏差**：
- 值类型用 `set(TYPE_NAMES)` 9 种（非 AC 描述 5 种），与 core TYPE_SQL(REQ-041) 口径一致
- 视图引用列含 `pk`（代理键也是视图可引用列）
- policies 缺策略 load_pack 不拒绝（fail-closed 是运行时 PolicyEngine 行为，非装载期）

### 阶段 B：W-022 异常通道 + W-011 数据治理 — ✅ 6 例全绿

**新增**：
- `server/app/routers/anomaly.py`：GET `/cases/{cid}/anomalies`（`_AllRowsHealth` 直接 SELECT run_diagnostic，避开 RunHealth DDL）
- `server/app/routers/data_governance.py`：GET `/cases/{cid}/governance/missing-columns`

**测试**：`test_anomaly_api.py`(3)、`test_data_governance.py`(3)

**偏差**：诊断 object/property 在 `detail` JSON 内（非顶层列），聚合从 detail 提取

### 阶段 C：W-021 人审裁决 — ✅ 6 例全绿（唯一 core 触点）

**core 追加**：
- `core/entity.py`：`merge_entities(resolver, candidate_id)` / `reject_review(resolver, candidate_id, reason)` 纯函数（只改内存 resolver，返回裁决记录）
- `core/ontology_loader.py`：`ALLOWED_SIDE_EFFECTS` 追加 `merge_entity`/`dismiss_review`
- `ontology/default/actions.json`：声明 `review_merge`/`review_reject` 动作

**server 新增**：
- `server/app/worker/review.py`：`handle_review`（构建 resolver → 调 core 纯函数 → 落 state.review_decision + 审计链，不产版本）
- `server/app/worker/tasks.py`：注册 `TASK_REVIEW`
- `server/app/routers/review.py`：GET `/review/queue`、GET `/review/{rid}/evidence`、POST `/review/{rid}/decision`🔒

**测试**：`test_review_api.py`(6) — AC-1 队列入队 / AC-2 证据面 / AC-3 裁决进审计 / AC-4 驳回后不重复 / AC-5 永不自动合并 / 驳回需理由

**偏差**：
- 实体裁决不走 DisposalBoard（线索板），单独 `TASK_REVIEW` 任务落 state.review_decision；actions.json 的 review_merge/review_reject 为声明式文档（target_status=待查 占位，不实际迁移线索状态）
- 候选从 obj_org 实时构建 OrganizationResolver，排除已裁决候选（state.review_decision）

### 阶段 D：收口

- 全量回归 `run_tests.py` 118 组全绿
- MCP `scripts.mcp_client_test` 69 项
- git 分拣提交（排除 data/ 产物）

### core diff 实际触点汇总
1. `core/entity.py`：+`merge_entities`/+`reject_review`（纯函数，不改解析逻辑）
2. `core/ontology_loader.py`：`ALLOWED_SIDE_EFFECTS` 追加 2 项副作用标记
3. `ontology/default/actions.json`：+2 动作声明
