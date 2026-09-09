# MVP-4 · 能配置 实施方案（后端契约核对版）

> 版本 v1.0 ｜ 日期 2026-09-09
> 依据：`MVP迭代计划.md` v1.5（MVP-4 第 13–16 周）· `前端需求清单.md` v1.8（FE-P-003/012/013/014/018/019/020/021/022）· `配置中心需求.md` v1.1 · `配置中心后端API核对报告.md` v1.1
> 核对方式：逐行核对 `server/app/routers/` 实际路由代码（非 backend_api.md 文本）
> 状态：**四项关键决策已经用户确认定案（见第五节）**

---

## 一、总体结论

**后端就绪度远超需求清单 v1.8 的标注**：清单中 5 个标 ⛔ 待补的治理端点（data-elements / etl-pipeline / validate / quality-checks / quarantine+clean-trace）**代码均已实现**；规则/模型/权限/知识四个研判模型页端点也全部就绪。九页**无一页缺端点**，但有 4 处能力缺口已经决策定案（第五节）。

通用事实：

- 全局前缀 `/api/v1`，响应信封 `ok({...}, data_version=...)`；写权限门槛统一为 **clearance≥2（偏将及以上）**（`snapshot_config.require_analyst` / 各路由内 `_require_analyst`）。
- 角色 rank：见习 0 / 正兵 1 / 偏将 2 / 主办 3 / human 4（`core/access.py` ROLE_RANK）。
- 配置写全部走「临时副本过 `core.ontology_loader.load_pack` 全量校验 → 原子写案件快照 → `repo.record_ops` 审计」范式，不合法 400 且不落盘。
- 配置数据均为**案件快照级** `/cases/{cid}/...`（非 pack 级）；快照目录由 `snapshot_paths(ctx, case_id)` 解析。
- 前端 API 层（`frontend/src/api/endpoints/`）目前**无任何配置类封装**，九页全部新建；侧栏六分组导航（`nav/sections.ts`）已预留 data-elements/etl/mapping/quality/quarantine/designer/rules/masking 八个占位 key，**知识包 nav key 缺失需补**。

---

## 二、九页端点对照（均已实现 ✅）

| 页面 | 端点（实际路由，前缀 `/api/v1`） | 代码位置 |
|---|---|---|
| FE-P-003 规则工坊 | `GET /cases/{cid}/rules` · `PUT /cases/{cid}/rules/{rid}` 🔒 · `POST /cases/{cid}/rules/draft` 🔒 | `server/app/routers/rule_workshop.py` |
| FE-P-012 模型设计器 | `GET/PUT /cases/{cid}/objects` 🔒 · `GET/PUT /cases/{cid}/links` 🔒 · `POST /cases/{cid}/validate` | `server/app/routers/model_designer.py` |
| FE-P-013 权限与遮蔽 | `GET/PUT /cases/{cid}/policies` 🔒 · `GET/PUT /cases/{cid}/views` 🔒 | `server/app/routers/access_config.py` · `server/app/routers/views.py` |
| FE-P-014 知识包 | `GET /cases/{cid}/knowledge` · `POST/PUT /cases/{cid}/knowledge` 🔒 | `server/app/routers/knowledge.py` |
| FE-P-018 数据元 | `GET/PUT /cases/{cid}/data-elements` 🔒 | `server/app/routers/etl.py` L73–94 |
| FE-P-019 ETL 管道 | `GET/PUT /cases/{cid}/etl-pipeline` 🔒 | `server/app/routers/etl.py` L128–182 |
| FE-P-020 映射校验 | `POST /cases/{cid}/etl-pipeline/validate` · `GET /cases/{cid}/governance/missing-columns` | `server/app/routers/etl.py` L185–248 · `data_governance.py` |
| FE-P-021 质量检查 | `POST /cases/{cid}/quality-checks` 🔒⚡（202）· `GET /cases/{cid}/quality-checks/latest` | `server/app/routers/quality.py` |
| FE-P-022 隔离区 | `GET /cases/{cid}/quarantine?reason=&page=&page_size=` · `GET /cases/{cid}/clean-trace?object=&page=&page_size=` | `server/app/routers/quality.py` L35–88 |

