# REQ-P 本体画像与数据地图 —— 交付总结报告（M1-M6）

- 分支：`ontology_v2`（已全部推送至 https://github.com/martinzsir/investigation.git）
- 区间：`e53efad..9cd3760`（7 个提交：6 个里程碑 + 1 个文档）
- 全量回归：**70 组测试全绿**（其中 REQ-P 新增 5 个测试组、共 109 项 AC）
- 交付形态：4 个新核心模块 + 2 个 CLI/演练脚本 + 5 个新测试文件 + 1 份使用指南

---

## 一、总览

REQ-P 把外部沙盒文档（ONTOLOGY_PROFILER.md / DATA_MAP.md）的「本体画像 + 数据地图」
方法裁剪到本仓，核心是把数据画像的单位从**物理列**搬到**对象.属性**，全部取数复用语义层
网关，检测器/图库/MCP 不破「不准直读 Parquet」红线。

| 里程碑 | 波次/需求 | 提交 | 规模 | 新增测试 |
|---|---|---|---|---|
| M1 | F 波（031-034）数据层缺陷修复 | `9c1bc93` | 13 文件 +1000/-32 | reqpm1 组 12 项 |
| M2 | M 波（025-030）数据地图 | `f608a93` | 3 文件 +795 | datamap 组 24 项 |
| M3 | P0/P1 画像前置 + 值类型识别 | `ea07c35` | 6 文件 +773 | valuetype 组 19 项 + gateway 扩 17 项 |
| M4 | 六层本体画像 | `e1866e8` | 6 文件 +652 | profiler 组（M4 时 27 项） |
| M5 | 治理集成 | `12a5ec2` | 5 文件 +357/-1 | profiler 组扩至 34 项 |
| M6 | G 波（REQ-P-035）新表画像 + 推荐器 | `67aba45` | 5 文件 +947 | drafts 组 20 项 |
| 文档 | 草案组装器使用指南 | `9cd3760` | 1 文件 +271 | — |
| **合计** | | | **代码 ~4524 行新增** | **109 项 AC** |

---

## 二、各里程碑交付内容

### M1 —— 数据层缺陷修复（F 波，REQ-P-031~034）

落地真实数据缺陷，全部为语义层绑定/声明问题：

- **031 transfers 断链修复**：`bindings.json` 补 LEFT JOIN，双列方案——raw 列保留 +
  新增 `from_account_id`/`to_account_id` 外键，NULL 边不丢（此前直接 JOIN 导致断链）。
- **032 tipoff_from_reporter**：举报人归一链接，LEFT JOIN 匿名降级（材料不丢、边可空）。
- **033 归一声明化**：8 条链接补 `normalize` 段声明，loader 硬失败四态校验
  （JOIN 不一致 / select 未投影 / 未声明 raw_name 等值 JOIN / 业务 JOIN 豁免）。
- **034 metadata_props**：org/bid_project/tipoff/osint_article/clue/decision 六对象标注
  元数据属性（`transaction.date`/`trackpoint.location` 是关系信号，不标）。

### M2 —— 数据地图（M 波，REQ-P-025~030）

[core/data_map.py](../../../core/data_map.py)，**零依赖**（仅 json/re，ast 静态扫描，不连库）：

- **L0 静态拓扑**：对象资产清单、语义度（非 runtime 链接端点计数）/物理度（引用该对象的
  链接绑定数）、判定核心枢纽(≥5)/枢纽/★隐形枢纽/孤立。
- **L1 物理血缘**：UNION 拆解对象←源表、清洗规则、边←物理来源对象。
- **归一判定与缺口检测**：等值归一（两侧属性含 raw）、归一定向（JOIN 表为 target）、
  缺口看 build_sql 不看端点；缺陷 1~4 判据内置（物理度先于孤儿判定等）。
- 渲染：Markdown 报告 + Mermaid 图（断链虚线）。
- 真实包 M1 后缺口=0；mini 夹具检出 3 类缺口。

### M3 —— 画像前置与值类型识别（P0/P1）

- [core/value_type.py](../../../core/value_type.py)（纯函数，仅 re/unicodedata）：
  `classify()`/`analyze_column()`。**缺陷 1**（金额串正则收紧：要求货币符号/千分位/小数/
  万元单位至少其一，`6222000111110001` 不再误判金额）、**缺陷 2**（否定式按特异性降序：
  手机号→身份证→日期→金额→账号→纯数字）内置；混装口径=归一落点≥2；落点两方向都报。
