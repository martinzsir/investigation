# demoD 问题归因诊断：为什么 Web 端操作会产出这些缺陷

日期：2026-09-11　对象：`cases/demoD`（Web 端全量操作，8 次上传 + 8 次构建）

---

## 0. 一句话答案

**不是你的操作有问题，是内核里的一条缺陷：语义层编译器把「源表缺失时的降级裁剪」结果就地写回了进程级缓存的声明对象，导致只要有一次构建发生在某张源表还没上传之前，那个分支就被永久除名。** 你后面每次再传、再构建都救不回来，而且 Web 端不会显示任何告警。

demoD 正好命中了这条路径：**银行流水**在 17:48:53 完成导入，而 v6 构建在 17:48:48 就已经启动——早了 5 秒。这一版裁掉了 person 六路 UNION 里的银行流水分支，此后的 v7、v8 永远少这 8 个人。

---

## 1. 从 Web 端还原出来的操作时序（meta/meta.db）

| 时刻 | 源表 | 状态 |
|---|---|---|
| 17:45:23 | 工商信息 | **staged**（未确认列名映射，`table_name` 为空） |
| 17:45:35 | 人员信息 | imported |
| 17:47:53 | 工商信息 | imported（重传后完成） |
| 17:48:06 | 公开OSINT | imported |
| 17:48:25 | 轨迹出行 | imported |
| 17:48:33 | 举报材料 | imported |
| 17:48:43 | 通话记录 | imported |
| **17:48:48** | **BUILD v6 启动** | ← 比银行流水早 5 秒 |
| **17:48:53** | **银行流水** | **imported（这一刻之后才有）** |
| 17:49:04 | BUILD v7 启动 | 源已齐，但结果没变 |
| 17:49:07 | 招投标档案 | imported |
| 17:49:12 | BUILD v8 启动 | 仍没变 |

8 次 `IMPORT → BUILD → QUALITY_CHECK` 全部 `SUCCEEDED`，无一条失败任务。

---

## 2. 缺陷的精确位置

`core/ontology.py:996-1010`

```python
if b.source_sql and re.search(r"\bUNION\b", b.source_sql, re.IGNORECASE):
    pruned, dropped = _prune_union_sql(conn, b.source_sql)
    ...
    if dropped:
        b.source_sql = pruned          # ← 1007 行：就地改写
```

`b` 来自 `spec.object_bindings[otype.name]`，而 `spec` 来自 `core/ontology_loader.load_pack()` 的**进程级缓存**：

```python
_PACK_CACHE: dict[tuple, "OntologyPack"] = {}

def _pack_fingerprint(pack, base_dir) -> float:
    mtimes = [p.stat().st_mtime for p in root.glob("*.json")]   # 只看包 JSON 的 mtime
    return max(mtimes) if mtimes else 0.0
```

两个因素叠加：

1. **指纹不含数据面**：`_pack_fingerprint` 只取 `ontology/default/*.json` 的 mtime，与"库里挂载了哪几张源表"完全无关。案卷包在 v1~v8 期间没动过一个字，指纹恒为 `1789117708.0`，缓存永久命中。
2. **Worker 是常驻进程**：`server/app/worker/pool.py` 起 2 个守护线程常驻轮询，`_PACK_CACHE` 在整个服务生命周期内复用。

于是「一次裁剪」变成「永久裁剪」。

---

## 3. 受控复现（单进程内模拟 Web 时序）

```
[S0] 全新进程：person.source_sql = 六路 UNION（含银行流水）
[S1] 挂 7 张表（缺银行流水）→ BUILD
     obj_person = 19 行 | UNION 分支 = 5 | **已丢失银行流水分支**
     degraded = ["obj_person 部分源表未导入，UNION 分支已裁剪: ['银行流水']"]
[S2] 补齐银行流水 → 再次 BUILD（同进程，未重启）
     obj_person = 19 行 | UNION 分支 = 5 | **已丢失银行流水分支**
     degraded = []            ← 内核认为"无需裁剪"，无任何告警
     缓存里的 source_sql = 五路版本
[S3] invalidate_pack_cache() 后同一张库再 BUILD
     obj_person = 27 行 | UNION 分支 = 6 | 含银行流水分支
```

**S1=19 → S2=19 → 清缓存后 S3=27**：源表补齐无用，只有清进程缓存才恢复。缺陷确认。

案件自身的旁证完全一致：