---

## 三、以后端为准的契约要点（前端类型按此定义）

1. **规则工坊**
   - PUT body 仅 `{rule_text?: string, params?: object, enabled?: boolean}` 三字段；`function/stage/hit_when/jian_types/dimension/title` 等结构字段**只读**（提交即 400）。
   - PUT 返回 `{rule_id, changed: string[], rescan_task: TaskRow | null}`：改 `params/enabled` 自动入队 RESCAN（幂等键 `rescan:{rid}:{version}`），纯 `rule_text` 文本修订不重跑。
   - `GET rules` 响应：`{rules: Rule[], function_catalog: string[], pack}`。**函数目录取 `function_catalog` 字段，无独立 functions 端点**（侧栏"函数目录"并入规则工坊只读 tab）。
   - rule_text 最少 20 字（须写明模式/反常理由/边界排除）。
   - draft 是 LLM 守卫层：默认 `llm_enabled=false` 返 503 `LLM_DISABLED`；无模型通道返 503 `LLM_CHANNEL_UNAVAILABLE`；注入特征 400；模型输出只许 `rule_text` 键（含 function/params/SQL/action 一律 400），产物标"待核实"、永不落盘。前端按"守卫已就位、通道未接线"做态。

2. **权限与遮蔽（policies）**
   - `object_policies: [{object, roles: string[], min_clearance: number}]`
   - `link_policies: [{link, roles: string[], min_clearance: number}]`
   - `property_policies: [{object, property, default: "allow"|"deny", allow_roles?: string[], mask?: "partial"|"full"|"none"}]`
   - PUT 为三数组**整体替换**（`PoliciesIn`）；未声明对象 fail-closed（`core/policy.py` 装载期 coverage 校验）；保存即生效，无需重建语义层。
   - views：`{views: [{name, base_object, properties: string[], roles: string[], description?}]}` 整体替换；`base_object` 必须已声明、`properties` 必须是该对象属性子集（含 pk）。
   - **遮蔽实时预览纯前端本地模拟**（不出网），按 property_policies 的 mask/allow_roles 对样本值渲染。

3. **知识包（knowledge）**
   - `relation_assertions: [{from, to, type, source?, valid_until: string|null}]`（必填 from/to/type）
   - `subject_aliases: {主体名: [别名...]}`
   - POST = 追加断言（合并），PUT = 整体替换 assertions（aliases 可选合并）。
   - 过期行（`valid_until` < 今天）前端 opacity .5 + 删除线 + "扫描中自动排除"标签；core `org_interest_links` 已自动排除过期断言。

4. **数据元（data-elements）**
   - GET 整包透传 `{elements: {DE_CODE: {name, type, length?, format?, checksum?, sensitive?, mask?, enum?, enum_space_dim?}}}`；PUT 整包回写（body 即完整 JSON）。
   - 分类树前端按 `type/enum_space_dim/sensitive` 构建。

5. **ETL 管道（etl-pipeline）**
   - `sources[]` 从 bindings.json object_bindings 派生；可编辑字段仅 `clean: string[]`、`on_cast_error: {属性: 状态}`、`null_policy: {属性: 状态}`、`dedup_key: string[]`、`dedup_on_conflict: string`。PUT body `{sources: [...]}`，按 object 匹配回写。
   - 枚举以 loader 为准（`core/ontology_loader.py`）：
     - `on_cast_error` 状态：`"fail"`（硬 CAST 回退）/ `"quarantine"`（TRY_CAST 失败行隔离）/ 缺省（降级 NULL）；仅结构化源（source）绑定可用。
     - `null_policy` 状态：`"allow"`（NULL 保留，缺省）/ `"reject"`（空值行剔除）/ `"quarantine"`（空值行整行隔离）。
   - `composite_props` GET 恒为 `[]`（复合列无声明位置，见决策 3）。