- [core/ontology_profile.py](../../../core/ontology_profile.py)：`connectable_props()`
  （string − metadata_props − runtime）+ `EntityLinkExplorer` 变体双轨
  （规则轨复用 entity_resolution；别名轨读 case_knowledge，无别名表显式降级）。
- [core/gateway.py](../../../core/gateway.py) 扩 5 个画像方法：`materialized_objects()`/
  `materialized_props()`（**缺陷 3**：拆成两方法，amount/date 不再误判未物化）、
  `value_profile()`/`value_overlap()`/`distinct_values()`；`_require_prop()` fail-loud
  （**缺陷 4**：缺列标注而非裸 BinderException 崩溃）。

### M4 —— 六层本体画像 OntologyProfiler

[core/ontology_profile.py](../../../core/ontology_profile.py) 新增 `OntologyProfiler`：

- **L0** 文件层 → not_applicable（物化后无文件），拓扑引用 data_map。
- **L1/L2 列层/值层**：每 对象.属性 空值率/基数/样例/值类型分布/混装/落点；未物化对象与
  缺列输出占位不报错（缺陷 4）；非 string 不做值类型识别。
- **L3 语义层**：四指标（万元整数率/时间窗覆盖/关注主体命中/与已有重合数）全走 gateway
  `prop_indicator()`（SQL 模板固定网关内，IN 列表参数绑定）；focus_entities/anchor_date
  调用方传入不硬编码；窗口从 thresholds 读可配置。
- **L4 间类层**：五间只从 objects/links 的 `jian` 声明读正反两表（本仓五间全覆盖，无硬编码映射）。
- **L5 决策层**：质量分权重常量（阻断：混装-25/空值≥50%-20/0行-30；告警：未物化-12/
  基数≤2-8/肯定式-5/无万元整数-5/有变体-5），只对可连接属性计分，输出分数区间 +
  【待核实】「结论可推翻」声明。
- 参数声明化：`thresholds.json` profiler 段（window_days/draft_overlap_min_ratio/value_sample_limit）。

### M5 —— 治理集成

- [core/run_health.py](../../../core/run_health.py) KINDS 增三类诊断：
  `profile_missing_column`(warning)/`profile_unmaterialized`(info)/`map_normalize_gap`(warning)。
- OntologyProfiler 加 `health=None`（不传零行为变化）：未物化/缺列/版本锚点缺失落诊断；
  `record_map_gaps()` 由编排层落 data_map 缺口（data_map 零依赖不 import core）。
- [scripts/demo_profile.py](../../../scripts/demo_profile.py)：真实库 → output/profile_report.md
  + data_map.md；默认不写任何库表，`--record-diagnostics` 才落 run_diagnostic；关注主体从
  case_knowledge 声明读。
- 治理 AC：两模块源码 grep 无写路径（INSERT/UPDATE/DELETE/COPY/CREATE/DROP/conn.execute）、
  【待核实】文案 + PII 扫描固化、默认不写诊断表。
- 技术债登记（REQ-P-024）：肯定式识别改进方向；materialized_props 无缓存未来加缓存须随 ALTER 失效。

### M6 —— 新表画像 + 草案组装器与步骤推荐（G 波，REQ-P-035）

- `TableProfile` 契约（ColumnProfile/CandidateAssoc/TableProfile）+ `build_table_profile()`：
  raw 字符串矩阵输入；候选关联 = 外部列 distinct ∩ 各对象 **name_property 身份列**，
  overlap ≥ 阈值（0.8，收窄到身份列避免对 relation/from_raw 误报）。
- [core/draft_assembler.py](../../../core/draft_assembler.py)：
  - `VALUE_TYPE_TO_PROP_TYPE` 映射（输出 ⊆ TYPE_NAMES，AC 双向核对）+ 列名语义启发
    （金额/日期/是否列纠偏裸整数金额 `100000` 误判账号）。
  - `DraftAssembler.draft_object/links/bindings`：草案头 `_draft/_status=待核实/_evidence`；
    pk/name_property/metadata 候选纪律；links 双列方案（raw + `<col>_id` 外键，LEFT JOIN）；
    clean 只引用已注册规则（不编造）；`write_drafts()` 只写 output/drafts/，函数体无 ontology 路径。
  - `recommend_steps()`（零 IO）：STEP_ORDER = split_mixed→clean_rules→type_cast→
    cold_table→bind_object→bind_links→reprofile；混装必先拆、绑定后必复检；干净表退化三步；
    每步 why/how/done_when。
