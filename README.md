# 孙武侦查官 · 确定性侦查推演内核（DuckDB + LadybugDB 单机版）

把银行流水 / 通话 / 招投标 / 轨迹 / 举报 / OSINT 跑成**可溯源的候选线索、五间交叉等级与处置看板**。
纯离线、无大模型依赖、无需 API Key；Python + DuckDB + （可选）LadybugDB。

> 定位：跑完出**结构化产物**，不是聊天助手。工具层强制拦截定性结论，"已立案"是 human 专属终态。

---

## 数据流与语义层

```
data/*.parquet（L3 冷层） → investigation.duckdb（L2 温层） → output/*.json（产物）
                                                    └→ obj_*/lnk_*（语义层，声明式编译）→ 检测器/图库/MCP
data/ladybug/*.lbug（L4 图库，可选）
```

语义层采用 Palantir Ontology 裁剪版（**schema_version=2，声明是数据、实现是代码**）。
`ontology/<pack>/*.json` 由 `core/ontology_loader.py` 装载校验（未知名 / 版本不符硬失败），
`build_ontology()` 编译为 `obj_*` / `lnk_*` 语义表。**类型层与管道层分离**：

| 文件 | 层 | 回答 |
|---|---|---|
| `objects.json` / `links.json` | 类型层 | 对象/链接**是什么**：pk、kind(entity/event)、name_property、带类型 properties |
| `bindings.json` | 管道层 | **怎么来**：object_bindings（source/clean）+ link_bindings（build_sql） |
| `rules.json` | 规则手册 | 自然语言判据 `rule_text`（给人/LLM 读）+ 唯一机器挂钩 `function`+`params` |
| `functions.json` + `core/functions.py` | Function（只读） | 只读计算；SQL 强制 SELECT/WITH 白名单 + `{{param}}` 模板 |
| `actions.json` + `core/action_executor.py` | Action（可写） | 唯一写路径；角色/必填参数/状态机/副作用四步校验 |
| `policies.json` + `core/policy.py` | 权限 | 对象/链接级策略（未声明 fail-closed）+ 敏感列遮蔽 |
| `views.json` / `thresholds.json` / `case_knowledge.json` | 配置 | 语义视图 / 参数治理 / 案件主体别名与关系断言 |

换数据源 / 新案件只改 `bindings.json`，不改检测器代码；新检测规则加 `rules.json`，
检测器只是 Function 的薄编排层，**不写业务 SQL**。

---

## 快速开始

默认使用 WSL2（Ubuntu-24.04）环境，venv 在 `/root/.venvs/inves`
（ladybug/duckdb/pandas/pyarrow/pypinyin/openpyxl/jsonschema）。

```bash
# 全链路（人工确认）：冷层 → 语义层 → 检测 → 五间 → 处置看板 → 导出
python run_all.py --auto-review --no-cli

# 单独构建/重建语义层（obj_*/lnk_*）
python -m scripts.build_ontology
python -m scripts.build_ontology --pack <包名>      # 切换案件包
python -m scripts.build_ontology --functions        # 查看只读 Function 目录
python -m scripts.build_ontology --actions          # 查看 Action 注册表

# 测试：70 组必须全绿（--fast 跳 e2e、--only <组> 单跑）
python run_tests.py
python run_tests.py --only profiler

# MCP server（stdio）+ 端到端自测
python -m scripts.mcp_server
python -m scripts.mcp_client_test

# 本体画像 / 数据地图（治理面，只读）
python -m scripts.demo_profile                       # 已物化语义层六层画像 → output/profile_report.md
python -m scripts.profile_table --input data/samples/工商信息.xlsx
                                                      # 新表接入前画像 → output/profiles + output/drafts
```

> Windows 原生不可用 LadybugDB duckdb 扩展（官方 CI 不构建 Windows 版），走 CSV 中转；
> WSL/Linux 可用，手工装配见 [INSTALL.md](INSTALL.md)。

---

## 目录结构

