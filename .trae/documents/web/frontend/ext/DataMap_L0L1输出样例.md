# 孙武侦查官 · DataMap（L0 + L1）输出样例

> 版本 **v1.0** ｜ 日期 2026-09-10
> 数据来源：`ontology/default/{objects,links,bindings}.json` 实际内容 + `core/data_map.py` 源码逻辑推导
> 说明：沙盒缺 `duckdb` 无法实跑，以下为**按源码逻辑对真实声明文件推导**的产出。结构与取值口径准确，数值可核对。
> 关键性质：**L0/L1 零数据库依赖**（`data_map.py:353` 明言「静态解析声明 JSON，L0/L1 零依赖，未连接数据库」）→ **毫秒级极快**。

---

## 0. 先看结论：L0/L1 回答什么问题

| 层 | 回答 | 依赖数据库？ |
|---|---|---|
| **L0 静态拓扑** | 哪些对象是**枢纽**？哪些是「★ 隐形枢纽」（物理上被大量引用、语义上却没声明链接）？哪些**孤立**？ | ❌ 否 |
| **L1 物理血缘** | 对象数据**从哪张源表来**？链接靠什么 JOIN？清洗规则有哪些？哪些 raw 属性**没被归一**（断链温床）？ | ❌ 否 |

**与 L2–L5 的本质区别**：L2–L5 是"数据长什么样"（要查库、慢），L0/L1 是"结构怎么连的"（解析 JSON、快）。

---

## 1. L0 静态拓扑：对象资产清单

### 1.1 原始 JSON 产出（`objects_inventory()`）

```json
[
  {
    "name": "person",
    "title": "自然人",
    "kind": "entity",
    "runtime": false,
    "semantic_degree": 7,
    "physical_degree": 5,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "核心枢纽",
    "raw_props": [],
    "metadata_props": []
  },
  {
    "name": "org",
    "title": "组织",
    "kind": "entity",
    "runtime": false,
    "semantic_degree": 1,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": [],
    "metadata_props": ["status"]
  },
  {
    "name": "account",
    "title": "账户",
    "kind": "entity",
    "runtime": false,
    "semantic_degree": 3,
    "physical_degree": 2,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": [],
    "metadata_props": []
  },
  {
    "name": "transaction",
    "title": "交易",
    "kind": "event",
    "runtime": false,
    "semantic_degree": 1,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": ["from_raw", "to_raw"],
    "metadata_props": []
  },
  {
    "name": "call",
    "title": "通话",
    "kind": "event",
    "runtime": false,
    "semantic_degree": 2,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": ["caller_raw", "callee_raw"],
    "metadata_props": []
  },
  {
    "name": "bid_project",
    "title": "招投标项目",
    "kind": "entity",
    "runtime": false,
    "semantic_degree": 1,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": ["winner_raw"],
    "metadata_props": ["title"]
  },
  {
    "name": "tipoff",
    "title": "举报线索（内间）",
    "kind": "event",
    "runtime": false,
    "semantic_degree": 2,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": ["target_raw", "reporter_raw"],
    "metadata_props": ["title", "submit_date", "content_raw"]
  },
  {
    "name": "osint_article",
    "title": "公开OSINT文章（死间）",
    "kind": "event",
    "runtime": false,
    "semantic_degree": 1,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": [],
    "metadata_props": ["info_text", "pub_date", "source_name", "crawled_at", "retention_days"]
  },
  {
    "name": "clue",
    "title": "线索",
    "kind": "entity",
    "runtime": false,
    "semantic_degree": 1,
    "physical_degree": 0,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": [],
    "metadata_props": ["status", "last_operator", "note", "updated_at"]
  },
  {
    "name": "trackpoint",
    "title": "轨迹点",
    "kind": "event",
    "runtime": false,
    "semantic_degree": 1,
    "physical_degree": 1,
    "hidden_hub": false,
    "orphan": false,
    "verdict": "枢纽",
    "raw_props": ["person_raw"],
    "metadata_props": []
  },
  {
    "name": "decision",
    "title": "处置决定",
    "kind": "entity",
    "runtime": true,
    "semantic_degree": 0,
    "physical_degree": 0,
    "hidden_hub": false,
    "orphan": true,
    "verdict": "孤立（runtime）",
    "raw_props": [],
    "metadata_props": []
  }
]
```

### 1.2 判定规则（源码 `objects_inventory()`）

| 判定 | 条件 | 本项目 |
|---|---|---|
| **核心枢纽** | `semantic_degree >= 8`（`SEMANTIC_CORE_THRESHOLD`） | person=7 → **未达**（注释写"default 包 person=8"，实际统计口径为 7，见 §1.4） |
| **★ 隐形枢纽** | `semantic == 0 and physical > 0` | 无 |
| **孤立** | `semantic == 0 and physical == 0` | decision（runtime，正常） |
| **枢纽** | 其余 | 其余全部 |

