# 自然语言规则手册（Rulebook）实施方案

> 状态：**待确认**（方案文档，审完再动手）
> 日期：2026-09-03
> 前置：v2 五段语义层已入库（分支 ontology_v2，commit 079443b）

---

## 〇、需求澄清

要规则化的"规则"指**自然语言规则**——像 SKILL.md 里"单源不算数：至少 2 类间独立指向才升格"那种 prose 判据：分析师可读写、LLM 编排时读取解析、灵活定义；而不是 JSON 谓词 DSL 或让 LLM 写 SQL。

起因（clue_37d06399 对账，已核实）：
1. 规则名"季度末整数现金存入"暗示三条判据（季末窗口 / 整万元 / 现金存入），实现里只有一条（整万元按季度分组）——**规则文本不存在，判据散在 SQL 字符串里，名实不符无人校验**；
2. 630 万对公转账与现金存入混入同一聚合——交易类型分流判据同样只存在于命名里；
3. 同一判据（整数资金/排除公司）在 lnk_time_window 的 build_sql 里又硬编码一遍——没有单一事实来源。

## 一、Palantir 对照

| 本方案概念 | Palantir 对应 | 说明 |
|---|---|---|
| 自然语言规则手册（rules.json，rule_text 为主体） | **AIP Logic / AIP Agents 的规则意图** + AML 规则手册（分析师 authored 的检测规则文本与阈值） | 规则用业务语言写"什么算反常、为什么、归哪个间"，LLM 读取后编排 |
| 规则绑定 function + params（最小机器挂钩） | AIP 中 LLM 调用的确定性 **Function/Tool** | 计算永远在确定性内核，LLM 不碰计算 |
| run_all 无 LLM 遍历规则出线索 | Foundry pipeline/Transforms 离线跑规则 | 规则文本不参与执行，执行靠 function 绑定——确定性不破 |
| MCP `rule_list` 暴露规则文本给编排层 | AIP Agent 的 system/tool context | LLM 先读规则再调函数，按规则文本解释结果 |
| 线索携带 rule_id + rule_text 原文 | provenance / 规则命中审计 | "依据哪条规则、规则原文是什么"可回放 |

**分工红线**：自然语言负责"判据意图与解释"（人可读、LLM 可读）；function+params 负责"怎么算"（确定性、可复现）。LLM 不改规则文本、不自创判据、不写 SQL、不造数；每条线索仍挂 source_rows、needs_human_review=true。

## 二、目标结构：案件包第六段 rules.json

自然语言为主体，机器挂钩最小化：

```json
{
  "schema_version": 2,
  "rules": [
    {
      "id": "R1",
      "stage": "xu_shi",
      "title": "季度末整数现金存入",
      "dimension": "资金",
      "jian_types": ["生间"],
      "assumption": "H1",
      "rule_text": "在每季度末（3/31、6/30、9/30、12/31）前后 15 天内，出现金额为整万元（1 万元整数倍）且对方摘要为“现金存入”的交易。正常工资与经营性收入多为非整数金额、按固定周期发放；季末时点的整数现金存入与走账、平账、利益输送的常见节奏吻合，列为候选反常。对方为单位/账户名的整数对公转账适用 R2，不归本规则。",
      "basis_text": "与工资性非整数收支规律不符；季末时点整数现金存入",
      "function": "quarter_end_integer_deposits",
      "params": {"round_unit": 10000, "quarter_end_window_days": 15, "cash_summary_tokens": "现金存入"},
      "hit_when": "rows_nonempty"
    }
  ]
}
```