- `build_stats_v6.json` 有 `degraded: ["obj_person 部分源表未导入，UNION 分支已裁剪: ['银行流水']"]`
- `build_stats_v7.json` / `v8.json` 的 `degraded` 是**空**——不是因为修好了，而是因为 SQL 已经是裁剪后的版本，内核不再认为需要裁剪
- 版本时钟里 person 的内容哈希从 v6 起三版恒定 `b647072b44f2`

---

## 4. 为什么你在 Web 端完全看不到

| 环节 | 表现 |
|---|---|
| BUILD 任务 | `SUCCEEDED`，进度 100%，构建本身确实成功 |
| QUALITY_CHECK | `SUCCEEDED`（它查的是数据元合规，不查语义层覆盖率） |
| `stats["degraded"]` | 只落到 `artifacts/build_stats_vN.json` 文件；**`server/` 下没有任何代码消费它**——不进 UI、不进 `run_diagnostic`、不进健康度。只有 CLI `run_all.py` 和 `scripts/build_ontology.py` 会打印 |
| `record_build_degraded()` | 仅在 `run_all.py` 里被调用，Web 链路不调用 |
| obj_person = 19 | 没有基线对照，看不出少了 8 个人 |

结论：**这条降级路径在设计上是"静默"的。** 只要走 CLI 就能看见，走 Web 端就没有出口。

---

## 5. 其余四项的归因

| # | 问题 | 成因 | 性质 |
|---|---|---|---|
| 2 | person 取数单边、account 双边 | `bindings.json` 里 person 声明 `SELECT 主体 FROM 银行流水`（单边），account 声明 `主体 UNION 对方`（双边）。只在"对方"列出现的戴若曦（14 行）永远进不了人名单，其账户也就永远没有归属边 | 包声明设计不对称，非链路缺陷；但与缺陷 1 叠加后更难察觉 |
| 3 | 空名实体 `person_da39a3ee5e6b` 承接 7 条边 | 冷层 `通话记录.parquet` 里**源数据本身是空串**而非 NULL（主体='' 1 行、对端='' 1 行，NULL 行数为 0）。`strip` 清洗后仍是 `''`；内核注释明确"空串是确定值，保留既有语义不在此剔除"，于是产生了一个名字为空串的实体 | 语义政策与夹具期望不符（fixture 注释写的是"NULL 无身份、INNER 丢边"），需要二选一：要么在源侧把空串归一化为 NULL、要么同步修订 fixture 注释 |
| 4 | 2099 未来日期被判"数据新鲜" | `core/data_freshness.py` 只有 `age > stale_days` 一个判据，`age = -26410` 通过。4 张表各 1 行未来日期（银行流水.日期 / 轨迹出行.日期 / 公开OSINT.发布日期 / 举报材料.举报日期） | 缺负值判据，代码缺口 |
| 5 | ~~audit_chain 的 rule_version 全 NULL~~ **（已复核：不是缺陷）** | 逐条核对 demoD `audit_chain` 的 17 条记录：seq1 = case_created，seq2/4/6…16 = source_imported（8 次上传），seq3/5/7…17 = build_succeeded（8 次构建）。**全部是生命周期事件**，本来就不属于任何规则，`rule_version` 为 NULL 是正确语义。上一版报告把它归因为「core/rules.py 未传版本」是我推断错了，实际 `core/rules.py` 并不写审计——这条撤回。 | 无需修复 |

> 顺带一项正面结论：17 条里没有任何处置/决策/配置写事件，与体检结果「9 条线索全部待查、决策/处置/写回表全空」互相印证——demoD 至今只做过接入与构建，未越权写过任何处置结论。

---

## 6. 修复落地（已实施）

| 级别 | 改动 | 位置 |
|---|---|---|
| P0-1 | 当次 effective SQL 改走局部变量 `src_sql`，**不再写回** binding；`UNION` 裁剪与「可选列缺失重渲染」两条降级路径都已隔离 | `core/ontology.py:1018/1040/1061/1103` |
| P0-1b | 新增 `_localize_spec()`：build/materialize 入口把 bindings 换成本次副本，构成第二道防线（将来再有人写回 `b.xxx` 也只影响当次） | `core/ontology.py:393`、接入 `build_ontology` 与 `materialize_changed` |
| P0 回归锁 | 新增 `tests/test_spec_pollution.py`（4 例），并注册测试组 `pollution` | `tests/test_spec_pollution.py`、`run_tests.py` |
| P1 | 构建降级/跳过不再静默：degraded 按内容分口径落诊断（UNION 裁剪→`source_branch_pruned`，可选列缺失→`source_column_missing`），skipped 落 `object_skipped`；Worker 在 BUILD 后统一写 `run_diagnostic` + `ops_events(build_degraded)`，并把 warnings 带进返回体 | `core/run_health.py`、`server/app/worker/tasks.py` |
| P2 | 数据新鲜度增加未来日期判据（负账龄超容差 → `data_freshness_future` 告警，不再被判"新鲜"） | `core/data_freshness.py`、`tests/test_data_freshness.py`（AC-6 两例） |

