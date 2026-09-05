---
name: rule-manager
version: 1.1.0
description: >
  规则手册管理：新增/修改/停用侦查规则（ontology/<pack>/rules.json）、调阈值、
  命中核对、排查 loader 硬失败。Invoke when：用户要求加检测规则、改规则阈值或判据表述、
  排查规则为何命中/不命中、rules.json/functions.json 参数报错。
  勿用于跑侦查推演（用 sunzi-investigation）或案件定性。
---

# 规则手册管理 Rule-Manager Skill

> 规则手册（rules.json，语义层第六段）是检测判据的**唯一事实来源**：
> `rule_text` 自然语言表意图（分析师写、LLM 经 `rule_list` 读取解释、随线索落产物审计），
> `function`+`params` 是唯一机器挂钩（确定性内核执行，自然语言不被机器解析）。
> 本技能管规则的生命周期；LLM 不解析 rule_text、不写 SQL、不自创规则外判据。

## 一、定位与边界

- **管什么**：rules.json 增删改、functions.json 参数化/新函数、规则命中核对、装载硬失败排查
- **不管**：全链路推演（用 sunzi-investigation）、线索处置/立案（ActionExecutor 唯一写路径）、案件定性
- **核心文件**：

| 文件 | 角色 | 本技能何时动 |
|---|---|---|
| `ontology/<pack>/rules.json` | 规则声明（rule_text + function/params） | **主操作对象** |
| `ontology/<pack>/functions.json` | 只读 Function 声明（SQL 模板 / py 挂钩） | 新判据现有函数覆盖不了时 |
| `core/functions.py` | py Function 注册（`FUNCTION_IMPLS`）+ `{{param}}` 模板渲染 | 新增 py 函数时 |
| `core/rules.py` | 确定性执行器（`catalog()` / `run_rules()`） | 一般不改 |
| `core/ontology_loader.py` | 装载硬校验（`_load_rules`/`_validate_function_params`） | 只读参考，不改 |

## 二、规则字段表（rules.json 每条 rule）

| 字段 | 必填 | 取值 | 说明 |
|---|---|---|---|
| `id` | ✅ | 唯一，惯例 `R<n>` | 线索 detail.rule_id 溯源用 |
| `stage` | ✅ | `xu_shi` \| `qi_zheng` \| `yong_jian` | 决定何时被跑：虚实扫描/奇正/用间 |
| `title` | ✅ | 短标题 | 成为线索标题 |
| `rule_text` | ✅ | **≥30 字**自然语言 | 事实型判据：时间/主体/动作/阈值 + 正常情形对照 + 边界（什么不归本规则） |
| `function` | ✅ | functions.json 已声明函数名 | 唯一机器挂钩 |
| `params` | ⬜ | 键必须是该函数 `parameters` 子集 | 阈值/枚举值；缺省用函数 default |
| `hit_when` | ✅ | `rows_nonempty` \| `result_hit` | SQL 函数 rows 非空即命中；py 报告看 `result.hit` |
| `dimension` | ⬜ | 资金 \| 通讯 \| 行为 \| 关系 \| 时间 | 庙算五维覆盖统计 |
| `jian_types` | ⬜ | 因间 \| 内间 \| 反间 \| 死间 \| 生间 | 五间归类（可多个） |
| `assumption` | ⬜ | `H1`..`Hn` 或 `""` | 挂庙算假设链；空串=自动发现 |
| `basis_text` | ⬜ | 一句话依据 | 线索"依据"栏 |

## 三、红线（写规则必守，loader 强制一部分，其余靠人守）

1. **rule_text 只写事实型判据**（什么时间、谁、什么动作、多少金额），禁止"可疑/涉嫌/违法"等定性词；产出线索一律 needs_human_review，定性权在人
2. **文本与参数必须同步改**：rule_text 里的阈值表述（"前后 15 天""1 万元整数倍"）必须与 `params` 数值一致——loader **不校验**文本与参数的语义一致，名实不符靠作者双人核对（教训：R1 曾长期只有整数判据、无季末日期过滤）
3. **机器只执行 function+params**：改判据实质是改 params/function，不是改文本；文本改了 params 没改 = 名实不符
4. **不写自由 SQL**：新判据优先复用现有 10 个 function + 新 params；确需新算法才加 function——SQL 仅 SELECT/WITH 白名单、只用 `{{param}}` 占位、**string 参数必须声明 enum 白名单**、SQL 函数每个参数必有 default
5. **链接 build_sql 只表达关系**（如 lnk_time_window 的 ±20 天邻接），检测判据一律在 rules/function，不回流到链接层
6. **停用规则 = 从 rules 数组移除该条目**（JSON 无注释，禁止加注释/伪字段）；git 历史可回溯
7. **不改检测器代码**：xu_shi 等已规则驱动，自动遍历对应 stage 的规则；新规则加 JSON 即生效
8. 规则不物化：改 rules.json 不影响 obj_*/lnk_* 表结构，无需数据迁移

## 四、标准操作流程