6. **映射校验（validate）**
   - body：`{target_table: string, mapping: {属性: 源列}}`。
   - 响应：`{valid: boolean, conflicts: [{type, ...}], paths: [{key, label}]}`。
   - conflict.type：`one_to_one`（同源列映射两属性，含 source_col/target_a/target_b）、`unknown_prop`（属性不在声明内）、`missing_column`（上传列不在声明源列内）。
   - paths 固定两路：`A_split_source_sql`（上游 source_sql 拆分）、`B_degrade_column`（整列降级为低可信度）。
   - **响应无 force/ignore/continue 字段**；红线四"不得提供忽略并继续"由前端保证，split 类操作 disabled。

7. **质量检查（quality-checks）**
   - POST 触发四扫描（compliance 合规 / freshness 新鲜度 / sensitive 敏感列 / unit 单位），进行中幂等回跳既有任务，未 BUILD 返 400；返回 task_dto（202）。
   - latest 响应：`{available: false}` 或 `{available: true, check_id, created_at, created_by, data_version, summary, checks}`。
   - `summary: {total, passed, warnings, violations}`。
   - `checks[]: {category: compliance|freshness|sensitive|unit, mode: deterministic|heuristic, rule_id, obj, prop, severity: ok|warn|suggest|block, count, message, samples_masked: string[]}`。
   - severity 语义：`block`（确定性违规，红线七不静默）/ `warn`（确定性超期）/ `suggest`（启发式封顶，红线六不阻断）/ `ok`。

8. **隔离区（quarantine / clean-trace）**
   - quarantine：query `reason`（枚举 `cast_error|null_value|dedup|other`）、`page`（≥1）、`page_size`（1–50，**上限 50**）；响应 `{items, total, stats: {四类计数}, page, page_size, empty_message?}`。
   - item 字段：`object, property, rule, src_column, reason, source_table, sample_masked: string[], name_value, quarantined_at`。
   - 零隔离返 `empty_message: "本次装载无数据被丢弃"`（红线五，不留空）。
   - clean-trace：query `object`、`page`、`page_size`（≤50）；按 (object,property) 聚合，item 含 `rules[], rows_before, rows_after, dropped_rows, rate, samples_masked[], source(build|rescan), created_at`。
   - 未 BUILD 案件返空结构不 500；样本只出脱敏值。

9. **缺列降级（governance/missing-columns）**
   - 响应 `{items: [{object, property, count, kinds: string[], samples: string[]}], total_warnings}`；聚合 `source_column_missing` 与 `source_value_cast_failed` 两类诊断，按对象/属性分组。**无分页**（见默认项）。

---

## 四、前端实施方案

### 4.1 新增 API 封装（`frontend/src/api/endpoints/`）

| 文件 | 覆盖端点 |
|---|---|
| `rules.ts` | rules GET/PUT/draft |
| `model.ts` | objects/links GET/PUT + validate |
| `policies.ts` | policies + views GET/PUT |
| `knowledge.ts` | knowledge GET/POST/PUT |
| `dataElements.ts` | data-elements GET/PUT |
| `etl.ts` | etl-pipeline GET/PUT + validate + governance/missing-columns |
| `quality.ts` | quality-checks POST/latest |
| `quarantine.ts` | quarantine + clean-trace GET |

均带 `noteDataVersion(caseId, res.dataVersion)`；写请求带 `idempotencyAction`；PUT body 增可选 `reason`（危险项必填，进审计链，见决策 4）。

### 4.2 新增页面与路由（侧栏 key 已预留，`/c/gov/*`、`/c/model/*`）

**数据治理组**：
- `DataElementView.vue`（`/c/gov/data-elements`）：分类树 + 标准列表 + 详情 + 代码表预览。
- `EtlPipelineView.vue`（`/c/gov/etl`）：流程节点 + 属性级规则表（clean/on_cast_error/null_policy/dedup）+ 部分成功态并排展示成功与隔离。
- `MappingValidationView.vue`（`/c/gov/mapping`）：阻断红横幅 + 冲突双方列名 + A/B 两路出路；跨作用域跳转带 `?return=` 返回路径。
- `QualityView.vue`（`/c/gov/quality`）：指标卡（summary）+ severity 分组规则检查表 + 治理健康度；触发检查走 SSE（复用 TaskProgressCard）。
- `QuarantineView.vue`（`/c/gov/quarantine`）：四类统计条 + 隔离行 DataTable（服务端分页）+ clean-trace 子表 + 零隔离文案。