```
inves_duckdb/
├── run_all.py                 # 全链路入口（冷层→语义层→检测→五间→处置→导出）
├── run_tests.py               # 70 组测试注册与执行
├── data_ingest.py             # L3 冷层接入适配器（CSV/Excel/SQLite/JSON → Parquet）
├── entity_resolution.py       # 人名实体对齐（core.entity 反向加载，避免循环引用）
├── core/
│   ├── store.py               # L1/L2/L3 统一 Store（DuckDB 连接）
│   ├── ontology.py            # 类型定义 + build_ontology 编译（obj_*/lnk_* 物化）
│   ├── ontology_loader.py     # 八段 JSON 装载 + 强校验（未知名/版本不符硬失败）
│   ├── gateway.py             # OntologyReadGateway 语义层唯一读入口（只读画像方法）
│   ├── functions.py           # 只读 Function 实现注册表（FUNCTION_IMPLS）
│   ├── rules.py               # 规则手册离线执行（run_rules，零业务 SQL）
│   ├── action_executor.py     # 写操作唯一入口（角色/参数/状态机/副作用）
│   ├── access.py / policy.py  # AccessContext 权限上下文 + 对象/链接/属性级策略
│   ├── data_map.py            # 数据地图 L0 拓扑 + L1 血缘（零依赖静态解析）
│   ├── value_type.py          # 值类型识别纯函数（账号/手机/身份证/金额/日期/人名/机构）
│   ├── ontology_profile.py    # connectable_props + EntityLinkExplorer + OntologyProfiler
│   │                          #   六层画像 + TableProfile 新表画像契约
│   ├── draft_assembler.py     # DraftAssembler 草案组装器 + recommend_steps 步骤推荐
│   ├── threshold.py           # thresholds.json 参数装载（profiler/rules 等段）
│   ├── run_health.py          # 统一失败留痕 + 健康度（run_diagnostic）
│   ├── entity.py              # 组织层级对齐（子公司/分公司/项目部归并法人主体）
│   ├── graph.py               # LadybugDB 图库双轨取数（_flow_source）
│   ├── hypotheses.py          # 庙算假设引擎（模式库/枚举/五维覆盖度）
│   └── llm/                   # LLM 治理基座（默认关闭 isolated 拒网，shadow 影子模式）
├── skills/                    # 检测编排（Function 的薄编排层，不写 SQL）
│   ├── xu_shi.py              # 虚实扫描
│   ├── qi_zheng.py            # 奇正分工
│   ├── yong_jian.py           # 五间交叉
│   ├── miaosuan.py            # 庙算沙盘
│   └── zhi_ji_zhi_bi.py       # 双向盘点
├── ontology/default/          # 默认案件包（八段声明 + 知识/参数/策略）
├── schemas/                   # 各段 JSON Schema（validate_ontology --strict）
├── scripts/                   # build_ontology / init_duckdb / incremental /
│                              #   export_ladybug / mcp_* / profile_table / demo_profile …
├── data/                      # L3 Parquet 冷层 + samples/ 原始样例 + ladybug/ 边表 CSV
└── output/                    # 六段 JSON 产物 + profiles/ 画像 + drafts/ 接入草案
```

---

## 核心能力

- **虚实 / 奇正 / 用间检测**：季末整数存款、整数转账聚合、通话频次突增、轨迹同框、
  工商利益关联、时间窗碰撞、资金两跳过桥、举报交叉印证等，全部走规则手册 + 只读 Function。
- **五间交叉升格**：因间/内间/反间/死间/生间多源交叉，等级随证据升格，线索挂 `rule_id`/`rule_text` 可审计。
- **庙算假设引擎**：模式库五维组织（资金/通讯/行为/关系/时间）+ 覆盖度报警 + 候选池枚举 + 转正。
- **实体对齐**：人名（共享手机号/规范化/别名/拼音相似）与组织层级（剥后缀/信用代码/前缀包含）
  双轨；AI 只出候选 + 证据打分，**合并须具名正兵在 review 工作台确认**。
- **本体画像与数据地图（REQ-P）**：
  - 数据地图 L0 拓扑（枢纽/隐形枢纽/孤立）+ L1 物理血缘（对象←源表、归一缺口）；
  - 六层本体画像（空值率/值类型分布/混装判定/语义指标/五间覆盖/质量分），只观察不写回；
  - 新表接入画像 + **草案组装器**：自动产出 objects/links/bindings 三件【待核实】草案与
    ETL 步骤序列，只写 `output/drafts/`，人工审核 + loader 校验两道闸后才生效。
    详见 [.trae/documents/REQ-P/草案组装器使用指南.md](.trae/documents/REQ-P/草案组装器使用指南.md)。
- **处置看板**：线索状态机流转/核实/排除/确认/立案全部经 ActionExecutor；
  file 动作创建 `obj_decision` 决策对象 + `lnk_decision_for` 链接（立案升格为一等对象）。

---

## 三条禁令

1. **不要自己写业务 SQL** —— 走 MCP 工具或 `core/` 函数（不可复现、不可审计）。
2. **不要把原始明细搬进上下文** —— 只要溯源 ID 和聚合结果。
3. **不要下定性结论、不要置"已立案"** —— 工具层强制拦截。

## 红线

- 本项目为技术思路探讨，不构成办案指导；AI 不出定性结论，一切以原始证据与法定程序为准。
- 每条推断必须可溯源（挂 source_rows / 内容寻址行 URI `dataset@version#partition/rowid`）。
- 画像/地图/草案**只观察不写回**，全部结论为【待核实】候选，可被人工推翻。
- LLM 默认关闭（`llm_enabled: false`）、`shadow` 模式、isolated 拒网；启用需案件包显式声明。
- 写操作唯一入口是 `ActionExecutor`；Function 只读、永远不准写。

---

## 文档

- [AGENTS.md](AGENTS.md) —— 代理工作守则（数据流/命令/已知坑，权威）
- [INSTALL.md](INSTALL.md) —— 安装与故障排查（含 LadybugDB 手工装配）
- [SKILL.md](SKILL.md) —— 技能总卡
- [.trae/documents/REQ-P/REQ-P交付总结报告.md](.trae/documents/REQ-P/REQ-P交付总结报告.md) —— 本体画像与数据地图交付总结