- [scripts/profile_table.py](../../../scripts/profile_table.py)：**raw 模式**（dtype=str，
  禁适配器隐式 coerce）读 csv/tsv/xlsx/sqlite/parquet/json → output/profiles 画像报告 +
  output/drafts 三件草案。
- 使用指南：[草案组装器使用指南.md](./草案组装器使用指南.md)。

---

## 三、新增资产清单

**核心模块（core/）**

| 文件 | 职责 | 里程碑 |
|---|---|---|
| data_map.py | L0 拓扑 + L1 血缘，零依赖静态解析 | M2 |
| value_type.py | 值类型识别纯函数（缺陷 1/2 内置） | M3 |
| ontology_profile.py | connectable_props + EntityLinkExplorer + OntologyProfiler + TableProfile | M3-M6 |
| draft_assembler.py | DraftAssembler 草案组装 + recommend_steps | M6 |

**脚本（scripts/）**：`demo_profile.py`（M5 演练）、`profile_table.py`（M6 新表画像 CLI）

**测试（tests/，5 个新文件 109 项 AC）**：test_reqp_m1(12) / test_data_map(24) /
test_value_type(19) / test_ontology_profiler(34) / test_draft_assembler(20)；
另 test_gateway 扩 17 项画像 AC。run_tests.py 注册 reqpm1/datamap/valuetype/profiler/drafts 五组。

**声明/配置变更**：ontology/default/bindings.json（双列+normalize）、objects.json
（metadata_props）、links.json（新链接）、thresholds.json（profiler 段）。

---

## 四、设计红线（全程未破）

1. **声明是数据、实现是代码**：判据/阈值/元数据/归一走 ontology JSON + loader 硬校验，
   检测器与画像器不写业务 SQL。
2. **只读取数走网关**：画像/探测消费 obj_*/lnk_* 语义表；治理工具读原始外部表是本职
   （profile_table raw 只读），与「检测器/图库/MCP 不准直读 Parquet」不冲突。
3. **生成 ≠ 生效**：草案只写 output/drafts/，工具函数体不构造 ontology/ 路径；人工审核
   复制 + build_ontology loader 校验两道闸后才物化。
4. **只观察不写回**：画像/地图/recommend_steps 零写库；全部结论【待核实】可被人工推翻。
5. **不新增 MCP 工具**：建模/画像属治理面，MCP 面向办案。

---

## 五、实测发现（真实数据）

- `transaction.from_raw` 账号/人名混装、`org.raw_name` 机构/人名混装、`account.raw_name`
  含主体名——均被值类型识别正确检出（混装危害=归一落点不明确，须先拆分）。
- 数据地图真实包归一缺口 M1 后清零；mini 夹具可复现 from_raw/to_raw/reporter_raw 三类缺口。
- 新表画像对 `工商信息.xlsx` 演练通过：raw 读取、候选关联、草案三件、5 步 ETL 建议均正确产出。

---

## 六、已知技术债（后续）

1. **肯定式识别仍误判**：人名/机构名对「存续/供应商/设备采购」等词有误判，已用
   「只对可连接属性报警 + metadata 排除」规避；改进方向为领域词典或人工确认回流。
2. **时间窗参数**：已声明化到 thresholds.json，可进一步接参数治理。
3. **materialized_props 缓存**：当前实时查 information_schema 无缓存，未来加缓存须随
   表结构变更（ALTER）失效。
4. **规则轨变体检测阈值**：短名单字差异在拼音空间相似度偏低，默认阈值 0.85 检不出，
   需低基数字典或人工别名补充（阈值可配置，AC 固化）。

---

## 七、验证记录

- 全量 `python run_tests.py`：**70 组全部通过**（含 mcp/golden/e2e）。
- MCP 端到端 `mcp_client_test` 未受影响（画像/建模不加 MCP 工具）。
- ladybug golden 在 M1 刷新（transfer_edges +2 列、新增 tipoff 边文件），后续里程碑逐字节稳定。
- 数据管线产物（parquet/lbug/xlsx/sqlite/CSV）与 output/ 演练产物（profiles/drafts，
  已 gitignore）按仓库惯例不入版本库。