字段说明：
- **rule_text（核心，必填，≥30 字）**：自然语言判据——什么模式、为什么反常、边界与排除项。它既是审计依据（线索产物带原文），也是 LLM 编排时的解释上下文；
- **function + params（执行挂钩）**：规则确定性落地的唯一出口——规则跑的是哪个只读 Function、参数取什么值。params 必须是 function 已声明参数的子集（loader 硬校验）；
- **stage**：规则在决策顺序中的位置（本期实现 `xu_shi` 虚实扫描；qi_zheng/yong_jian 后续可挂）；
- **dimension/jian_types/assumption**：五维（资金/通讯/行为/关系/时间）、五间、庙算假设挂钩，枚举校验；
- **hit_when**：命中判定，`rows_nonempty`（SQL 函数 rows 非空）或 `result.hit`（py 报告函数的 hit 布尔，如 call_frequency_spike）；
- 本期规则集（5 条，对应现 xu_shi 五个 finding）：R1 季度末整数现金存入 / R2 整数转账过桥聚合 / R3 通话频次突增 / R4 轨迹同框 / R5 工商利益关联；另把 time_window 判据写成 R6（stage=qi_zheng，绑定 time_window_collision）。

## 三、执行双轨（确定性不被自然语言稀释）

**轨道 A：离线确定性（run_all，无 LLM）**
`core/rules.py` RuleExecutor 遍历 rules.json：按 function+params 调 FunctionExecutor → hit_when 判定 → 非空即产出 finding（候选虚处=title、依据=basis_text、source_rows=函数 rows、rule_id/rule_text 随线索落产物）。xu_shi.py 从"手写 5 段调用"改为"遍历规则包"，换规则只改 JSON。

**轨道 B：LLM 编排（MCP/SKILL 交互）**
新增只读 MCP 工具 `rule_list`：返回规则手册（rule_text/params/function）。编排约定（写入 SKILL.md）：
1. 先 `rule_list` 读规则原文；
2. 按规则文本调 `function_invoke`，参数以规则 params 为基准；可在规则文本允许的语义范围内调参对比（如窗口 10/15/20 天），但**不得自创规则外判据**；
3. 按 rule_text 解释结果、表述线索；线索必带 rule_id；
4. 计算、溯源、状态机红线全部不变。

**确定性边界（诚实声明）**：rule_text 不被机器执行，机器只执行 function+params。自然语言的风险是"LLM 解释偏差"——缓解：离线轨道以 params 为准绳可复现；LLM 解释只是候选表述，线索一律 needs_human_review；规则文本随 git 版本化，改规则=改数据可审计。

## 四、底座：SQL Function 参数注入（规则 params 要能进 SQL）

现状：functions.json 声明了 parameters，py 函数消费正常；**SQL 函数直接执行 spec.sql，参数不注入**（merged 算了没用）。

- functions.json 的 SQL 支持 `{{param_name}}` 占位，FunctionExecutor 渲染后执行；
- 安全渲染（不依赖 DuckDB 绑定行为，可审计）：占位符与 parameters **装载期双向核对**（SQL 里的占位必须声明、声明的参数必须被使用）；类型化字面量渲染——integer（`^-?\d+$`）、decimal、date（ISO 正则）、boolean；string 类型**仅允许 enum 白名单取值**（如 cash_summary_tokens ∈ {"现金存入"}），单引号转义，拒绝自由文本；标识符（表/列）永不参数化；渲染后照旧过 `_assert_readonly`（SELECT/WITH 白名单 + 写关键词拦截）；
- 约定 `window_days=0` 关闭窗口谓词（可复现旧口径做回归对照）。

R1 函数 SQL（参数化后）：

```sql
SELECT date_trunc('quarter', CAST(date AS DATE)) AS q, COUNT(*) AS cnt,
       SUM(CAST(amount AS DOUBLE)) AS amt
FROM obj_transaction
WHERE CAST(amount AS BIGINT) % {{round_unit}} = 0
  AND to_raw = '{{cash_summary_tokens}}'
  AND ABS(date_diff('day', CAST(date AS DATE),
        date_trunc('quarter', CAST(date AS DATE)) + INTERVAL 3 MONTH - INTERVAL 1 DAY)) <= {{quarter_end_window_days}}
GROUP BY q ORDER BY q
```

演示数据口径对照（7 笔现金存入距最近季末边界天数）：window=10 命中 2 笔；**window=15（建议默认）命中 3 笔**（2019-06-25、2020-03-30、2022-12-18，共 30 万）；window=20 命中 4 笔。630 万转账归 R2/R6，不再混入。**默认值请拍板。**

