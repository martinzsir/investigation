# REQ-P 实施方案 —— 数据地图 + 本体画像（可落地版）

> 依据：[REQ-P.md](./REQ-P.md)（34 项需求，含 DATA_MAP/ONTOLOGY_PROFILER 两份沙盒文档的核对结论）。
> 核对基线：`ontology_v2 @ e53efad`。环境：WSL2 venv `/root/.venvs/inves/bin/python`。
> 验证铁律：每波跑 `python run_tests.py` 全绿；改 MCP 相关加跑 `python -m scripts.mcp_client_test`。

## 〇、总览

| 里程碑 | 波次 | 内容 | 交付判据 |
|---|---|---|---|
| M1 | F 波（REQ-P-031~034） | 已核实数据层缺陷修复 | transfers 归一、举报人归一、归一声明校验、metadata 排除标记；全量回归绿 |
| M2 | M 波（025~030） | 数据地图 L0+L1 | `tests.test_data_map` 建组；真实包缺口=0（M1 后）+ mini 夹具检出能力 AC |
| M3 | P0+P1（001~009） | 画像前置 + 值类型识别 | gateway 值画像方法、`core/value_type.py` 纯函数模块 |
| M4 | P2（010~019） | 六层画像 | `core/ontology_profile.py`；demo 演练输出三层地图 |
| M5 | P3（020~024） | 治理集成 | run_health 接线、GROUPS 注册、全量回归 + MCP 绿 |
| M6 | G 波（035） | 新表接入画像 + 两大推荐器 | `scripts/profile_table.py` + 草案组装器 + 步骤序列推荐器；四问能力闭环 |

**不做什么**：不加 MCP 工具；不写 obj_*/lnk_*；不改检测器代码；不引入 CaseContext（时间窗走 REQ-027 thresholds.json）。

**红线**：只观察不写回；结论恒【待核实】；模拟数据 + PII 形态扫描；取数走网关（数据地图 L0/L1 例外：静态解析声明 JSON 不连库；M6 治理工具读原始外部表为第二例外——只读画像，不落库）。

---

## 一、M1 —— F 波：已核实缺陷修复（独立可先行）

### REQ-P-031 transfers 断链修复

**改动 1：[bindings.json](../../../ontology/default/bindings.json) L92 build_sql**

```sql
SELECT t.txn_id,
       t.from_raw AS from_account, t.to_raw AS to_account,
       fa.account_id AS from_account_id, ta.account_id AS to_account_id,
       t.amount, t.date
FROM obj_transaction t
LEFT JOIN obj_account fa ON fa.raw_name = t.from_raw
LEFT JOIN obj_account ta ON ta.raw_name = t.to_raw
```