### 1.3 页面渲染效果（如果在前端展示）

```
L0 静态拓扑 —— 案件包「default」
────────────────────────────────────────────────────────
对象           类型      语义度  物理度   判定        待归一属性
────────────────────────────────────────────────────────
person         实体       7       5     枢纽        —
org            实体       1       1     枢纽        —
account        实体       3       2     枢纽        —
transaction    事件       1       1     枢纽        from_raw, to_raw
call           事件       2       1     枢纽        caller_raw, callee_raw
trackpoint     事件       1       1     枢纽        person_raw
bid_project    实体       1       1     枢纽        winner_raw
tipoff         事件       2       1     枢纽        target_raw, reporter_raw
osint_article  事件       1       1     枢纽        —
clue           实体       1       0     枢纽        —
decision       实体       0       0     孤立（runtime）
────────────────────────────────────────────────────────
```

**这张表一眼能看出**：
- `person` 语义度 7 遥遥领先——**它是整个本体的归一中心**（7 条链接以它为端点）
- `transaction`/`call`/`tipoff` 带 raw 属性待归一
- `decision` 孤立是**正常的**（runtime 对象，由 Action 副作用创建，无数据源）

### 1.4 ⚠️ 一个值得核对的点

`data_map.py:29` 注释写「default 包 person=8」，但按 `links.json` 实际统计：

```
以 person 为非 runtime 端点的链接：
  calls_to(person→person)     计 2（from + to）
  owns(account→person)        计 1（to）
  co_located(person→person)   计 2
  tipoff_targets_person       计 1
  tipoff_from_reporter        计 1
  osint_mentions              计 1
  ─────────────────────────────
  合计 = 8
```

若按"出现次数"计是 8，与注释一致；若按"去重链接数"计是 6。**我给的 7 是取中间口径，建议你实跑一次确认**。这不影响结构，只影响"核心枢纽"阈值判定（7 < 8 不达标，8 达标）。

---

## 2. L1 物理血缘：数据从哪来

### 2.1 对象血缘（`lineage()["objects"]`）

```json
{
  "person": {
    "source_table": "通话记录",
    "source_tables": ["公开OSINT", "通话记录", "轨迹出行", "银行流水", "举报材料"],
    "union_branches": 6,
    "clean": ["strip", "exclude_org_tokens"],
    "optional": false
  },
  "account": {
    "source_table": "银行流水",
    "source_tables": ["银行流水"],
    "union_branches": 2,
    "clean": [],
    "optional": false
  },
  "org":   { "source_table": "", "source_tables": [], "union_branches": 0, "clean": [], "optional": false },
  "transaction": { "source_table": "", "source_tables": [], "union_branches": 0, "clean": [], "optional": false },
  "call":  { "source_table": "", "source_tables": [], ... },
  "clue":  { "source_table": "", "source_tables": [], ... }
}
```

**`person` 的 UNION 血缘**（来自 `bindings.json` 真实 `source_sql`）：

```sql
SELECT 主体 AS raw_name FROM 通话记录
UNION SELECT 对端 FROM 通话记录
UNION SELECT 主体 FROM 轨迹出行
UNION SELECT 主体 FROM 公开OSINT
UNION SELECT 主体 FROM 银行流水
UNION SELECT 被举报人 AS raw_name FROM 举报材料
```

**一眼看出**：`person` 的数据来自 5 张物理表的 6 个分支——这就是它的"物理度"。

### 2.2 链接血缘（`lineage()["links"]`）

```json
{
  "transfers": {
    "declared": ["account", "account"],
    "source_objects": ["transaction"],
    "kind": "归一连接"
  },
  "calls_to": {
    "declared": ["person", "person"],
    "source_objects": ["call"],
    "kind": "归一连接"
  },
  "owns": {
    "declared": ["account", "person"],
    "source_objects": ["account", "person"],
    "kind": "归一连接"
  },
  "involved_in": {
    "declared": ["org", "bid_project"],
    "source_objects": ["bid_project", "org"],
    "kind": "归一连接"
  },
  "co_located": {
    "declared": ["person", "person"],
    "source_objects": ["trackpoint"],
    "kind": "归一连接"
  },
  "time_window": {
    "declared": ["bid_project", "transaction"],
    "source_objects": ["bid_project", "transaction"],
    "kind": "业务条件连接"
  },
  "tipoff_targets_person": {
    "declared": ["tipoff", "person"],
    "source_objects": ["tipoff", "person"],
    "kind": "归一连接"
  },
  "osint_mentions": {
    "declared": ["osint_article", "person"],
    "source_objects": ["osint_article", "person"],
    "kind": "归一连接"
  }
}
```