**研判模型组**：
- `RuleWorkshopView.vue`（`/c/model/rules`）：上区 rule_text 自由编辑 / 下区 function+params 只读结构+可改阈值（危险确认）；函数目录只读 tab；RESCAN 任务提示；draft 守卫态。
- `ModelDesignerView.vue`（`/c/model/designer`）：objects/links 双表编辑 + 值类型 5 种枚举（string/integer/decimal/date/boolean）+ 间类五间枚举 + 端点引用校验 + validate 错误条。
- `KnowledgeView.vue`（`/c/model/knowledge`，**nav 需补 key**）：断言表增改停用 + valid_until 有效期轴 + 主体别名编辑器 + 过期行样式。
- `AccessConfigView.vue`（`/c/model/masking`）：角色×对象/链接权限矩阵（FE-C-031 斜纹拒绝格，格子不留白）+ 字段遮蔽规则表 + 右侧实时遮蔽预览（角色切换）。

### 4.3 新增 domain 纯函数

- `ruleEdit.ts`：三字段 diff、双层门禁判定、RESCAN 影响提示。
- `policyMatrix.ts`：矩阵格子三态（允许/拒绝/未声明=斜纹拒绝）、fail-closed 派生。
- `maskPreview.ts`：按 mask（partial/full/none）+ allow_roles 本地模拟遮蔽（310****1234 口径）。
- `qualityReport.ts`：severity 分组、红/黄/绿语义色阶、deterministic vs heuristic 区分。
- `etlConfig.ts`：on_cast_error/null_policy 枚举校验、dedup 派生、validate 冲突归类。

### 4.4 MSW（`mocks/handlers.ts`）

补九组 handler：配置快照 GET 直出 ontology default 包 JSON 结构；写操作模拟 200 成功 / 400 校验失败（loader 错误文案）/ 403 低权限 / 202 任务；quarantine/quality 造含 block/warn/suggest/ok 与零隔离两种态；validate 造 one_to_one 冲突态。

### 4.5 复用既有件

DataTable（分页/跳页）、EmptyState（三态）、MaskedField、PolicyGate（clearance<2 渲染只读态）、ConfirmDialog 模式、TaskProgressCard/SSE、pagination 纯函数、Stepper。

### 4.6 技术债偿还

MVP-3 跳过的 ETL 配置器 / 数据质量页本轮补齐；接入向导映射步可接 validate（同作用域，均案件快照级）。

---

## 五、已确认的四项决策

| # | 议题 | 定案 |
|---|---|---|
| 1 | 规则工坊下区"主办审批"（后端 PUT 门槛仅 clearance≥2、无审批流） | **按后端现状 + 危险确认**：偏将及以上可写 params/enabled；下区改动走 🔴 ConfirmDialog + 变更理由必填 + RESCAN 影响提示，不伪造审批流；function 等结构字段只读展示 |
| 2 | 知识包"敏感地点白名单"（loader 仅 subject_aliases + relation_assertions，无字段无测试） | **本轮砍掉白名单**：知识包页只做关系断言增改停用 + 主体别名 + valid_until 有效期轴；白名单待后端补 case_knowledge 字段后另排 |
| 3 | ETL 复合列定义（GET 恒返 `composite_props: []`、PUT 不落、validate 两路出路无写端点） | **只做诊断 + 出路指引**：阻断横幅展示冲突双方列名 + A（source_sql 拆分）/B（整列降级）两路文案，跳转带 `?return=`；不提供"忽略继续"、不做实际写；split 类操作 disabled（红线四） |
| 4 | FE-T-012 配置变更"审计链可查"（配置写只进全局 ops_events，不进 `/cases/{cid}/audit`） | **后端追加进案件审计链**：7 个配置写端点落盘后经 `core.AuditChain(backend="sqlite")` 追加哈希链事件；审计链页可按 action 筛到；前端 PUT body 增可选 `reason`（危险项必填） |

