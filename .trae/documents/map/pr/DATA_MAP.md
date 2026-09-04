# 数据地图 L0 + L1 —— 实施交付

> 状态：**162 项验收全绿**（数据地图 28 + 画像 42 + 实体连接 91 + 2 skip）
> 依据：仓库 `ontology_v2` 真实 `bindings.json`（已 fetch 完整版，5239B）
> 依赖：**零**（L0+L1 不连数据库、不依赖 duckdb）

---

## 一、先回答先前挂起的两个疑问

### 疑问 1：正则 `obj_\w+` 够不够 → ✅ 够

8 条边全部提取成功，**无 CTE、无动态表名**。已有 AC 固化这个前提
（`test_ac6_正则无CTE无动态表名`）——将来若引入 CTE，测试会失败提醒重估。

### 疑问 2：归一逻辑是否内嵌在 build_sql → ⚠️ 是，且挖出真问题

```
calls_to              JOIN obj_person p1 ON p1.raw_name = c.caller_raw
owns                  JOIN obj_person   p ON p.raw_name = a.raw_name
involved_in           JOIN obj_org      o ON o.raw_name = b.winner_raw
osint_mentions        JOIN obj_person   p ON p.raw_name = a.raw_name
tipoff_targets_person JOIN obj_person   p ON p.raw_name = t.target_raw
co_located            JOIN obj_person   p ON p.raw_name = t1.person_raw
```

**全部是 `raw_name = xxx_raw` 的硬编码等值 JOIN** ——
发现新别名（`ZhouMingyuan ↔ 周明远`）要改 SQL 而非改配置，
与「声明是数据」的信条冲突。

---

## 二、⚠ 最重要的发现：transfers 是断链

```
transfers   links.json   from_obj=account  to_obj=account
            build_sql    SELECT txn_id, from_raw AS from_account,
                                        to_raw AS to_account, amount, date
                         FROM obj_transaction
            ⚠ 无任何 JOIN obj_account
```

`from_account` 装的是 **raw 值**（如 `6222000111110001`），
而不是 `account_id`。**资金链条根本连不起来。**

对比其余 6 条边都有归一 JOIN，唯独 `transfers` 没有——
而它恰恰是唯一的 `account→account` 边。

这直接印证了实体连接的定位：

> `account.raw_name ⊇ transaction.from_raw` 这条归一建议，
> **正是 transfers 边所缺失的那一环**。

（另有 `time_window` 也无归一 JOIN，但它的端点是
`bid_project→transaction`，靠时间窗条件连接，不依赖归一，故不算断链。）

---

## 三、L0 静态拓扑产出

| 对象 | 语义度 | 物理度 | 判定 |
|---|---|---|---|
| person | 7 | 5 | 核心枢纽 |
| account | 3 | 1 | 枢纽 |
| bid_project | 2 | 2 | 枢纽 |
| transaction | 1 | 2 | 枢纽 |
| **call** | **0** | **1** | **★ 隐形枢纽** |
| **trackpoint** | **0** | **1** | **★ 隐形枢纽** |
| clue | 0 | 0 | 孤立 |
| decision | 0 | 0 | 孤立（runtime） |

**隐形枢纽**：语义度 0 却在 `links.json` 里看不见、却真实支撑着边。
只看 `links.json` 画拓扑图会误判它们不重要。

---

## 四、L1 物理血缘产出

对象 ← 业务表（含 `person` 的 UNION 六源）、边 ← 物理来源对象、
归一 JOIN 清单、断链清单。

### 归一缺口（真正该做的三件事）

| 属性 | 说明 |
|---|---|
| `transaction.from_raw` | transfers 断链的根源 |
| `transaction.to_raw` | 同上 |
| `tipoff.reporter_raw` | 被举报人归一了，**举报人没有** |

⚠ **判据修正**：最初我用「links.json 里该对象无端点」判定缺口，
结果误报了 `call.caller_raw` / `callee_raw` / `trackpoint.person_raw`——
它们在 links.json 里确实没有端点，但 **build_sql 已经归一了**。

正确判据必须看 L1（build_sql 是否已归一）。已加 AC 固化这个回归
（`test_ac12_已归一的不是缺口`）。

---

## 五、实施中修掉的四个缺陷

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | 调用顺序错：物理度在 `_parse_link_sql` 累加，但 `_find_orphans` 先执行 → 隐形枢纽全丢 | 调整顺序 |
| 2 | 归一审定据错：用「一端 `_raw` 另一端非 `_raw`」→ `owns`/`osint_mentions` 两侧都是 `raw_name`，被误判为业务条件 | 改为「两侧属性名都含 raw」 |
| 3 | 定向规则错：按 EQUI 左右定向 → 方向反了 | 改为「**JOIN 的表是 target**，另一侧是 source」，实测 8 条边全对 |
| 4 | 归一缺口判据错（见上） | 改看 build_sql |

### 一个已知行为（已固化，未"修"）

`TABLE_ALIAS_RE` 会把 SQL 关键字 `JOIN` 抓成别名：

```sql
FROM obj_transaction JOIN obj_person p ON ...
                     ^^^^ 被抓为 obj_transaction 的别名
```

过滤在 `_alias_map` 层做，不在正则层。
`test_ac2b` 专门固化这个行为，避免后人误以为正则本身安全。

---

## 六、验收 28 项

| 组 | 覆盖 |
|---|---|
| L0 拓扑 | 资产清单、待归一属性、语义度、optional、runtime |
| L1 血缘 | 物理来源、**对象来源表（含 UNION 多源）**、清洗规则、**归一 JOIN 定向**、raw_name↔raw_name 判定、**业务条件不算归一**、**隐形枢纽**、完全孤立、**断链检测**、归一缺口、**已归一不是缺口**、内容字段排除 |
| 解析鲁棒 | 无别名表、**关键字不误当别名**、bindings 缺失不崩溃、无 JOIN 不报错、mini 声明独立解析、正则无 CTE |
| 渲染 | Markdown、Mermaid、**只观察不写回** |

```bash
python -m unittest tests.test_data_map      # 28 项，零依赖
```

---

## 七、设计取舍：无数据时给什么结论

`bindings.json` 缺失时，所有 `_raw` 属性都"未被归一"——
全报缺口在技术上成立，但**等于给假结论**。

选择：**不判定**，并在 `notes` 里声明未知，而非静默输出空列表。

```
无 link_bindings 数据，无法判定归一状态 —— 归一缺口未计算（不是「无缺口」）
```

理由：数据地图要诚实反映已知。不知就说不知。

---

## 八、遗留

1. **`transfers` 断链待修复** —— 需要给 `transfers` 的 build_sql
   补上 `JOIN obj_account`。这是数据地图发现的、最该优先处理的真问题。
2. **归一逻辑硬编码** —— 建议把 `raw_name = xxx_raw` 抽成声明式映射
   （如 `bindings.json` 加 `normalize` 段），发现新别名改配置而非改 SQL。
3. **`content_raw` 用清单排除** —— 按"声明是数据"的信条，
   应在 `objects.json` 加 `entity_ref: false` 标记。
4. **L2-L4 未做** —— 但画像已覆盖大部分 L2（物化/行数/NDV/空值率）；
   L4 能力层（五间覆盖度）已在画像里实现。

---

⚠ **红线**：数据地图只观察不写回；结论均为【待核实】，不构成办案指导。