设计要点（锁死）：
- **双列方案**：保留 raw 列（`_flow_source` 图库双轨同源红线 + overpass 自连接 `to_account=from_account` 兼容，[graph.py L204](../../../core/graph.py#L204) 不动）；新增 `*_id` 外键列。
- **LEFT JOIN 而非 JOIN**：未命中账户不丢边；account 与 transaction 同源（主体/对方），当前数据 id 非空率应为 100%。
- account 声明（pk=account_id, name_property=raw_name, entity）无需改。

**改动 2：links.json transfers endpoints**

```json
"endpoints": {
  "from": {"col": "from_account_id", "ref": {"object": "account", "key": "account_id", "name": "raw_name"}},
  "to":   {"col": "to_account_id",   "ref": {"object": "account", "key": "account_id", "name": "raw_name"}},
  "extra": ["from_account", "to_account", "amount", "date"]
}
```

连锁回归（按序）：`python -m scripts.build_ontology` → `run_tests.py`（重点 graph/mcp/export_endpoints/golden 组）→ `export_ladybug` 重导出 → `transfer_edges.csv` 列变化属**预期漂移**，刷新 ladybug 比对基线（方式同 G-015：旧 CSV 移 /tmp 存档，新导出与语义层多集核对一致后固化）。

**AC（并入 tests.test_ontology 或新 mini 组）**：
1. build_sql 含 `JOIN obj_account` ×2 且保留 raw 列投影；
2. `lnk_transfers` 行数 = `obj_transaction` 行数（LEFT JOIN 无丢失）；
3. id 列非空率 = 100%（当前数据）；
4. `_flow_source` 双轨仍同源（raw 列在位）；
5. 导出 golden 刷新后 transfer_edges 与语义层一致。

### REQ-P-032 tipoff 举报人归一

**方案 A（采纳）：新增链接** `tipoff_reported_by`（举报人→person），不与 tipoff_targets_person 混方向。
四处同步声明：
- `bindings.json`：`{"link": "tipoff_reported_by", "build_sql": "SELECT t.tipoff_id, p.person_id, t.reporter_raw, t.title AS tip_type, t.submit_date FROM obj_tipoff t JOIN obj_person p ON p.raw_name = t.reporter_raw"}`
- `links.json`：新链接 + endpoints（from: tipoff_id ref tipoff/tipoff_id/title；to: person_id ref person/person_id/raw_name）——顺带补齐 tipoff_targets_person 的 endpoints（现为 null）
- `policies.json`：**链接策略必须同步声明，漏了 = 运行时 fail-closed 被拒**
- 回归：build_ontology + 全量；AC：举报材料"匿名"等未命中行不丢（JOIN 语义取舍见下）

> 设计决策：举报人常为"匿名"，等值 JOIN 会静默丢行。与 031 不同（account 同源全覆盖），此处采 **LEFT JOIN + id 可空**，AC 固化"匿名举报不产出边但不丢 tipoff"。

### REQ-P-033 归一映射声明化（两案取舍，v1 采校验一致）

- **v1（本里程碑实施，低风险）**：bindings.json link_bindings 增可选 `"normalize"` 段，声明每条归一 JOIN（table/alias/on/select_as）；loader 校验 **build_sql 实际 JOIN 与声明一致**（缺失/多余/不一致 → 硬失败）。build_sql 仍是唯一执行源——"声明即文档 + 防漂移"，新别名仍需改 SQL 但会被 loader 强制同步校验。
- **v2（远期，另立需求）**：编译器从 links.json endpoints 自动生成归一 JOIN，build_sql 退化为 raw 投影——彻底"改配置不改 SQL"，但动 build_ontology 核心，风险大，不与画像器捆绑。

```json
"normalize": [
  {"as": "from_person", "alias": "p1", "table": "obj_person",
   "on": "p1.raw_name = c.caller_raw", "select": "p1.person_id"}
]
```

**AC**：mini 声明缺 normalize（有 raw JOIN）→ 硬失败；normalize 与 build_sql 不一致 → 硬失败；6 条既有边声明补齐后全量绿。

### REQ-P-034 内容字段排除声明化

- objects.json 各对象增可选 `"metadata_props": ["content_raw", ...]`（对象级数组，不动 properties 的 {attr: type} 结构，**loader 向后兼容**）；loader 校验：metadata_props ⊆ properties 键集，违者硬失败。
- 本仓标注基线（按属性语义人工过一遍）：tipoff: title/submit_date/content_raw；osint_article: pub_date/source_name/crawled_at/retention_days；call: date/times；trackpoint: date/location?（location 参与同框是**关系信号**，不标）；bid_project: pub_date/leader?（leader 是人名属性，可连接，不标）；transaction: date。**每处标注须在 AC 中给出理由**。
- 消费方：REQ-P-001 connectable_props；数据地图 L0 待归一属性清单（content_raw 从"疑似缺口"摘除）。

---

## 二、M2 —— M 波：数据地图（零依赖）

**新模块 `core/data_map.py`**（只 import json/re，**不 import duckdb、不 import core 其他模块**——AC 固化）：

```python
class DataMap:
    @classmethod
    def from_pack(cls, ontology_dir, pack="default") -> "DataMap"   # 读 objects/links/bindings.json
    def objects_inventory(self) -> list[dict]      # L0：name/语义度/物理度/判定
    def lineage(self) -> dict                       # L1：对象←源表（UNION 拆解）、边←来源对象、清洗规则
    def normalize_joins(self) -> list[dict]         # 定向判定 + 等值归一判定
    def normalize_gaps(self) -> list[dict] | None   # None=无法判定（bindings 缺失）
    def render_markdown(self) -> str
    def render_mermaid(self) -> str
```

**四个缺陷判据内置**（写死在实现+AC 双层）：
1. 物理度累计发生在 `_parse_link_sql` 之后、`_find_orphans` 之前（隐形枢纽不丢）；
2. 等值归一判定 = **两侧属性名都含 raw**（`p.raw_name = a.raw_name` 的 owns/osint_mentions 正确判归一）；
3. 定向 = **JOIN 的表是 target**、另一侧是 source；
4. 归一缺口看 **build_sql 是否 JOIN 实体表**，不看属性名是否出现、不看 links.json 端点。

**解析鲁棒（REQ-P-029）**：
- `TABLE_ALIAS_RE` 把 `JOIN` 关键字抓成别名的**行为固化**（AC 断言该误抓 + `_alias_map` 层过滤——防后人"修正则"破坏已验证行为）；
- bindings.json 缺失 → `normalize_gaps() == None` + notes="无法判定归一状态——缺口未计算（不是无缺口）"；
- `obj_\w+` 正则无 CTE/动态表名前提 AC（未来引入 CTE 时测试失败提醒重估）；
- 无 JOIN 的链接（time_window）不报错，标"业务条件连接"。

**与 F 波解耦**：检出能力用 **mini 声明夹具**（构造含断链的假 bindings）测"能检出 3 类缺口"；再用真实 default 包跑一条 AC：**M1 落地后缺口 = 0**。实施顺序无论 F/M 谁先谁后都成立。

**渲染（REQ-P-030）**：Markdown（三层表）+ Mermaid（对象图，断链虚线/隐形枢纽★标注）；AC：输出含"只观察不写回"与【待核实】声明。

**测试**：`tests/test_data_map.py`（沙盒 28 项分组照录 + 上述本仓新增）；`run_tests.py` GROUPS 注册：

```python
"datamap": ("数据地图 L0+L1 静态拓扑与血缘", [sys.executable, "-m", "unittest", "tests.test_data_map"]),
```

---

## 三、M3 —— P0+P1：画像前置与值类型识别

### P0（REQ-P-001~005）

- **`core/gateway.py` 扩两个方法**（复用 `_guard_fresh`/`_resolve` 既有防护）：
  - `materialized_objects() -> list[str]`：declared ∩ information_schema 实表；
  - `materialized_props(props=None) -> dict`：属性级列存在性（`information_schema.columns`，**不按值类型过滤**——画像缺陷 3 的正解）；
  - `value_profile(obj, prop) -> dict`：`COUNT(*) / COUNT(col) / COUNT(DISTINCT col) / MIN/MAX / LIMIT 5 样例` 单 SQL 聚合；
  - `value_overlap(obj_a, prop_a, obj_b, prop_b) -> dict`：精确交集/包含率（子查询 IN，不采样）。
- **`core/ontology_profile.py` 新模块**（M3 起建，M4 扩完）：
  - `connectable_props(pack) -> dict[object, list[prop]]`：string 属性 − metadata_props（REQ-P-034）− runtime 对象；来源 pack 声明；
  - `EntityLinkExplorer`（对齐沙盒名的编排器）：connectable_props + materialized 判定 + 变体双轨；
  - 变体：规则轨复用 `entity_resolution._name_similarity/_pinyin_key`（import 复用不复制）；别名轨读 `load_case_knowledge(pack)["subject_aliases"]`。
- **AC 要点**：decimal/date 属性被 materialized_objects 覆盖（缺陷 3 反向验证）；value_profile 不取回全量（AC：样例 ≤5 行）；变体 0 ≠ 干净 + 无别名表降级。

### P1（REQ-P-006~009）：`core/value_type.py` 纯函数模块（零依赖）

```python
ORDER = ["phone", "id_card", "date_str", "amount", "account", "number"]   # 否定式，特异性降序
AMOUNT_RE = 编译"货币符号|千分位|小数点|万元亿单位 至少其一"       # 缺陷1：拒判 6222000111110001
def classify(value: str) -> str                    # 按序判定；全不中 → 人名/机构名（肯定式，需确认）
def analyze_column(values) -> dict                 # 分布 + 混装判定 + 归一落点建议（两个方向都报）
```

- 混装判定：分布中"否定式类"≥2 类 → mixed；落点建议 = 各类型映射的实体对象（account/person），**只建议不决定**。
- AC：`¥1,200,000`/`15万元`→金额、`6222000111110001`→账号、`2021-10-01`→日期、`13800138000`→手机号；元数据属性调用侧跳过（REQ-P-009 由 profiler 编排层保证，value_type 本身不管）。

---

## 四、M4 —— P2：六层画像（`core/ontology_profile.py` 扩完）

```python
class OntologyProfiler:
    def __init__(self, gateway: OntologyReadGateway, pack="default", window_days=None)
    def profile_all(self) -> dict        # 三层地图：L0(L1L2) / L3 / L4(L5)
```

- **L0**：显式 `"not_applicable"` 节点（物化后无文件）——地图的 L0 拓扑由 core/data_map.py 承担，画像报告引用之，不重复实现。
- **L1/L2**：每 对象.属性：空值率/value_profile/值类型分布/混装/落点；未物化对象输出占位条目不报错。
- **L3 四指标**（SQL 模板，全走 gateway，参数化）：
  - 万元整数率：`AVG(CASE WHEN MOD(CAST(amount AS BIGINT),10000)=0 THEN 1.0 ELSE 0 END)`（仅 decimal）；
  - 时间窗覆盖：`window_days` 从 **thresholds.json** 读——`core/threshold.py` loader 增 `profiler` 段：

    ```json
    "profiler": {"window_days": 20, "_note": "REQ-P-014：画像时间窗参数，AC 固化可配置"}
    ```

  - 关注命中：`focus_entities` 参数（调用方传，不硬编码人名——对齐 R5 教训）；
  - 重合数：`COUNT(DISTINCT CASE WHEN col IN (known) THEN col END)`。
- **L4 间类**：`load_pack(pack)` 遍历 objects/links 的 `jian` 字段（G-013 声明），正反两表；**禁止 DEFAULT_JIAN_MAP**（AC：grep 模块源码无该名）。
- **L5 质量分**：权重表为模块常量（阻断：混装-25/空值率≥50%-20/0行-30；告警：未物化-12/基数≤2-8/含肯定式-5/无万元整数-5/有变体-5）；只对可连接属性计分；输出含"结论可推翻"标记 + 分数区间 AC。
- **混装实测**：本仓 from_raw 分布实施后实测（golden/demo 数据），AC 断言"有分布输出"而非沙盒的 50/50 数值。

---

## 五、M5 —— P3：治理集成

- **REQ-P-020**：profiler/data_map 无任何写路径（AC：模块源码无 INSERT/UPDATE/COPY/CREATE TABLE）；【待核实】文案在 render 层固化。
- **REQ-P-021 健康度接线**：profiler 接受 `health=None`（None→NullRunHealth，REQ-G 兼容红线），诊断类型：
  - `profile_missing_column`（warning，REQ-P-012）
  - `profile_unmaterialized`（info）
  - `version_anchor_missing`（沿用 G-007）
  - `map_normalize_gap`（warning，M 波缺口检出）
  `record(kind, severity, source="ontology_profile", reason=...)` 对齐 [run_health.py L83](../../../core/run_health.py#L83) 签名。
- **REQ-P-022**：数据源纪律 AC——profiler 只经 gateway（gateway 自带 STALE/未知对象防护）；data_map 只读声明 JSON；夹具 PII 扫描复用 test_golden 的 `_PII_PATTERNS`。
- **REQ-P-023**：GROUPS 注册 `"profiler"` 组；演练脚本 `scripts/demo_profile.py`（读真实 pack → 输出 output/profile_report.md + mermaid，不写库）；**MCP 不加工具**。
- **REQ-P-024**：技术债两条登记在本文档尾部（不建 AC）：肯定式识别改进（领域词典/人工回流）、`_columns_of` 缓存失效。

---

## 六、M6 —— G 波：新表接入画像与两大推荐器（REQ-P-035）

> 目标：把 REQ-P 从"诊断工具"升级为"诊断 + 生成"，闭环回答四问：表/列情况、候选关联、
> ontology 模型推荐（草案组装器）、ETL 步骤推荐（序列推荐器）。依赖 M3（value_type/value_overlap）
> 与 REQ-P-034（metadata_props 标记），M4 后启动。

### REQ-P-035① 画像入口与 TableProfile 契约

**新脚本 `scripts/profile_table.py`**（CLI）：

```bash
python scripts/profile_table.py --input data/samples/xxx.xlsx [--sheet 名] [--pack default]
# 经 data_ingest.py 适配器读表 → 输出 output/profiles/<table>_profile.json + Markdown
```

**TableProfile 契约**（`core/ontology_profile.py` 内 dataclass，M6 消费方共用）：

```python
@dataclass
class ColumnProfile:
    name: str; null_rate: float; type_dist: dict; samples: list
    mixed: bool | None; landing_suggestions: list[str]      # 混装落点（008 复用）

@dataclass
class TableProfile:
    table_name: str; row_count: int; columns: list[ColumnProfile]
    candidates: list[CandidateAssoc]   # 对 obj_* 可连接属性 value_overlap ≥ 阈值
    # CandidateAssoc: (col, target_obj, target_prop, overlap_ratio, direction)
```

- 候选关联阈值 `draft_overlap_min_ratio`（默认 0.8）入 thresholds.json `profiler` 段，AC 固化可配置。
- **边界声明**：治理工具读原始外部表是本职（只读画像），与"检测器/图库/MCP 不准直读 Parquet"红线不冲突
  （该线约束的是取证取数路径）——AC：profile_table 不写任何库表。

### REQ-P-035② 草案组装推荐器（DraftAssembler）

**`core/draft_assembler.py` 新模块**：

```python
class DraftAssembler:
    def __init__(self, profile: TableProfile, pack="default")
    def draft_object(self) -> dict      # objects.json 草案
    def draft_bindings(self) -> dict    # bindings.json 草案（object_binding + link_binding）
    def draft_links(self) -> list[dict] # links.json 草案（候选关联 → 链接 + endpoints ref）
    def write_drafts(self, out_dir="output/drafts") -> list[Path]
```

组装规则（每条建议必须携 `_evidence` 画像证据）：
- **值类型映射**：value_type 判定 → TYPE_SQL 同口径（phone/id_card/account/人名机构名→string、number→integer
  或 BIGINT、amount→decimal、date_str→date、布尔→boolean）——映射表为常量，与 [core/ontology.py](../../../core/ontology.py)
  TYPE_SQL 双向核对（AC：不存在 TYPE_SQL 不认识的输出类型）；
- **pk/name_property 候选**：高基数 + 否定式标识类（账号/证件/编号）→ pk 候选；人名/机构名 → name_property 候选
  （恒为候选，人工确认）；
- **metadata_props 建议**：content/长文本/时间戳类非标识列 → 建议排除（联动 REQ-P-034）；
- **候选关联 → 链接草案**：overlap ≥ 阈值 → 建议归一 JOIN（`LEFT JOIN obj_<t> ON <t>.raw_name = <col>`）+
  lnk 端点 ref（对齐 031 的双列方案：raw 列保留 + id 列）；
- **草案头**：`{"schema_version": 2, "_draft": true, "_status": "待核实", "_evidence": {...}}`；
- **落点纪律**：只写 `output/drafts/<table>/`（AC：源码无任何指向 `ontology/` 的写路径）；人工审核后
  复制进 `ontology/<pack>/`，经 build_ontology（loader 校验）+ 人工确认两道闸才生效——三禁令不破。

**AC**：mini 表夹具 → 草案三件齐全且 `_evidence` 非空；草案复制到临时 pack 能过 loader（可选强 AC）；
值类型映射与 TYPE_SQL 全覆盖；输出目录不含真实 PII。

### REQ-P-035③ 步骤序列推荐器（recommend_steps）

**`core/draft_assembler.py` 同模块函数**：

```python
STEP_ORDER = ["split_mixed", "clean_rules", "type_cast", "cold_table",
              "bind_object", "bind_links", "reprofile"]
def recommend_steps(profile: TableProfile) -> list[dict]
# 每步: {"step", "why"(画像证据), "how"(落到既有命令/声明字段), "done_when"(可执行验收)}
```

依赖排序规则（写死 + AC）：
1. **混装拆分**必须先于一切绑定（G-015 教训：落点不明确时探测/连线会同时报两方向）；
2. **清洗规则**先于冷层建表（CTAS 用源列类型）；clean 名只能引用 loader 已声明规则（AC：不存在→建议为"需新增清洗规则"而不是编造名字）；
3. **冷层建表**直接引用 G-014（"加 binding 即自动建表"，给出 binding 草案位置）；
4. **绑定**后必须 **reprofile 复检**（闭环：复检结果回写 TableProfile，验证混装/空值率改善）；
5. **序列退化**：无混装无缺值干净表 → `[cold_table, bind_object, reprofile]`（AC 固化）；
6. **只输出清单不自动执行**（AC：函数无任何 IO 副作用，执行仍走 data_ingest / build_ontology 既有命令）。

**输出形态**：并入 profile_table 的 Markdown 报告（"建议 ETL 步骤"节），每步可追溯到 `_evidence` 编号。

### M6 测试与注册

- `tests/test_draft_assembler.py`（草案组装 + 步骤序列 + 退化 + 副作用为零 + TYPE_SQL 映射核对）；
- GROUPS 注册 `"drafts"` 组；
- 不新增 MCP 工具（MCP 面向办案，建模是治理面）。

---

## 七、验证协议与提交切分

| 批次 | 提交内容 | 验证命令 |
|---|---|---|
| C1 | M1（031+032+033v1+034） | `build_ontology` → `run_tests.py` → `export_ladybug` → ladybug 基线刷新 → `mcp_client_test` |
| C2 | M2（data_map + datamap 组） | `run_tests.py --only datamap` → 全量 |
| C3 | M3（gateway 扩展 + value_type） | `run_tests.py --only gateway` 相关 + 全量 |
| C4 | M4（ontology_profile） | `run_tests.py --only profiler` + demo 演练 |
| C5 | M5（health 接线 + GROUPS） | 全量 + `mcp_client_test` + `run_all --auto-review --no-cli` 冒烟（健康度首节 + 画像节） |
| C6 | M6（profile_table + DraftAssembler + recommend_steps） | `run_tests.py --only drafts` → 全量；演练：对演示表跑 profile_table → 草案人工审核 → 临时 pack build_ontology 通过 |

**回归敏感点清单**：graph 组（12 项，transfers 双轨）、export_endpoints 组（G-015）、golden 组（若快照涉及）、mcp（69 项）、ontology 组（函数/规则计数不受影响——本方案不加 Function）。

## 八、风险与对策

| # | 风险 | 对策 |
|---|---|---|
| 1 | transfers 列变化连锁 graph/mcp/golden | 双列方案保 raw 语义不变；golden 刷新流程沿用 G-015 存档比对法 |
| 2 | LEFT JOIN NULL 边在导出 ref JOIN 时被丢 | AC 明确当前数据非空率 100%；_edge_sql 对 NULL 的行为在 AC 里固化（不丢边断言） |
| 3 | metadata_props 标注误伤（location 这类关系信号被排除了） | 每处标注 AC 附理由；co_located 依赖 location → 明确不标 |
| 4 | 画像权重照抄沙盒不适配本仓数据 | 权重为常量+AC 只锁区间与可推翻性，数值可随实测调 |
| 5 | 033 v1 校验过严阻断日常改声明 | normalize 段可选；未声明归一 JOIN 的边（time_window）不校验 |
| 6 | 草案组装器建议质量低（pk 候选/类型误判）误导人工审核 | 每条建议强制 `_evidence` 画像证据可追溯；恒为候选 + 三道闸（草案目录→人工审核→loader 校验）兜底；阈值 draft_overlap_min_ratio 可配置 |
| 7 | data_ingest 适配器行为差异（Excel 日期自动解析等）扭曲画像 | profile_table 固定 raw 模式读取（禁适配器隐式类型转换）；AC：同一 CSV 经不同适配器画像结果一致 |
