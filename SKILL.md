---
name: sunzi-investigation
description: >
  侦查线索的结构化推演内核。把举报、银行流水、通话记录、招投标档案、轨迹、OSINT 等数据
  展开为可溯源的候选线索，输出「假设—证据—程序」对照、五间交叉等级、
  优先级排序与处置状态看板。
  Use when: 需要盘案件要素、扫描资金/通讯/轨迹异常、识别第三方过桥结构、
  判断多条线索是否达到交叉升格门槛、跟踪线索处置进度、本体画像/数据地图/新表接入草案。
  Do NOT use for: 给案件定性、决定立案、替代法定程序——定性权只属于人（正兵）。
  不要让它读取数据后直接下结论；它输出的是「候选 + 溯源 + 待核实标记」。
---

# 孙武侦查官 · Skill（DuckDB + LadybugDB 单机版）

> 侦查逻辑（庙算→知己知彼→虚实→奇正→用间→全胜）与数据量、与模型**完全解耦**。
> 本技能是**确定性计算内核**（Python + DuckDB，纯离线、无大模型依赖、无需 API Key），
> 跑完出结构化产物；LLM 只负责编排与表达，不碰计算、不写业务 SQL。

## 定位
侦查员的"决策副驾"，只做三件事：
1. 把案件要素展开成可复查的推演（庙算）
2. 把全量数据里的反常标成候选突破口（虚实/用间）
3. 把"假设—证据—程序"对齐，把定性权交回人（奇正/全胜）

## 数据流与技术栈
```
data/*.parquet（L3 冷层） → investigation.duckdb（L2 温层） → output/*.json（产物）
                                                  └→ obj_*/lnk_*（语义层，声明式编译）→ 检测器/图库/MCP
data/ladybug/*.lbug（L4 图库，可选）
```
- **L2 温层核心是语义层**：`ontology/<pack>/*.json`（schema_version=2，**声明是数据、实现是代码**）
  由 `core/ontology_loader.py` 装载校验、`build_ontology()` 编译为 `obj_*`/`lnk_*` 语义表。
  检测器/图库/MCP **一律消费语义表，不直读 Parquet**；换数据源只改 `bindings.json`，不改检测器。
- L3 冷层 Parquet 经 `data_ingest` 适配器接入（CSV/Excel/JSON/SQLite/Parquet → Parquet）。
- L4 LadybugDB 走 CSV 中转（Windows 原生 duckdb 扩展不可用，WSL/Linux 可用，见 INSTALL.md）。

语义层八段：`objects/links`（类型层）、`bindings`（管道层）、`rules`（规则手册）、
`functions`（只读 Function）、`actions`（可写 Action）、`policies`（权限）、`views`（语义视图），
另配 `thresholds/case_knowledge/dimensions/enum_space/llm_policy` 等配置。

## 核心决策顺序（不可颠倒）
庙算 → 知己知彼 → 虚实 → 奇正 → 用间 → 全胜

## 五条不可让渡红线（工具层代码强制，不靠 prompt 自觉）
1. AI 不出定性结论、不置"已立案"——写操作唯一入口 `ActionExecutor`，"已立案"是 human 专属终态
2. 每条推断必须挂 `source_rows` 溯源（内容寻址行 URI `dataset@version#partition/rowid`）
3. 严格区分【数据事实】【推断】【待核实】三栏
4. "知己"栏必须填证据缺口与授权边界，不准只画对象画像
5. 单源情报不算数，至少 2 类"间"独立指向才升格

## 五子技能（skills/，Function 的薄编排层，不写 SQL）
- `skill: miaosuan` 庙算沙盘：假设 ≤5 条，四层覆盖完整性（数据驱动×规则约束×反遗漏×人机协同）
- `skill: zhi_ji_zhi_bi` 双向画像机：知己强制非空
- `skill: xu_shi` 虚实扫描：只标反常，不给定性（经规则手册调只读 Function 消费 `obj_*`/`lnk_*`）
- `skill: qi_zheng` 奇正分工器：奇兵拓线 / 正兵固证 双列
- `skill: yong_jian` 五间交叉器：单源=观察 / 双源=线索 / 三源=可立案依据候选

## 规则手册与 Function/Action（判据单点化）
- **Rule**（`rules.json`）：`rule_text` 自然语言判据（分析师写、LLM 经 `rule_list` 读、随线索审计）
  + 唯一机器挂钩 `function`+`params`；`core/rules.py` 确定性执行，机器不解析自然语言。
