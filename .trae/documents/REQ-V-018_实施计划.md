# REQ-V-018 核查手册建议项 · 分步实施计划

> 范围：补齐 [实施方案.md](file:///D:/dev/inves_duckdb/.trae/documents/%E6%A0%B8%E6%9F%A5%E5%B7%A5%E4%BD%9C%E5%8C%BA/%E5%AE%9E%E6%96%BD%E6%96%B9%E6%A1%88.md) §3.8 REQ-V-018 的 6 项缺口，严格按方案既定顺序推进。
> 纪律：声明是数据、实现是代码；建议项不进固证门禁；事实栏永不生成核查项；不新增自由 SQL。
> 本计划仅经审批后执行，审批前不改任何代码。

## 一、仓库调研结论（2026-09-12 取证）

### 已就位的地基（不重复建设）

| 能力 | 证据 |
|---|---|
| 七态状态机含「建议/已忽略」生命周期与采纳改写边 | [core/verify_machine.py](file:///d:/dev/inves_duckdb/core/verify_machine.py) |
| Worker 采纳/忽略/重采纳/审计/agent 拒绝（仅 transition、add_manual 两 op） | [server/app/worker/verify.py](file:///d:/dev/inves_duckdb/server/app/worker/verify.py) |
| StateStore 支持 status/channel/ref_function/external_json/falsification 落库，INSERT OR IGNORE 只补缺 | [state_store.py L517-L557](file:///d:/dev/inves_duckdb/server/app/store/state_store.py#L517-L557)；建议独立计数 [L613-L631](file:///d:/dev/inves_duckdb/server/app/store/state_store.py#L613-L631) |
| 前端七态镜像、VerifyItem 四路由字段、建议卡虚线样式、采纳/改一改/忽略弹窗、渠道徽标 | [domain/verify.ts L148-L164](file:///d:/dev/inves_duckdb/frontend/src/domain/verify.ts#L148-L164)、[VerifyWorkbench.vue](file:///d:/dev/inves_duckdb/frontend/src/components/research/VerifyWorkbench.vue) |

> 即：方案缺口⑤的「建议卡渲染 + 采纳/忽略交互」**已实现**，本计划只补其采纳后的路由按钮与结构化构造器。

### 关键事实（决定实现口径）

1. **R6 规则声明**（[rules.json L80-L93](file:///d:/dev/inves_duckdb/ontology/default/rules.json#L80-L93)）：`subject_column="资金主体"`、`params={round_unit:10000, exclude_org_suffix:"公司"}`、`assumption="H4"`。
2. **检测器 SQL**（functions.json `time_window_collision`）：命中条件仅 `amount % round_unit = 0 AND owner_raw NOT LIKE '%公司%'`，**无金额下界**——故 source_rows 天然含 0 元与 1e12 行。
3. **demoF v8 clue_9446b1bd 行集实测**（12 行，列：项目/资金主体/金额/偏移天数/中标公示日）：
   - 张卫国 8 行（金额均 100000，偏移 −3～+18）；华清越 3 行（含 1 行 **0 元**）；孟繁星 1 行（**1e12**）。
   - `detail.assumption_chain` 为空；经回填后 rule_id=R6，运行时关键词命中 **H1**。
4. **⚠️ AC 数字偏差（待裁决点 D1）**：方案 §3.8 步骤 3/AC1 锁定「过滤后张卫国 **7** 行、{project_count}=7（vs 华清越 2）」。按可声明复现的口径（剔单位 + 金额为正整数倍：剔 0 元行；1e12 无声明依据可剔）实测为**张卫国 8 行、华清越 2 行**。唯一能得到 7 的口径是再剔「公示日前」的 −3 天那 1 行，但这比 R6「公示日**前后** 20 天」判据更严，无声明依据。**推荐：AC 按实测锁 8，文档 7 视为笔误**；若业务确认公示前行不计，需在 playbook/规则显式声明后实现，不硬编码。
5. **pack 指纹**（[ontology_loader.py L47-L56](file:///d:/dev/inves_duckdb/core/ontology_loader.py#L47-L56)）按目录 `*.json` mtime 最大值计算，新增 `verify_playbooks.json` 自动纳入缓存失效，无需改指纹。
6. **两个案件包**：`ontology/default/`（内核默认）与 `cases/demoF/ontology/default/`（演示+回归测试 base_dir=DEMOF_ONTO_BASE），playbook 两处都要放。
7. **读面无 run handle**：`run_diagnostic` 仅由 DIAGNOSE 任务域写入（[worker/diagnose.py](file:///d:/dev/inves_duckdb/server/app/worker/diagnose.py)），assemble_detail 读面无 run 上下文，不应越界写诊断表。
8. **采纳后路由目标端点尚不存在**：「运行内核核查」依赖 P3 REQ-V-017、「转调取台账」依赖 P2 REQ-V-013，本批次均未实现（待裁决点 D2）。

## 二、涉及文件与模块

| 文件 | 改动 |
|---|---|
| `ontology/default/verify_playbooks.json` | **新建**：3 条 R6 建议 playbook（步骤 1） |
| `cases/demoF/ontology/default/verify_playbooks.json` | **新建**：与上同内容（demoF 回归用） |
| [core/ontology_loader.py](file:///d:/dev/inves_duckdb/core/ontology_loader.py) | 新增 `load_verify_playbooks()`；`load_pack()` 末尾调用做装载期硬失败；不进 obj_*/lnk_* 编译（步骤 2） |
| [server/app/verify_provision.py](file:///d:/dev/inves_duckdb/server/app/verify_provision.py) | 新增 `render_suggested()` 确定性渲染（match/槽位/主体计数/过滤）+ skipped 诊断返回（步骤 3） |
| [server/app/clues_view.py](file:///d:/dev/inves_duckdb/server/app/clues_view.py#L199-L207) | assemble_detail 供给段追加 suggested 项 upsert（步骤 3） |
| [frontend/src/components/research/VerifyWorkbench.vue](file:///d:/dev/inves_duckdb/frontend/src/components/research/VerifyWorkbench.vue) | 结构化构造器；采纳后路由占位按钮（步骤 4、5） |
| [frontend/src/views/ClueDetailView.vue](file:///d:/dev/inves_duckdb/frontend/src/views/ClueDetailView.vue) | 向工作台传 sourceRows（构造器实体下拉数据源） |
| `tests/test_verify_suggest.py` | **新建**：AC1～AC7 用例（步骤 6） |
| [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py#L215-L216) | GROUPS 注册 `verifysuggest`（步骤 6） |
| `frontend/tests/verify-workbench.spec.ts` | 扩充构造器拼装/路由按钮用例（步骤 4、5） |

## 三、实施步骤（依赖序）

### 步骤 1：核查手册声明 `verify_playbooks.json`

按方案 §3.8 步骤 1 契约新建，`schema_version=1`，default 与 demoF 两份内容一致，3 条 playbook：

| id | channel | match | 关键声明 |
|---|---|---|---|
| `r6_fund_tw_rerun` | function | `{rule_id:"R6", assumption:"H1"}` | `function:"time_window_collision"`、`fallback_function:"integer_transfer_aggregates"`；text 含 `{subject}`、`{project_count}`；falsification「窗口期资金均为对公工程尾款、无个人账户整数进出」 |
| `r6_call_window` | function | `{rule_id:"R6", assumption:["H1","H4"]}` | `function:"call_frequency_spike"`；text「补查 {subject} 在 R6 中标窗口期（公示日前后 20 天）与中标方联系人的通话频次」（白名单槽位 {subject}；无 project_count 则不计算该槽） |
| `r6_bid_archive` | external | `{rule_id:"R6"}`（无 assumption，不约束假设） | `external:{target:"住建局招标办", material:"中标项目招投标底档及资金审批联签单"}`；text「向住建局招标办调取 {subject} 关联中标项目招投标底档及资金审批联签单」 |

assumption 允许单值字符串或数组（数组=并集匹配）。

### 步骤 2：装载校验 `load_verify_playbooks()`（loader 硬失败）

新增独立函数（仿 `load_dimensions`/`load_enum_space` 的「文件缺失回退」模式，支持 `base_dir` 注入供测试用临时包）：

- 文件缺失 → 返回 `[]`（旧案件包/精简包零破坏）；存在即校验：
  - `schema_version == 1`、`playbooks` 非空数组；
  - 每条：`id` 非空且包内唯一；`channel ∈ {function, external}`；`match.rule_id` 必须在 pack 的 `rules` 中存在（**硬失败**）；
  - `channel=function`：`function` 必填且存在于 pack `functions` 键（**硬失败**）；有 `fallback_function` 时同校；
  - `channel=external`：`external.target`、`external.material` 均为非空字符串；
  - text 模板槽位只允许 `{subject}`、`{project_count}`（正则提取 `{xxx}`，白名单外槽位**硬失败**）；
  - match.assumption 形态校验（string 或非空 string 数组，值形如 `H\d+`）；
- 在 `load_pack()` 装载末段（load_enum_space 附近）调用一次本函数 → build_ontology / RE-SCAN 装载环节即暴露坏手册（满足 AC5），结果不挂 OntologyPack、**不参与语义层编译**；
- 模块级指纹缓存（复用 `_pack_fingerprint`），渲染侧重复调用零成本。

### 步骤 3：确定性渲染 + 读面接线

在 verify_provision.py 新增纯函数（不开库、不回写 artifact，与现有供给纪律一致）：

```
render_suggested(raw_for_view, evidence, *, pack_id, base_dir) -> (items, skipped)
```

- **有效假设并集**：`detail.assumption_chain（空则∅） ∪ evidence 待核实 h 卡解析出的 H\d+`（demoF = {H1}；h 卡文本格式 `待验证假设：H1（…）`，从三栏同源解析，不第二次跑关键词匹配）。
- **playbook 命中**：`match.rule_id == detail.rule_id`（回填后为 R6）且（assumption 省略 或 与有效集有交集）。
- **槽位计算**（仅当模板用到对应槽时才算）：
  - 从规则 spec.params 取 `round_unit`、`exclude_org_suffix`；
  - 过滤 source_rows：主体列（subject_column）值为非空字符串、不含单位后缀；金额可解析为数值且 **> 0** 且 `% round_unit == 0`；窗口判据由行集本身保证（行集即 lnk_time_window ±20 天产物），不再另造窗口阈值；
  - 按主体计数 → 最多者为 `{subject}`，其计数为 `{project_count}`；**并列取名称排序首者**保证确定性；
  - 过滤后无主体 → 该 playbook 跳过、不供给，记入 `skipped=[{playbook_id, reason}]`。
- 输出项：`{kind:"suggested", origin:"suggested", status:"建议", text, channel, ref_function, external, falsification}`；item_key 仍走 `verify_item_key(clue_id,"suggested",text)`（ADR-V-4 稳定键）。
- **clues_view 接线**：[L199-L207](file:///d:/dev/inves_duckdb/server/app/clues_view.py#L199-L207) auto 项 upsert 后，追加一次 suggested upsert（复用 `upsert_verify_items`，已支持建议字段）；幂等只补缺、不覆盖已采纳/已忽略（INSERT OR IGNORE 天然满足）。
- **诊断落地（待裁决点 D3，推荐方案）**：skipped 在读面以 `logging.warning` 留痕（含 case/clue/playbook/reason），**不写 run_diagnostic**（读面无 run handle，不越任务域）；若审批要求落表，改为给 render/assemble_detail 增加可选 `diagnostic_sink` 回调，由有 run 上下文的调用方注入。

### 步骤 4：前端结构化构造器（替换纯文本添加框）

- VerifyWorkbench 新增可选 prop `sourceRows?: Record<string, unknown>[]`，由 ClueDetailView 用 `detail.source_rows` 传入；
- 添加区改为三步引导 + 自由输入兜底：
  1. **实体**下拉：sourceRows 中常见主体列（人/from_raw/资金主体/对方户名等，按列名非空去重；sourceRows 缺省回退为纯手填）；
  2. **维度**下拉：取 ontology-config 维度集（复用 useCaseOntologyConfig，缺省回落内置五维）；
  3. **渠道**单选：库内 function / 外部调取；选外部时出现 target/material 输入；
  4. 拼装可编辑文本（实体+维度+渠道+核查点），用户可「改一改」后提交——提交仍只发 `{text}` 走现有 add 端点（MVP 不结构化，方案明确）；保留「自由输入」入口。
- 后端零改动。

### 步骤 5：采纳后路由按钮（待裁决点 D2）

- 建议项采纳（进入待核查/核查中）后，按其行上路由字段显示：
  - `channel=function`：显示「▶ 运行内核核查」，悬停展示 ref_function（+fallback）；
  - `channel=external`：显示「转调取台账」，旁注预填 `target / material`；
- **推荐分期处理**：因 REQ-V-013/017 端点不存在，按钮渲染为**禁用态 + tooltip「将在后续批次开放（REQ-V-013 台账 / REQ-V-017 复跑）」**；预填数据（external.target/material、ref_function）已在行上就绪并参与断言。审批若认为禁用按钮属假功能，则本步骤降级为仅保留渠道徽标、按钮随 P2/P3 同批上线。

### 步骤 6：测试与注册

**新建 `tests/test_verify_suggest.py`（注册组 `verifysuggest`），对 AC 逐条锁：**

- **AC1**：demoF v8 clue_9446b1bd → 3 条建议（function×2 + external×1）；r6_fund_tw_rerun 文本含「张卫国」；`{project_count}` 按 **D1 裁决值（推荐 8）**锁定；item_id 稳定（同 AC 二次供给 vi_ 前缀不变）。
- **AC2**：采纳 → 待核查（仅 origin 变 suggested→auto 语义、status 变、text 保留）；忽略 → 已忽略；重采纳；`verify_progress` 计数正确；固证门禁（confirm/exclude）在建议/已忽略存在时**不拦截**（pending=0 即放行）。
- **AC3**：建议经状态机，非法迁移 VERIFY_REJECTED；Worker 写透传，复用既有 verify 错误码测试模式。
- **AC4**：agent 会话对 suggested 项采纳/忽略 → AGENT_FORBIDDEN；human 可采纳/忽略。
- **AC5**：临时案件包注入坏 playbook（未知 function / 白名单外槽位 / 未知 rule_id / id 重复 / schema 版本错）→ `load_pack`/`load_verify_playbooks` 硬失败（五类各一例）；文件缺失 → [] 且旧包正常装载。
- **AC6**：采纳后 function 项出现「运行内核核查」、external 项出现「转调取台账」且 target/material 预填可见（前端 spec 断言；禁用态文案按 D2）。
- **AC7**：事实栏（fact）行永不生成 suggested 项；渲染只消费 inference/rule 关联信息与 source_rows。
- 补充：非 R6 线索（rule_id 不匹配/假设不交集）→ 零建议；过滤后无主体 → skipped 含原因且不供给；同 clue 二次详情建议项幂等（added=0）。

**前端**：`verify-workbench.spec.ts` 增构造器用例（实体/维度/渠道选择→文本拼装→改一改→提交只发 text；sourceRows 缺省回落手填）、路由按钮用例（按 channel 显隐、预填文案、禁用态）。

## 四、依赖与考虑

- `call_frequency_spike`、`time_window_collision`、`integer_transfer_aggregates` 三个函数均已存在于 [functions.json](file:///d:/dev/inves_duckdb/ontology/default/functions.json)，步骤 1 引用可直接通过校验。
- 渲染所需规则 params/subject_column 经 `load_pack(pack_id, base_dir)` 取 RuleSpec（[core/ontology.py L212-L237](file:///d:/dev/inves_duckdb/core/ontology.py#L212-L237)），有指纹缓存，与 backfill_rule_fields 同源同次加载。
- 两份 playbook（内核包 + demoF 包）需手工保持一致；测试加一条「两包 playbook id 集合一致」的轻断言防漂移。
- `load_pack` 新增校验对所有包生效：文件缺失回退 [] 保证 reqd_case 等旧包零影响；坏文件会让 build 硬失败（这正是 AC5 意图）。
- 建议项 upsert 复用现有 INSERT OR IGNORE 通道，无 schema 变更、无新 Worker op、无新 API。
- 前端构造器只产出 text，不向后端传结构化实体——与方案「MVP 不做结构化存储」一致，后续 P2 台账再演进。

## 五、验证（WSL venv，触发权归用户）

```bash
# 1) 新组单跑
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only verifysuggest"
# 2) 连带既有核查回归
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only verifyitem && /root/.venvs/inves/bin/python run_tests.py --only verifyapi"
# 3) 全量 138 组（验收口径）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
# 4) 两包装载不硬失败（正常 playbook）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.build_ontology --pack default"
# 5) 前端
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb/frontend && npx vitest run tests/verify-workbench.spec.ts && npx vitest run"
```

手工抽检：demoF v8 clue_9446b1bd 详情 `verify.items` 出现 3 条 origin=suggested/status=建议；刷新后 item_id 不变；采纳后进度 pending 增 1 且固证门禁随之收紧。

## 六、风险与处置

| 风险 | 处置 |
|---|---|
| **D1：AC 数字 7 与实测 8 不符** | 推荐按 8 锁定（口径=剔单位+金额为正整数倍，可复现、不违背 R6）；1e12 行主体仅 1 行，不影响主体选择，不为其引入无声明依据的金额上限。审批确认后实施 |
| **D2：路由按钮目标端点（REQ-V-013/017）不存在** | 推荐禁用占位 + tooltip + 预填数据断言；或降级为仅徽标，按钮随 P2/P3 上线 |
| **D3：读面写不了 run_diagnostic** | 推荐 logging.warning + skipped 返回值；如需落表加可选 sink，由任务域注入 |
| 坏 playbook 让 build_ontology 硬失败，影响面广 | 仅「文件存在且内容坏」才失败；缺失回退 []；五类坏例各有测试锁定错误信息 |
| 建议渲染增加读面开销 | load_pack 有指纹缓存；渲染为 O(行数×playbook) 的纯内存操作，demoF 12 行 × 3 条可忽略 |
| 构造器实体下拉列名因数据源而异 | 按常见主体列名候选 + 非空去重，取不到列即回落自由手填，不阻断 |
| 前端 prop/模板变更影响既有 verify-workbench 用例 | 新 prop 全部可选；旧用例不传 sourceRows 走手填回落，逐一跑 vitest 校准 |

## 七、需求—缺口—AC 映射

| 方案缺口 | 本计划步骤 | 主 AC |
|---|---|---|
| ① verify_playbooks.json | 步骤 1 | AC1/AC5/AC7 |
| ② loader 装载校验 | 步骤 2 | AC5 |
| ③ provision 确定性渲染 | 步骤 3 | AC1/AC7 |
| ④ 结构化构造器 | 步骤 4 | 方案 §3.8 步骤 4（人工添加结构化） |
| ⑤ 前端建议卡 + 采纳后路由 | 已实现部分不重做；步骤 5 补路由 | AC6 |
| ⑥ test_verify_suggest + verifysuggest 组 | 步骤 6 | AC1～AC7 全覆盖 |