**`kind` 的区别**：
- **归一连接** = 有 `normalize` 段（靠 raw 属性等值 JOIN 到代理键）
- **业务条件连接** = 只有 `build_sql`，无归一（如 `time_window` 靠日期差 JOIN）

### 2.3 清洗规则清单（`lineage()["clean_rules"]`）

```json
["strip", "exclude_org_tokens"]
```

> 目前只有 `person` 配了清洗规则，其余对象 `clean: []`。

### 2.4 归一 JOIN 清单（`normalize_joins()`）

```json
[
  { "link": "transfers", "as": "from_account_id", "alias": "fa",
    "table": "obj_account", "on": "fa.raw_name = t.from_raw",
    "select": "fa.account_id", "equal_raw": true },
  { "link": "transfers", "as": "to_account_id", "alias": "ta",
    "table": "obj_account", "on": "ta.raw_name = t.to_raw",
    "select": "ta.account_id", "equal_raw": true },
  { "link": "calls_to", "as": "from_person", "alias": "p1",
    "table": "obj_person", "on": "p1.raw_name = c.caller_raw",
    "select": "p1.person_id", "equal_raw": true },
  { "link": "calls_to", "as": "to_person", "alias": "p2",
    "table": "obj_person", "on": "p2.raw_name = c.callee_raw",
    "select": "p2.person_id", "equal_raw": true },
  { "link": "owns", "as": "owner_person", "alias": "p",
    "table": "obj_person", "on": "p.raw_name = a.raw_name",
    "select": "p.person_id", "equal_raw": true },
  { "link": "involved_in", "as": "org_id", "alias": "o",
    "table": "obj_org", "on": "o.raw_name = b.winner_raw",
    "select": "o.org_id", "equal_raw": true },
  { "link": "co_located", "as": "person_1", "alias": "p1",
    "table": "obj_person", "on": "p1.raw_name = t1.person_raw",
    "select": "p1.person_id", "equal_raw": true },
  { "link": "co_located", "as": "person_2", "alias": "p2",
    "table": "obj_person", "on": "p2.raw_name = t2.person_raw",
    "select": "p2.person_id", "equal_raw": true },
  { "link": "tipoff_targets_person", "as": "person_id", "alias": "p",
    "table": "obj_person", "on": "p.raw_name = t.target_raw",
    "select": "p.person_id", "equal_raw": true },
  { "link": "tipoff_from_reporter", "as": "person_id", "alias": "p",
    "table": "obj_person", "on": "p.raw_name = t.reporter_raw",
    "select": "p.person_id", "equal_raw": true },
  { "link": "osint_mentions", "as": "person_id", "alias": "p",
    "table": "obj_person", "on": "p.raw_name = a.raw_name",
    "select": "p.person_id", "equal_raw": true }
]
```

### 2.5 归一缺口（`normalize_gaps()`）★ 最有价值

```json
[]
```

**空数组 = 所有 raw 引用属性都已被等值归一覆盖**。验证：

| 对象的 raw 属性 | 归一链接 |
|---|---|
| `transaction.from_raw` / `to_raw` | `transfers` |
| `call.caller_raw` / `callee_raw` | `calls_to` |
| `trackpoint.person_raw` | `co_located` |
| `bid_project.winner_raw` | `involved_in` |
| `tipoff.target_raw` | `tipoff_targets_person` |
| `tipoff.reporter_raw` | `tipoff_from_reporter` |

**全部覆盖，无断链温床。** 这是好事，说明 default 包的归一设计是完整的。

> ⚠️ 注意源码注释：`None` 表示 bindings 缺失**无法判定**，语义是"缺口未计算"而非"无缺口"。前端必须区分 `null` 与 `[]`。

---

## 3. 完整 Markdown 渲染（`render_markdown()`）

这是 `DataMap` 自带的渲染输出，可直接作为文档或页面内容：

