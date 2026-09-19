# 图像证据轻量回流：书证材料卡挂 findings（含发起入口前移）实施计划

> v2 轻量版。v1 重方案（画布新节点 kind / 系统边 / GET reconcile / 线索详情独立卡片区）经用户评审否决——「改动太重」。本版按用户确认的三决策收敛。

## 已确认决策（用户 2026-09-19）

1. **发起入口搬到书证材料卡**：线索工作面材料卡上加「AI 图像分析」，区内直接发起（仍走原 draft 闸门：content_class 门禁 / LLM 策略 / 提案草案）。
2. **待核草案也显示**：材料卡下 findings 子列表同时呈现 AI 草案（待办样式）与已人验 finding，支持材料旁内联核验，形成「发起→待核→人验」同处闭环。
3. **类型层同批加列**：objects.json 的 image_evidence 补 title/detail/severity，与 state 表、内核 persist 三处同构。

## 核心设计

- **归属键 = image_uri 反解 material_id**：image_uri 固定 `evidence/<material_id>/<文件名>`（vlm.py `_make_image_loader` 同口径），材料卡天然分组，不需要新关联表/边。
- **线索归属由材料决定**：clue_evidence.clue_id NOT NULL，finding 一定出现在其材料所在线索；核验时手填 clue_id 降级为图谱/报告归组辅助（保持可选）。
- **画布本批不动**（后续可选：书证节点加「图像发现 N」角标）；VlmView 页保留作为全案入口。

## Repository Research（锚点）

- `server/app/store/state_store.py`：image_evidence DDL ~L195-209（无文案列）；`list_image_evidence(case_id=, clue_id=)` 已有；旧库幂等补列先例 `_migrate_verify_item_columns`（PRAGMA 探测 + ALTER ADD）；`_read_state` 可能为 None（读面不建库）。
- `server/app/routers/vlm.py`：verify ~L200-220 写 `state.insert_image_evidence(...)` + `ps.decide(approve)`；`rec["payload"]["candidate"]` 含 title/detail/severity（核验写库时仍可取）；draft 端点参数 content_class / image_uri / clue_id（可选）/ subject（可选）。
- `core/action_executor.py::persist_image_evidence`：内核 Action verify_image 同构写 obj_image_evidence + lnk_image_for_*；需对旧 DuckDB 表幂等补列（information_schema.columns 探测 → ALTER ADD VARCHAR）。
- `ontology/default/objects.json`：image_evidence ~L224-255；`ensure_runtime_tables()` 仅 CREATE IF NOT EXISTS，不补列。
- `.trae/skills/sunzi-report/scripts/gather_evidence.py::collect_image_evidence` ~L415-446 显式列 SELECT，旧库缺列会整段降级 → 改按实际列查询。
- 前端：`components/research/VerifyWorkbench.vue` = 线索工作面书证面板（材料卡；其取数端点待定位）；`views/VlmView.vue` preview 内联 `split('/')[1]`；`evidenceApi.download(caseId, clueId, materialId)`；`vlmApi.draft/verify/list`。

## Files and Modules

### 后端

- `server/app/store/state_store.py`：DDL 加三列（NOT NULL DEFAULT ''）；新增 `_migrate_image_evidence_columns()` 构造时调用；`insert_image_evidence` 增 title/detail/severity 参数。
- `ontology/default/objects.json`：image_evidence properties/metadata_props 加三属性（string）。
- `core/action_executor.py`：persist_image_evidence 增三可选参；obj_image_evidence 幂等补列。
- `server/app/routers/vlm.py`：verify 从 candidate 取三字段透传。
- 书证面板取数端点（实现时定位 VerifyWorkbench 数据源，clues/verify 路由）：返回按 material_id 分组的 `image_findings`：pending（image_draft 提案，按 image_uri 前缀匹配）+ verified（list_image_evidence 行）；最小接线，优先搭既有响应，必要时独立轻端点。
- `gather_evidence.py`：按 cursor.description 白名单交集取列，新列存在即带出。

### 前端

- `src/domain/imageEvidence.ts`（新建小工具）：`materialIdFromUri(uri)`；VlmView.preview 与材料卡共用。
- `components/research/VerifyWorkbench.vue`：材料卡加「AI 图像分析」（content_class 选择复用 VlmView 现有选项，默认取材料已知类别）；材料卡下 findings 子列表——待核：severity/标题/明细 + 内联核验表单（主体类型必选、主体 ID 必填、结论必填，clue_id 预填当前线索）；已人验：标题/明细/warn 标记/核验人/结论/时间/查看图像（evidenceApi.download）。
- `views/VlmView.vue`：preview 改用 materialIdFromUri；页面其余不动。

### 测试

- tests/test_vlm_pack.py：verify 后行含 candidate 三字段。
- state_store 迁移用例；action_executor 补列/透传用例。
- 书证面板端点分组用例（pending+verified、uri 前缀匹配、state 缺失空）。
- 前端 spec：材料卡发起按钮、findings 渲染、内联核验提交、空态。
- golden：objects.json 属性新增可能动 ontology 基线，统一刷新。

## Implementation Steps

1. state 层：三列 + 迁移 + insert 参数 + 单测。
2. objects.json 加属性；persist_image_evidence 三参 + obj_image_evidence 幂等补列。
3. vlm.py verify 透传；vlm_pack 用例。
4. 定位书证面板取数端点 → 分组数据接线 + 用例。
5. gather_evidence.py 列兼容。
6. 前端 materialIdFromUri + 材料卡 findings/发起/内联核验 + VlmView preview 改造。
7. 前端 spec + vue-tsc；golden 刷新。
8. 收口：改动摘要 + 可选验证命令（**不自动跑测试**——项目纪律）。

## Validation（用户指令后执行）

- WSL：`/root/.venvs/inves/bin/python run_tests.py --only vlmpack`、`--only sunzireport`、`--only ontology`（组名以 run_tests.py GROUPS 注册表为准）。
- 若 actions.json 参数声明有变：`python -m scripts.mcp_client_test`。
- 前端（Windows 侧 frontend/）：`npm test`、`npm run build`。
- 手工链路：材料卡发起 → 草案挂材料下待办 → 旁核验（填主体+结论）→ 已人验样式 + 查看图像 → 报告含 finding 文案。

## Risks

- 旧库缺列：state（sqlite 幂等 ALTER 先例）与 obj_image_evidence（探测补列）双迁移。
- golden 漂移：步骤 7 统一刷新并在改动摘要列明。
- draft 契约：content_class 白名单，材料卡发起需明确默认值/选择器。
- 内联核验与 VlmView 核验行同契约（主体必选必填，前端已加固）。
- 范围冻结：画布 / 线索详情独立卡片区 / 线索列表聚合均不做。