## 五、链接与规则的职责切分（判据上移）

| 层 | 内容 |
|---|---|
| lnk_time_window（链接=关系） | 保留 ±20 天时间邻接（"时间窗"关系的定义本身）；build_sql **移除** `amount % 10000 = 0` 和 `NOT LIKE '%公司%'`（检测判据） |
| R6 规则 + time_window_collision（function） | 承接两个判据，参数 `round_unit=10000`、`exclude_org_suffix="公司"`；过滤后输出仍 7 行（Q1 产物不变） |

影响面（已清点 lnk_time_window 消费方）：jian_cross_level 只看非空（无影响）；export_ladybug 导出边变多（图更完整，过桥走 lnk_transfers 不受影响）；test_ontology.py:242/586 两处链接内容断言改为"链接=邻接边、规则函数输出=过滤后行"；init_duckdb 的 Q1_time_window parquet 是 L2 旧血缘产物，不动。

## 六、改动清单

| 文件 | 改动 |
|---|---|
| ontology/default/rules.json | **新建**：6 条自然语言规则（R1–R6，rule_text + function/params 挂钩） |
| core/ontology.py | 新增 RuleSpec dataclass；OntologyPack 增 rules |
| core/ontology_loader.py | `_load_rules()`：id 唯一、stage/dimension/jian_types 枚举、rule_text 非空（≥30字）、function 存在、params 键 ⊎ 函数声明参数、hit_when 合法；functions 装载增占位符/参数类型/enum 校验 |
| core/rules.py | **新建**：RuleExecutor（遍历规则→调函数→hit 判定→findings，带 rule_id/rule_text） |
| core/functions.py | SQL 模板 `{{param}}` 安全渲染 + invoke SQL 分支注入参数 |
| skills/xu_shi.py | 5 段手写调用改为 RuleExecutor 驱动（stage=xu_shi） |
| ontology/default/functions.json | quarter_end_integer_deposits / integer_transfer_aggregates / time_window_collision 参数化并声明 parameters |
| ontology/default/bindings.json | lnk_time_window build_sql 移除两个检测谓词 |
| scripts/mcp_server.py | 新增只读工具 `rule_list` |
| SKILL.md / AGENTS.md | SKILL 增"规则手册"编排约定（轨道 B 四步）；AGENTS 语义层改六段描述 |
| tests/test_ontology.py | 规则装载校验（未知 function/参数越界/空文本/枚举非法硬失败）、SQL 模板渲染（默认/传参/0 关闭/非 enum 字符串拒绝）、规则驱动 xu_shi 产出与 rule_id 溯源；time_window 职责断言调整 |
| scripts/mcp_client_test | +2 项：rule_list 目录、function_invoke 传参复现旧口径（window=0 → 9 笔） |

**行为不变量**：线索总数与处置链路、状态机/审计链、代理键、溯源串格式、双轨过桥、五间等级、MCP 既有 40 项。
**预期变化（口径修正）**：R1 默认命中 9 笔→3 笔（window=15）；lnk_time_window 物化行数变多、R6 输出仍 7 行；线索产物增 rule_id/rule_text 字段。

## 七、验收

1. `run_tests.py` 8 组全绿；`mcp_client_test` 42/42；
2. run_all 重跑：R1 线索 source_rows 与 function 重算逐行一致、携带规则原文；R6 输出 7 行不变；
3. `function_invoke(quarter_end_integer_deposits, {quarter_end_window_days: 0})` 复现旧口径 9 笔；
4. loader 硬失败：规则引用未注册 function、params 含未声明参数、rule_text 为空、dimension/jian 枚举非法、SQL 占位未声明、string 参数非 enum 值。

## 八、回退与不做

- 改动集中在新增 rules.json/rules.py + functions/loader/2 个 JSON，git revert 单提交可回退；
- 不做：派生事实属性物化（is_cash_deposit 等，留 backlog）；谓词 DSL（原 P3）——**自然语言规则已承担灵活性，DSL 不再需要**；规则热加载/生效日期版本管理（等规则数增长）。