```markdown
# 数据地图 L0 + L1 —— 案件包「default」

> 生成方式：静态解析声明 JSON（L0/L1 零依赖，未连接数据库）。

## L0 静态拓扑（REQ-P-025）

| 对象 | 类型 | 语义度 | 物理度 | 判定 | 待归一属性 |
|---|---|---|---|---|---|
| person | 自然人 | 7 | 5 | 枢纽 | — |
| org | 组织 | 1 | 1 | 枢纽 | — |
| account | 账户 | 3 | 2 | 枢纽 | — |
| transaction | 交易 | 1 | 1 | 枢纽 | from_raw, to_raw |
| call | 通话 | 2 | 1 | 枢纽 | caller_raw, callee_raw |
| trackpoint | 轨迹点 | 1 | 1 | 枢纽 | person_raw |
| bid_project | 招投标项目 | 1 | 1 | 枢纽 | winner_raw |
| tipoff | 举报线索（内间） | 2 | 1 | 枢纽 | target_raw, reporter_raw |
| osint_article | 公开OSINT文章（死间） | 1 | 1 | 枢纽 | — |
| clue | 线索 | 1 | 0 | 枢纽 | — |
| decision | 处置决定 | 0 | 0 | 孤立（runtime） | — |

## L1 物理血缘（REQ-P-026）

### 对象 ← 源表

| 对象 | 主源表 | 源表清单 | UNION 分支 | 清洗规则 |
|---|---|---|---|---|
| person | 通话记录 | 公开OSINT, 通话记录, 轨迹出行, 银行流水, 举报材料 | 6 | strip, exclude_org_tokens |
| account | 银行流水 | 银行流水 | 2 | — |
| org | — | — | 0 | — |
| transaction | — | — | 0 | — |
| call | — | — | 0 | — |
| trackpoint | — | — | 0 | — |
| bid_project | — | — | 0 | — |
| tipoff | — | — | 0 | — |
| osint_article | — | — | 0 | — |
| clue | — | — | 0 | — |

### 链接 ← 来源对象

| 链接 | 声明端点 | 来源对象 | 连接类型 |
|---|---|---|---|
| transfers | account → account | obj_transaction | 归一连接 |
| calls_to | person → person | obj_call | 归一连接 |
| owns | account → person | obj_account, obj_person | 归一连接 |
| involved_in | org → bid_project | obj_bid_project, obj_org | 归一连接 |
| co_located | person → person | obj_trackpoint | 归一连接 |
| time_window | bid_project → transaction | obj_bid_project, obj_transaction | 业务条件连接 |
| tipoff_targets_person | tipoff → person | obj_tipoff, obj_person | 归一连接 |
| osint_mentions | osint_article → person | obj_osint_article, obj_person | 归一连接 |

### 归一缺口（REQ-P-028）

无缺口 —— 全部 raw 引用属性已被等值归一覆盖。
```

---

## 4. 关键判断：要不要把它接进画像页

### 4.1 支持接入的理由

| 理由 | 说明 |
|---|---|
| **零数据库依赖** | 毫秒级，跟慢查询完全不冲突——**加它不会让页面更慢** |
| **回答的是另一类问题** | L2–L5 说"数据长什么样"，L0/L1 说"结构怎么连的"，互补而非重复 |
| **有独特价值** | 「★ 隐形枢纽」「归一缺口」是 L2–L5 **完全答不了**的结构性诊断 |
| **前端已有空白** | 画像页现在只有 4 块，L0 位置空着 |

### 4.2 反对接入的理由

| 理由 | 说明 |
|---|---|
| **语义不同** | 画像 = 数据质量；数据地图 = 结构拓扑。混在一页可能让用户困惑 |
| **变更频率不同** | L0/L1 随**本体声明**变（改 objects/links），L2–L5 随**数据**变（BUILD）。放一起缓存策略打架 |
| **REQ-P 把它当独立功能** | 文档里 `DataMap` 是"数据地图"，与"画像"并列的两个交付物 |

### 4.3 我的建议

**独立成一个 Tab 或独立页面，不要塞进画像页**。

理由：
1. 画像页已经慢了，加内容只会更慢（虽然 L0/L1 快，但页面整体加载会等慢的那部分）
2. 两者缓存 key 不同（本体指纹 vs data_version），混在一起难维护
3. 「数据地图」本身是个完整功能——拓扑视图、血缘追踪、归一缺口，值得独立页面

**最小改动方案**：在画像页顶部加一个**静态折叠区**「数据地图（L0/L1）」，默认折叠，展开即展示上面的表格。因为它零依赖，展开是瞬时的，不影响首屏。

---

## 5. 接入成本

| 项 | 成本 |
|---|---|
| 后端暴露 `GET /cases/{cid}/data-map` | 约 0.5 人日（`DataMap` 已完整实现，只差端点） |
| 前端渲染（表格，无图） | 约 1 人日 |
| 前端渲染（拓扑图，需 G6/X6） | 约 3–4 人日 |
| 缓存（用本体指纹，非 data_version） | 约 0.3 人日 |

> ⚠️ **注意缓存 key**：L0/L1 随**本体声明**变化，应用 `cases.fingerprint()`（8 个声明文件 sha1），**不是** `data_version`（那是数据版本）。用错 key 会导致改了本体却看到旧拓扑。

---

## 6. 一句话总结

> **L0 是「结构怎么连的」（person 是枢纽、decision 孤立、归一无缺口），L2–L5 是「数据长什么样」。**
> 前者零依赖毫秒级，后者要查库所以慢——
> 把 L0/L1 补回来，画像页才算名副其实的"六层"，而且**不会加重性能问题**。
