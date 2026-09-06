# REQ-D 业务测试结果：海州"11·03"刷单返利电诈团伙案（端到端验收）

**执行日期**：2026-09-07
**对位文档**：[REQ-D业务测试案例_海州电诈案.md](./REQ-D业务测试案例_海州电诈案.md)
**测试入口**：`python run_tests.py --only reqdcase`（组名已注册进 run_tests.py GROUPS）
**结果**：**24 探针全部通过（24/24 PASS）**；全量回归另见 §4。

---

## 1. 落地物

| 落地物 | 位置 | 说明 |
|---|---|---|
| 数据集生成器 | `scripts/gen_reqd_case.py` | 6 张表单一真源（流水 120 / 通话 80+旧版 10 / 人员 26 / 台账 1 / 复合列拆分·整列各 15），`--fixture` 固化 JSON |
| 测试夹具 | `tests/fixtures/reqd_case.json` | 场景固化（对位 test_golden 模式），探针与数据解耦 |
| 声明包 | `ontology/reqd_case/`（8 文件） | objects / links / bindings / rules / data_elements / clean_rules / enum_space / policies / actions |
| 探针测试 | `tests/test_reqd_case.py` | 5 组 24 探针：装载生存 / 清洗管道 / 质量扫描 / 复合列 / 治理贯通 |
| 注册 | `run_tests.py` | 新增 `reqdcase` 组 |

## 2. 探针逐条结果

### 装载生存组（D-01~D-06）

| 探针 | 验收指向 | 结果 | 备注 |
|---|---|---|---|
| D-01 六源一次装载 0 中断 | 全局 | **PASS** | 8 对象物化行数锁定（person 24 / account 72 / transaction 74 / call 80 / call_old 10 / case_ledger 1 / cp_split 15 / cp_whole 15） |
| D-02 千分位/货币符金额可 CAST | 009 AC-1 | **PASS** | ￥1,280.50×5、48,000.00×10 值精确，74 行金额全非空 |
| D-03 科学计数法 `1.28e3` | 009 边界 | **PASS** | DuckDB DECIMAL 边界行为：解析为 1280.0×3（非 NULL）；失败诊断只挂 date 不挂 amount、不静默 |
| D-04 星号卡号 reject_if 剔除 | 010 AC-1/2 | **PASS**（N-A 降级口径） | 按案例文档降级为计数核对：6 行剔除、clean_stats 留痕（规则/行数/脱敏样本）；本包 reject 不落 build_quarantine |
| D-05 旧版通话缺"对端"列 | B5-01 回归 | **PASS** | 降级类型化 NULL（10 行，callee_raw 全 NULL）+ degraded 留痕 + source_column_missing 诊断 |
| D-06 policies 漏声明遮蔽 | AD-5/002 AC-4 | **PASS** | 负路径：漏声明 id_card 遮蔽 → 装载前硬失败并提示补声明 |

### 清洗管道组（D-07~D-12）

| 探针 | 验收指向 | 结果 | 备注 |
|---|---|---|---|
| D-07 三种脏电话格式归一 | 006 AC-1 | **PASS** | `138 0013 8000`/`+86 138-0013-8000`/`008613800138000` → 同一 11 位号 9 行 |
| D-08 "李  强"变体同代理键 | 005 AC-1 | **PASS** | 新增 `despace` 清洗 op 后内部空格归一，与"李强"合并（强证据一致） |
| D-09 双"李强"强证据全异 | R-1 红线 | **PASS** | 拆 2 簇、全 needs_review、entity_id 互异；**修复：mapping() 不再暴露 needs_review 簇**（见 §3） |
| D-10 组织词剔除留痕+告警 | 008 AC-1/2/4 | **PASS** | 40 行剔除（33%>30% 阈值）→ clean_drop_rate 告警 ≥3 条 |
| D-11 "王银行"不误剔 | 005 AC-5 陷阱 | **PASS** | 电诈词表 replace 模式下 0 误伤 |
| D-12 画像清洗前后行数 | 008 AC-6 | **PASS** | account.raw_name 112/40、transaction.card 120/6、to_raw 120/40 逐属性可见 |

### 质量扫描组（D-13~D-18）

| 探针 | 验收指向 | 结果 | 备注 |
|---|---|---|---|
| D-13 校验位错身份证逐行落账 | 016 AC-1/7 | **PASS** | 12 条违规（checksum_failed 9 + format_mismatch 3）→ 9 个唯一代理键可下钻（3 行短位双计）；6 个错校验位行全部在账 |
| D-14 12 位非法号段 | 016 AC-2 | **PASS** | format_mismatch 2 行 |
| D-15 负金额 | 016 AC-3 | **PASS** | DE_AMOUNT range.min → range_violation 5 行 |
| D-16 未来日期 | 016 AC-4 | **PASS** | DE_DATE range.max → 2026-12-31×2 检出（**修复：date 值 isoformat 后走字典序比较**，见 §3） |
| D-17 "杀猪盘"代码表外 | 016 AC-5/003 AC-3 | **PASS** | enum_unknown 1 行 |
| D-18 合法小写 x 结尾防误报 | 016 AC-8 陷阱 | **PASS** | 桂文革/彭少芬 0 误报 |