**暂不动**（涉及语义政策，改了会改变存量案件的图结构，建议单独立项）：
person 单边/account 双边口径、空名实体是否剔除。这两项需要在「实体补全 vs 误链风险」之间做取舍，不该混在这次缺陷修复里改。

### 回归锁的有效性已验证

回退到修复前的写法（两条路径都写回 binding + 取消 `_localize_spec`）后，4 个新用例**全红**，失败点正是缺陷语义：

- `['张三'] != ['张三','赵六']` —— 补传源表后 person 不恢复（demoD 症状的最小复现）
- 缓存里的 `source_sql` 被改写成单分支
- 可选列补齐后仍是 `None`（NULL 投影被固化）

恢复修复后 4 例全绿。

### 端到端效果

在同一份 `v8.duckdb` 的副本上用修复后的编译器重跑一次 BUILD（无需重启任何进程、无需清缓存）：

| | 修复前 | 修复后重跑一次 |
|---|---|---|
| `obj_person` | 19 行 | **27 行** |
| `lnk_owns` | 9 条 | **17 条** |

补回来的正是此前缺的 8 人：华清越、张卫国（含零宽空格）、李志强（宏业法人）、樊皓宁、测试😀表情、王𠀀、郁晨曦、鲁以墨；无人员消失，其余对象行数不变。
即：**源表本就在库里，只要编译器不再把旧裁剪带进这次构建，结果立刻正确。**

---

## 7. 原始建议（已按上表落地，本节存档）

**立即止血**：重启 Worker 进程后对 demoD 再跑一次 BUILD，`obj_person` 会立刻从 19 恢复到 27，`lnk_owns` 补齐 8 条边。（不重启的话，Web 端重复多少次构建都没用。修复后此项不再需要，但仍建议重启一次 Worker 让新代码生效。）

**根治（P0，改一行）**：`core/ontology.py:1007` 不要把裁剪结果写回 binding，改为本次编译的局部变量：

```python
src_sql = b.source_sql                      # 本次有效 SQL（不写回 b）
if dropped:
    src_sql = pruned
...
q = f"SELECT {otype.name_property}, * EXCLUDE (...) FROM ({src_sql})"
```

同一处的同类写法 `core/ontology.py:1037`（`b.source_sql = _rerender_source_sql(...)`，可选源列缺失时改写）应一并改掉——它存在完全相同的污染路径。

**加固（P1）**：

1. `build_ontology` 入口对 `spec` 的 bindings 做浅拷贝，或在 `load_pack` 返回后冻结不可写，从机制上杜绝编译期副作用污染缓存。
2. Web 端消费 `stats["degraded"]` / `stats["skipped"]`：`default_builder` 里把它们提升为任务级告警，落 `ops_events` + `run_diagnostic`，并在 Build 结果面板显示。**否则同类问题下次依旧静默。**
3. 给多源 UNION binding 增加"声明分支数 vs 实际参与分支数"的自检并纳入健康度。

**顺带（P2）**：person 是否改为双边取数（与 account 对齐）、空名实体是否符合预期、`data_freshness` 增加未来日期判据。
（其中「未来日期判据」已落地；其余两项见第 6 节「暂不动」。原报告中「规则命中时回填规则版本号」一条已撤销，见第 5 节第 5 项。）

---

## 附：本轮取证方式

全部只读，重量验证在临时内存库与 `cases/demoD/ontology` 快照上进行，原案件未改动：

- `.workbuddy/_diag_root_cause.py` —— 单进程三阶段复现（本次给出 S1/S2/S3 的 19/19/27）
- `.workbuddy/_diag_root_cause2.py` —— 冷层空值形态与未来日期分布
- `.workbuddy/_diag_root_cause3.py` —— meta.db 还原 Web 操作时序