**决策 4 后端改动范围**：
- 新增共享助手 `record_config_audit(ctx, case_id, p, action, before, after, reason)`：打开 per-case `state.sqlite`，`AuditChain.readonly/stateful(conn, case_id, backend="sqlite", ontology_version=f"v{current_version}")` 调 `append(operator=p.operator, before, after, source_row_ids=[], ontology_version=..., rule_version=action)`。
- 接线端点（7 处写）：rule_workshop.edit_rule、model_designer.save_objects/save_links、access_config.save_policies、views.save_views、knowledge.save_knowledge/add_knowledge、etl.put_data_elements/put_etl_pipeline。
- 保留既有 `record_ops`（运维事件流，仪表盘 ops 摘要用），审计链为追加不替换。
- 补 `run_tests.py` 用例：配置写后 `GET /cases/{cid}/audit?action=` 可查到事件、链 `verify` 不断、reason 落库。

---

## 六、默认项处理（已确认无异议按此执行）

- `governance/missing-columns` 无分页 → 前端客户端分组渲染（组数 = 对象×属性，天然小）。
- 质检报告内 `checks[]` 无分页 → 前端客户端分页/折叠（单报告文档）；质检只做 latest，不做历史列表。
- 数据元"平台级上下文（切案件不清空）"与案件快照级端点冲突 → 仅 UI 状态（选中树节点/详情）跨案件保留，数据随案件快照。
- 模型设计器 validate 失败 → 直接展示后端错误原文（通常含对象/字段名），不做字段级结构化解析。
- 侧栏"函数目录"无独立端点 → 并入规则工坊只读 tab，不单独建页。
- 知识包在研判模型组补 nav key（`knowledge`）。

---

## 七、实施顺序（4 段）

1. **后端审计接线 + 测试**（决策 4）：`record_config_audit` 助手 + 7 处写端点接线 + 后端测试（审计链可查、链校验不断）。
2. **前端访问层**：8 个 API endpoints + 5 个 domain 纯函数 + MSW 九组 handler。
3. **九页两批落地**：
   - 批 A（研判模型，含两条 P0）：规则工坊 → 权限与遮蔽（FE-T-013）→ 模型设计器 → 知识包；
   - 批 B（数据治理）：数据元 → ETL 管道 → 映射校验（FE-T-016）→ 质量检查（FE-T-018/019，SSE）→ 隔离区（FE-T-017，服务端分页 + 零隔离文案）。
4. **红线测试 + 全量验证**：FE-T-011/012/013/016/017/018/019 共 7 组 spec；`npm run test` + `vue-tsc + vite build`；后端 `python run_tests.py` 全绿；MSW 演示路径走查。

### 红线测试清单

| 编号 | 红线 | 本批落点 |
|---|---|---|
| FE-T-011 | 红线常量不可被配置覆盖 | 12 条 L0 常量注入配置修改必须失败 |
| FE-T-012 | 配置变更写入审计链 | 危险项 ConfirmDialog + 理由必填，审计链可查（决策 4） |
| FE-T-013 | 未声明=拒绝（矩阵斜纹态） | 未声明格子 class=state-denied、斜纹底+锁图标+「拒绝」 |
| FE-T-016 | 1:1 映射阻断 | 冲突横幅 + 冲突双方 + 无"忽略继续" + split disabled |
| FE-T-017 | 丢弃可见 | 60/40 并排可下钻；全干净显"无数据被丢弃" |
| FE-T-018 | 启发式只告警 | 未声明敏感列 → 装载成功 + severity=suggest + 数据保留 |
| FE-T-019 | 确定性检查不静默 | 校验位错误身份证 → block 记录并按 on_cast_error 处置；策略 null 时 NULL 计数可见 |

---

## 八、边界与纪律

- 不写自由 SQL：所有数据走后端端点 / MCP；前端只做编排与渲染。
- 不 Mock 红线交互：权限判定、fail-closed、阻断态、零隔离文案一律直连真实契约（MSW 仅模拟数据，不模拟"永远通过"）。
- 配置写操作全部经后端端点（前端不自造状态机、不本地落业务数据）；UI 偏好（主题/密度/字号）走 `sw.pref.*` localStorage。
- 后端改动遵守既有范式：临时副本过 loader 校验、原子写、record_ops + 审计链双留痕。
