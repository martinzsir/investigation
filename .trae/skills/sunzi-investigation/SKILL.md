---
name: sunzi-investigation
version: 1.1.0
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
1. 把案件要素展开成可复查的推演（庙算，`core/hypotheses.py`）——四层覆盖完整性机制：**数据驱动**（`auto_from_findings()` 按模式库把虚实扫描发现自动映射为候选假设，模式库按资金/通讯/行为/关系/时间五维组织）× **规则约束**（≤5 条、四字段必备、超授权边界标"受限(待授权)"、数据源缺失标"降级"，`build()` 内置）× **反遗漏**（五维度覆盖度双轨报警：声明轨 ≤80% 报警 G-009、实证轨缺口独立报警 G-024——"想到了但没查到"落 `miaosuan:dimension:empirical` 诊断；间类缺口警告、证据冲突检测、`enumerate_space()` 枚举候选池+候补清单）× **人机协同**（正兵 `add()/remove()/reorder()/promote()` 受控接口，全程审计）
2. 把全量数据里的反常标成候选突破口（虚实/用间，经规则手册调只读 Function 消费 `obj_*`/`lnk_*` 语义层）
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

辅助工具：`graph_overpass`（图库两跳过桥双轨）/ `clue_list` / `clue_transition` /
`function_list`+`function_invoke`（Ontology Function 只读计算目录/调用）/
`rule_list`（自然语言规则手册目录）/ `run_pipeline`（需 confirm:true）/
`review.list_pending`+`review.get_evidence`（提案待审队列/取证）/
`action.status`（处置状态只读查询，`pp-` 前缀走 ProposalStore）/
`review.submit_proposal`（Agent 唯一写通道，全程不触 ActionExecutor，提案 ID `pp-` 前缀）。
**MCP 共 13 个工具**（9 主工具 + 4 个 review/action 点号工具）。
**规则编排约定**：虚实研判先 `rule_list` 读规则原文（rule_text 是判据依据），再按规则调
`function_invoke`（参数以规则 params 为基准，可在规则语义内调参对比）；
不得自创规则外判据、不写 SQL、不造数，线索表述必带 rule_id。
MCP server：`python -m scripts.mcp_server`（stdio，JSON-RPC 2.0，零第三方依赖）。

## 五、标准输出（每次响应固定六段）
【庙算基线】【双向盘点】【虚实扫描】【奇正分工】【用间交叉】【全胜校验】
产物落 `output/`：`lineage_clues.json`（首部固定「健康度」小节：`run_diagnostic` 留痕汇总，
status=healthy/degraded/critical，warning/critical 项需人工复核、不参与线索升格；
维度覆盖双轨缺口/零命中规则/推翻率/审计链缺口/异常线索统一在此可见。
正文含假设链/溯源行/间类/优先级/处置状态/审计链，`miao_coverage` 带维度双轨明细）、
`entity_mapping.json`、`review_queue.json` 等；线索看板见 `clue_disposal_status` 表。

## 六、红线与免责（必须随技能分发）
- 本技能不构成办案指导，不替代《刑事诉讼法》/取证规则
- 任何输出标注"AI辅助推演，非证据"；全部返回体强制携带 `needs_human_review` 与 `定性_policy`
- 无合法授权字段时，拒绝执行调取类建议
- 模型幻觉必须在【全胜校验】里自曝，不许藏

## 七、校验
```bash
python run_tests.py                  # 70 组测试必须全绿（--fast 跳 e2e、--only <组> 单跑；
                                     #   含 reqpm1/datamap/valuetype/profiler/drafts 等治理组）
python -m scripts.mcp_client_test    # MCP 端到端（69 项、13 工具），改 MCP 后必跑
```
Schema 与红线校验在 `core/validate.py`；`--auto-review` 仅限演示，生产必须人工逐条确认。

## 八、数据输入约定
- 业务数据：`data/*.parquet`（银行流水/通话记录/招投标档案/工商信息/轨迹出行/公开OSINT/举报材料），重跑 `python -m scripts.init_duckdb` 挂载
- 语义层（Ontology，声明是数据/实现是代码）：`ontology/<pack>/*.json`（objects/links/bindings/rules/functions/actions/policies/views **八段**，schema_version=2，另配 thresholds/case_knowledge/dimensions/enum_space/llm_policy）由 `core/ontology_loader.py` 装载校验，`python -m scripts.build_ontology` 编译出 `obj_*`/`lnk_*`（run_all 步骤 6.5 自动执行）。Object/Link 为表；**Function 只读**（functions.json 声明 + `core/functions.py` 注册实现，SQL 强制 SELECT/WITH 白名单 + `{{param}}` 模板参数，string 参数仅 enum 白名单，检测器只是 Function 薄编排）；**Rule 是自然语言规则手册**（rules.json：rule_text 判据原文 + function/params 挂钩，`core/rules.py` 确定性执行，LLM 经 rule_list 读取解释、不执行文本）；**Action 可写**（actions.json 声明 + `core/action_executor.py` 唯一写路径，file 副作用创建 obj_decision 决策对象）；**权限**（policies.json + `core/policy.py`，未声明 fail-closed + 敏感列遮蔽）。新案件/新数据源/新检测规则：加 ontology 案件包 JSON，`--pack <包名>` 切换，不改检测器
- 任意格式接入：CSV/Excel/JSON/SQLite/Parquet 走 `data_ingest.DataIngestManager`（自动检测分隔符/映射中文列名/保留 `_source_file` 溯源列）
- 图库边表：`python -m scripts.export_ladybug` 从语义层导出 → `data/ladybug/*.csv` → COPY 进 LadybugDB
- 增量：`python -m scripts.incremental --quarter 2024-Q4`
- 本体画像与数据地图（治理面，只读，REQ-P，不加 MCP 工具）：
  - 六层本体画像 `python -m scripts.demo_profile` → `output/profile_report.md`（空值率/值类型分布/混装判定/语义指标/五间覆盖/质量分；`core/ontology_profile.py` OntologyProfiler + `core/value_type.py`）；
  - 数据地图 L0 拓扑 + L1 血缘（`core/data_map.py` 零依赖静态解析，归一缺口检测）；
  - 新表接入画像 + 草案组装 `python -m scripts.profile_table --input <外部表>`（raw 只读，禁适配器隐式类型转换）→ `output/profiles/` 画像 + `output/drafts/` 三件【待核实】草案（objects/links/bindings）+ ETL 步骤序列；`core/draft_assembler.py` 的 DraftAssembler/recommend_steps。**草案只写 output/drafts/，人工审核复制进 ontology/ 经 build_ontology 校验才生效，画像/地图/草案只观察不写回**。详见 `.trae/documents/REQ-P/草案组装器使用指南.md`。

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