### 复合列组（D-19/D-20）

| 探针 | 验收指向 | 结果 | 备注 |
|---|---|---|---|
| D-19 source_sql 上游拆分路径 | 013 AC-1 | **PASS** | split_part 拆分声明合法，15 行拆出列全非空、拆出身份证 0 违规 |
| D-20 composite 整列降级+关联键负路径 | 013 AC-3/AC-5 | **PASS** | 整列保留 15 行含竖线；composite 作 name_property → 装载硬失败 |

### 治理贯通组（D-21~D-24）

| 探针 | 验收指向 | 结果 | 备注 |
|---|---|---|---|
| D-21 合规违规率进 L5 质量分 | 017 AC-1/3 | **PASS** | compliance_violation(-10)/compliance_violation_high(-20) 两档齐现，与统计扣分可区分，五要素齐备 |
| D-22 数据元推荐 draft-only | 021 AC-1/2/3 | **PASS** | 干净列推荐 DE_IDCARD（high/format）；脏混装列只给拆分提示；推荐后数据元表零变更 |
| D-23 健康度四类质量结果 | 022 AC-1/5 | **PASS** | cast_failed/column_missing/clean_drop_rate/sensitive_suspect/compliance_violation 齐现，source 三方可区分 |
| D-24 enum_space 无人名 | 003 AC-2 | **PASS** | 扫描器零命中 |

## 3. 端到端过程中发现并修复的问题

| # | 问题 | 修复 | 影响 |
|---|---|---|---|
| 1 | "李  强"内部空格不被 strip 归一，D-08 无法合并 | `core/clean_ops.py` 新增 `despace` op（py+SQL 双实现，去首尾/内部/全角空白） | 005 改值语义补全 |
| 2 | 合规扫描 date/timestamp 物化值（date 对象）range 校验因 float 转换失败被**静默跳过**，未来日期漏检 | `core/compliance.py`：date 对象转 ISO 字符串走字典序比较 | 016 AC-4 真实生效 |
| 3 | 银行卡校验位无算法可用 | `core/data_elements.py` 注册 `luhn` 校验和（ISO/IEC 7812-1） | 016 checksum 家族扩展 |
| 4 | **红线 R-1 泄漏**：同名拆簇（needs_review=True）的簇若 confidence=1.0 仍进入 `mapping()` 自动映射，与"同名拆簇一律人工裁决，禁止进自动映射"声明相悖 | `entity_resolution.py` mapping() 增加 `not c.needs_review` 过滤 | R-1 补全；test_entity_redline 同口径回归通过 |
| 5 | `scripts/init_duckdb.py` 只认 default 包冷层 | 多案件包支持：非 default 包从 `data/<包名>/` 挂载 | 新包零改动建冷层 |

数据生成器修正（`scripts/gen_reqd_case.py`）：日期范围越界、身份证 19 位（缺 17 位截断）、`0086` 前缀电话 14 位（剥码后 10 位不合法）——均为测试数据本身问题，不涉内核。

## 4. 全量回归

```
python run_tests.py   # WSL /root/.venvs/inves
```

结果：**93 组全绿**（92 组存量 + 新增 reqdcase；含 e2e），无回归。
（mapping() 红线修复涉 entity_resolution，entityredline/golden/e2e 等组已覆盖验证。）

## 5. 遗留与说明

- **D-04 口径**：案例文档原设计 reject_if → quarantine（010 AC-1/2）；本声明包 quarantine 仅挂 on_cast_error，reject 走 clean_stats 剔行，按文档 N-A 降级为计数核对验收。若需 quarantine 全量覆盖，可在 bindings 声明 `on_cast_error: quarantine` 组合（casterror 组已单测覆盖三态）。
- **D6 项（015/020）**：案例文档编写时 D6 未落地故标 N-A；D6 现已实现（null_policy/dedup_key/data_freshness/unit_consistency 四组单测覆盖），本案件数据集的流水号重复 3 对、金额孤峰 1 行可在后续探针扩充中升格 PASS。
- 数据产物（`data/reqd_case/*.parquet`、`data/ladybug/*.lbug`、`data/samples/*.xlsx`）不入库，夹具 `tests/fixtures/reqd_case.json` 为可复现单一真源。