- **Function 只读**（`functions.json` + `core/functions.py` 注册）：SQL 强制 SELECT/WITH 白名单 +
  `{{param}}` 模板（string 仅 enum 白名单防注入）；10 个内置函数覆盖季末整数存款/整数转账/
  频次突增/同框/利益关联/时间窗碰撞/两跳过桥/五间升格/举报交叉/通话覆盖。
- **Action 可写**（`actions.json` + `core/action_executor.py`）：角色/必填参数/状态机/副作用四步校验；
  file 副作用创建 `obj_decision` 决策对象 + `lnk_decision_for` 链接。
- **权限**：`AccessContext`（operator 必填、LLM 默认 isolated 拒网）贯穿五出口；
  `policies.json` 未声明一律 fail-closed，敏感列遮蔽（310****1234）。

## 本体画像与数据地图（治理面，只读，REQ-P）
- **数据地图**（`core/data_map.py`，零依赖）：L0 拓扑（核心枢纽/枢纽/隐形枢纽/孤立）+
  L1 物理血缘（对象←源表、归一 JOIN、缺口检测），渲染 Markdown + Mermaid。
- **六层本体画像**（`core/ontology_profile.py` OntologyProfiler）：空值率/值类型分布/混装判定/
  L3 语义指标/五间覆盖/质量分；值类型识别 `core/value_type.py`（账号/手机/身份证/金额/日期/人名/机构）。
- **新表接入草案组装器**（`core/draft_assembler.py`）：外部表画像 → objects/links/bindings
  三件【待核实】草案 + ETL 步骤序列，只写 `output/drafts/`，人工审核 + loader 校验两道闸才生效。
- 画像/地图/草案**只观察不写回**；健康度诊断落 `run_diagnostic`（`core/run_health.py`）。

## LLM 与内核的边界
| 环节 | 谁做 |
|---|---|
| 扫描异常、交叉升格、多跳查图、画像/地图 | **内核**（确定性、可复现、可审计） |
| 读 Parquet、写业务 SQL | **内核**（禁止 LLM 直接做） |
| 组织流程、追问、排序表达 | LLM |
| 从举报信/笔录抽取假设雏形 | **LLM**（唯一介入计算的环节，假设自带证伪条件） |

LLM 默认关闭（`llm_enabled:false`）、`shadow` 影子模式、isolated 拒网；启用需案件包显式声明。
**LLM 负责"猜"，内核负责"验"。**

## 输出格式（六段，固定）
【庙算基线】【双向盘点】【虚实扫描】【奇正分工】【用间交叉】【全胜校验】

产物落在 `output/`：
- `lineage_clues.json` — 线索流（假设链/溯源行/间类/优先级/处置状态/审计链，detail 带 rule_id/rule_text）
- `六段输出.json` — 六段结构
- `profiles/` — 新表接入画像；`drafts/` — 接入草案（待人工审核）
- `entity_mapping.json` / `review_queue.json` — 实体映射 / 确认队列

---

## 快速命令（默认 WSL2，venv `/root/.venvs/inves`）
```bash
python run_all.py --auto-review --no-cli   # 一键全链路（冷层→语义层→检测→五间→处置→导出）
python -m scripts.build_ontology           # 单独构建/重建语义层（--pack/--functions/--actions）
python run_tests.py                        # 70 组测试，必须全绿（--only <组> 单跑）
python -m scripts.mcp_server               # MCP server（stdio，13 工具）
python -m scripts.mcp_client_test          # MCP 端到端自测，改 MCP 后必跑
python -m scripts.demo_profile             # 六层本体画像 → output/profile_report.md
python -m scripts.profile_table --input data/samples/工商信息.xlsx
                                            # 新表接入前画像 → output/profiles + output/drafts
python -m scripts.export_ladybug           # 语义层 → LadybugDB 边表 CSV
```

> ⚠️ LadybugDB 的 `duckdb` 扩展在 Windows 原生不可用（官方 CI 不构建，坏二进制），走 CSV 中转；
> WSL/Linux 手工装配见 `INSTALL.md`。

详细安装与故障排查见 `INSTALL.md`；代理工作守则与已知坑见 `AGENTS.md`。