### 任务 A：调阈值（最常见，如季末窗口 15→7 天）
1. MCP `rule_list`（或读 rules.json）定位规则 id 与其 function/params
2. **同步改两处**：`params` 的值 + rule_text 里的数字表述
3. 验证（第六节）；建议用 `function_invoke` 新旧参数各跑一次对比命中数（窗口类参数传 `0` = 关闭该谓词，可区分"谓词太严"还是"数据没有"）

### 任务 B：新增规则（复用现有 function）
1. MCP `function_list` 确认函数存在、参数够用、inputs 指向的语义表有数据
2. rules.json 的 `rules` 数组追加条目（id 取下一个 `R<n>`，字段表逐项填齐）
3. rule_text 三段式：**判据事实**（含阈值）→ **正常情形对照**（为什么反常）→ **边界声明**（什么不归本规则，避免与相邻规则重复计数）
4. stage 选 `xu_shi`（自动进虚实扫描）/`qi_zheng`（奇兵拓线）/`yong_jian`（用间交叉）
5. 验证（第六节）；新规则命中后线索 detail 自动带 rule_id/rule_text，assumption/jian_types 以规则声明为准

### 任务 C：新增规则（需要新 function）
1. 先确认现有 10 个 function 真的覆盖不了（function_list 看 inputs/description）
2. **SQL 函数**：functions.json 加条目——`impl:"sql"`、`inputs` 只列 obj_*/lnk_* 语义表（不准直读 Parquet/原始视图）、sql 里变量一律 `{{param}}` 占位、parameters 每个带 default、string 类型必带 enum
3. **py 函数**：functions.json 加条目（`impl:"py"`, `impl_ref:<名>`）+ 在 `core/functions.py` 用 `@register_function` 注册同名实现（只读，不准写库）
4. 回到任务 B 加规则挂钩；跑 mcp_client_test（函数目录计数变化）

### 任务 D：命中核对 / 排查"为什么没命中"
1. `function_invoke(name, params)` 单独复算（只读，立即看 rows/result）
2. `rows_nonempty`：看 rows 是否为空；`result_hit`：看 `result.hit` 与 `result.basis`（降级说明在 basis 里）
3. 对照三要素：**函数对不对**（function 名）、**阈值对不对**（params；用 0/极端值对比）、**数据有没有**（inputs 语义表行数——如 lnk_co_located 0 行 = 无数据，规则诚实不命中，禁止充数）
4. 线索侧：`output/lineage_clues.json` 中 `detail.rule_id` 挂钩 + 顶层 `source_rows` 行数应与函数复算一致
5. 报 loader 硬失败时，按第五节清单对号入座

## 五、loader 硬失败清单（装载即报错，对号入座）

- `schema_version` ≠ 2；JSON 语法错误
- 规则 `id` 重复；`stage`/`dimension`/`jian_types`/`hit_when` 枚举越界
- 缺必填：id/stage/title/rule_text/function/hit_when；`rule_text` < 30 字
- `function` 名不在 functions.json；`params` 键不是该函数 parameters 的子集
- params 值类型不符（integer 传字符串等）；string 值不在 enum 白名单
- 函数侧：parameter `type` 非法（仅 integer/decimal/date/boolean/string）；string 参数无 enum；SQL 函数参数无 default；SQL 占位符与 parameters 双向不一致（missing=SQL 用了没声明 / unused=声明了 SQL 没用）

最快复现：`python -m scripts.build_ontology`（load_pack 全量交叉校验，不用跑全链路）。

## 六、验证（WSL 默认环境，venv `/root/.venvs/inves`）

```bash
# 1. 最快：装载校验（rules/functions 交叉引用，秒级）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.build_ontology"
# 2. 全量：70 组测试必绿（ontology/spec/rule_dsl 等组含规则手册与 schema 用例；--fast 跳 e2e、--only <组> 单跑）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
# 3. 改了 functions.json / MCP 工具 / 规则目录计数时额外跑：
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.mcp_client_test"
# 4. 真实数据命中核对（虚实 finding 数、source_rows 与函数复算一致）：
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_all.py --auto-review --no-cli"
```

排错：WSL 报"模块内容与 Windows 侧编辑不符"先清各级 `__pycache__`；PowerShell 不支持 `&&` 与 bash heredoc，多行命令写进 `wsl -u root -- bash -c "..."` 或用临时 py 文件。

## 七、现状基线（default 包，2026-09-03，改完对照防回归）

- **6 条规则**：R1 季末整数现金存入（资金/生间/H1，window=15、现金存入 token）、R2 整数转账聚合过桥（资金/反间/H4）、R3 通话频次突增（通讯/生间/H3，result_hit，绝对阈值 30 降级判据）、R4 轨迹同框（行为/生间，演示数据 0 行不命中）、R5 工商利益关联（关系/因间/H2）、R6 中标-资金时间窗碰撞（时间/反间/H4，stage=qi_zheng，排除"公司"后缀）
- **10 个 function**；MCP **13 个工具**（9 主工具含 rule_list + 4 个 review/action 点号工具；规则手册相关为 `rule_list`/`function_list`/`function_invoke`）
- 真实数据基线：R1 `window=15` → 3 桶/30 万；`window=0`（旧口径）→ 7 桶；R6 → 7 行；虚实扫描 4 条 finding（R1/R2/R3/R5）
- 规则手册设计全文：`.trae/documents/rule_layer_plan.md`
