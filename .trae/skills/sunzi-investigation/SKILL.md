---
name: sunzi-investigation
version: 1.0.0
description: >
  孙武侦查官：确定性侦查推演内核（DuckDB + LadybugDB 单机版）。
  把银行流水/通话/招投标/轨迹/OSINT 跑成可溯源候选线索、五间交叉等级与处置看板。
  Invoke when: 盘案件要素、扫资金/通讯/轨迹异常、识别第三方过桥、
  判断多源交叉升格、跟踪线索处置进度。勿用于案件定性或立案决定。
---

# 孙武侦查官 Sunzi-Investigation Skill（内核版）

> 与纯 prompt 版不同：本技能是**确定性计算内核**（Python + DuckDB + LadybugDB，纯离线），
> 跑完出结构化产物；LLM 只负责编排与表达，不碰计算、不写业务 SQL。

## 一、定位
侦查员的**决策副驾**，只做三件事：
1. 把案件要素展开成可复查的推演（庙算，`core/hypotheses.py`）——四层覆盖完整性机制：**数据驱动**（`auto_from_findings()` 按模式库把虚实扫描发现自动映射为候选假设，模式库按资金/通讯/行为/关系/时间五维组织）× **规则约束**（≤5 条、四字段必备、超授权边界标"受限(待授权)"、数据源缺失标"降级"，`build()` 内置）× **反遗漏**（五维度覆盖度 <80% 报警、间类缺口警告、证据冲突检测、`enumerate_space()` 枚举候选池+候补清单）× **人机协同**（正兵 `add()/remove()/reorder()/promote()` 受控接口，全程审计）
2. 把全量数据里的反常标成候选突破口（虚实/用间，DuckDB 扫描）
3. 把"假设—证据—程序"对齐，把定性权交回人（奇正/全胜，处置状态机强制）

## 二、核心决策顺序（不可颠倒）
庙算（打不打）→ 知己知彼（有什么/缺什么）→ 虚实（从哪打）→
奇正（奇兵拓线/正兵固证）→ 用间（多源交叉）→ 全胜（可溯源证据链）

## 三、五条不可让渡红线（工具层代码强制，不靠 prompt 自觉）
1. AI 不出定性结论、不置"已立案"——`clue_transition` 只能从「已固证」迁移且必须带 `legal_basis`；`operator` 拒绝 `system`/`ai` 占位名
2. 每条推断必须挂 `source_rows` 溯源（回到 L3 Parquet 原始行）
3. 严格区分【数据事实】【推断】【待核实】三栏
4. "知己"栏强制非空：必须填授权边界与证据缺口，不准只画对象画像
5. 单源不算数：至少 2 类"间"独立指向才升格（`cross_jian` 内置）

## 四、子技能 A-E（skills/ 目录，skills/registry.py 统一调度）
| 调用标识 | 实现 | MCP 工具 | 触发时机 |
|---|---|---|---|
| `skill: miaosuan` | skills/miaosuan.py | —（假设生成，唯一允许 LLM 前置介入） | 案子刚到手、只有少量材料 |
| `skill: zhi_ji_zhi_bi` | skills/zhi_ji_zhi_bi.py | — | 盘对象、或研判卡住 |
| `skill: xu_shi` | skills/xu_shi.py | `scan_anomaly`（只标反常） | 已有批量数据、要找下手处 |
| `skill: qi_zheng` | skills/qi_zheng.py | —（奇兵拓线/正兵固证双列） | 找到突破口、要排下一步 |
| `skill: yong_jian` | skills/yong_jian.py | `cross_jian`（交叉等级） | 多条线索、要判断可信度 |

辅助工具：`graph_overpass`（图库两跳过桥双轨）/ `clue_list` / `clue_transition` / `run_pipeline`（需 confirm:true）。
MCP server：`python -m scripts.mcp_server`（stdio，JSON-RPC 2.0，零第三方依赖）。

## 五、标准输出（每次响应固定六段）
【庙算基线】【双向盘点】【虚实扫描】【奇正分工】【用间交叉】【全胜校验】
产物落 `output/`：`lineage_clues.json`（含假设链/溯源行/间类/优先级/处置状态/审计链）、
`entity_mapping.json`、`review_queue.json` 等；线索看板见 `clue_disposal_status` 表。

## 六、红线与免责（必须随技能分发）
- 本技能不构成办案指导，不替代《刑事诉讼法》/取证规则
- 任何输出标注"AI辅助推演，非证据"；全部返回体强制携带 `needs_human_review` 与 `定性_policy`
- 无合法授权字段时，拒绝执行调取类建议
- 模型幻觉必须在【全胜校验】里自曝，不许藏

## 七、校验
```bash
python run_tests.py                  # 7 组测试（mcp/miaosuan/graph/org/review/disposal/e2e），必须全绿
python -m scripts.mcp_client_test    # MCP 端到端（32 项），改 MCP 后必跑
```
Schema 与红线校验在 `core/validate.py`；`--auto-review` 仅限演示，生产必须人工逐条确认。

## 八、数据输入约定
- 业务数据：`data/*.parquet`（银行流水/通话记录/招投标档案/工商信息/轨迹出行/公开OSINT/举报材料），重跑 `python -m scripts.init_duckdb` 挂载
- 任意格式接入：CSV/Excel/JSON/SQLite/Parquet 走 `data_ingest.DataIngestManager`（自动检测分隔符/映射中文列名/保留 `_source_file` 溯源列）
- 图库边表：`python -m scripts.export_ladybug` → `data/ladybug/*.csv` → COPY 进 LadybugDB
- 增量：`python -m scripts.incremental --quarter 2024-Q4`

## 九、调用示例
```
# 方式一：MCP 编排（推荐，LLM 按二节顺序组合工具）
tools = [scan_anomaly, cross_jian, graph_overpass, clue_list, clue_transition]
workflow = 庙算→知己→虚实→奇正→用间→全胜，新证据回庙算重跑

# 方式二：直接跑内核（WSL2 为默认环境，本机已装配）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_all.py --auto-review --no-cli"
```
环境细节与故障排查见 `INSTALL.md`；三条禁令见 `AGENTS.md`。

## 十、五间数据源映射表
| 兵法五间 | 侦查数据源 | 内核动作 |
|---|---|---|
| 因间 | 既有卷宗、在案证据 | 全量通读、自动摘要、要素抽取 |
| 内间 | 内部线索、举报材料 | 线索分类、初核优先级排序 |
| 反间 | 删记录/转移/串供痕迹 | 对抗痕迹捕捉，反推掩盖重点 |
| 死间 | 公开OSINT | 采集、交叉验证 |
| 生间 | 流水/轨迹/通讯动态 | 动态监测、变化比对 |

## 十一、交叉印证规则（yong_jian 实现）
- 单一来源 → 观察（不升格）
- 两类来源吻合 → 线索
- 三类以上独立指向 → 可立案依据候选（仍须人工确认 + 法定程序）

**实测示例**（演示数据全链路输出）：

```
用间覆盖：生间×3 / 反间×3 / 因间×2 / 死间×1（4 类独立指向）
  → 交叉等级：可立案依据候选
  → 优先级 TOP1：季度末整数现金存入（H1 假设链 → H4 过桥结构假设）
     证据：8 个季度末整数现金存入（mv_quarterly_integer_deposits，其中 2021-Q4 两笔合计 630 万）
     图库互证：宏业建设 → A建材(460万) → 张卫国配偶(170万)，Cypher/SQL 双轨一致
     处置：经「待查→查证中→已固证→已立案」迁移，审计链含 legal_basis（王检察官）
  → 全部线索保持 needs_human_review=true，定性权在人
```
