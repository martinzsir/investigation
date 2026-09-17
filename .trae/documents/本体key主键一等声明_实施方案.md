# 本体 key（主键）一等声明 —— 实施方案与影响分析

> 目标：参考 Palantir，为 Object Type 增加显式 `key` 声明。
> 本文所有数字均来自 `impl_v4` 实跑，非估算。

---

## 0. 结论摘要

**加 `key` 是对的，但当前不能叫 `key`** —— 这个名字在本体里已被占用三次。方案的核心不在"加字段"，在于**把三处隐式推断变成显式声明**：

| 现在隐式推断 | 显式声明后 |
|---|---|
| `pk.split("_")[0]` 反解代理键前缀 | `key.prefix` |
| `kind==event` / `pk==name_property` 分支判断策略 | `key.strategy` |
| 主键唯一性靠"算法保证"，无断言 | 装载期断言 + DDL 约束 |

**成本**：4.4 人日（P0 2.0 + P1 2.4），零行为变化（严格保持现有键值）。

---

## 1. 现状实测

### 1.1 对象规模

| 包 | 对象数 |
|---|---|
| `ontology/default/objects.json` | 12 |
| `ontology/reqd_case/objects.json` | 8 |
| **合计** | **20** |

### 1.2 「key」这个名字已被占用四处

这是本方案第一件要处理的事。实测：

| # | 位置 | 含义 | 使用面 |
|---|---|---|---|
| 1 | `bindings.json` → `object_bindings[].key` | **业务键（去重键）** REQ-D-015 | **两个包均 0 处使用** |
| 2 | `links.json` → `endpoints.*.ref.key` | 引用对象的主键列名 | 11 条 link 大量使用 |
| 3 | `properties[].composite` | 复合**列**（脏数据降级）REQ-D-013 | 有使用 |
| 4 | Palantir primary key | **本次要加的** | — |

第 1 处是同名不同义的直接冲突；第 2 处是同名同义但分散声明（后面会讲如何联动）；第 3 处是第五次撞名（views.json / lens.json views / Palantir Object Views / 业务键 + 复合列）。

**好消息**：业务键实测两个包 **0 处使用**，改名成本几乎为零。**现在不改名，等它有了存量数据就改不动了。**

### 1.3 三套键，pk 一个字段扛三种语义

| 键 | 位置 | 语义 |
|---|---|---|
| `pk` | objects.json | 主键列名（隐式列，不得在 properties） |
| `name_property` | objects.json | 身份/展示列 |
| `key` | bindings.json | 业务键（去重） |

---

## 2. 五个核心发现（实测）

### 🔴 发现 1：代理键前缀靠 `pk.split("_")[0]` 反解

`core/ontology.py:1128`：

```python
prefix = otype.pk.split("_")[0]
if otype.kind == "event":        keys = _event_proxy_keys(rows, prefix, ...)
elif otype.pk == otype.name_property:  keys = [r[0] for r in rows]   # 自然键直通
else:                            keys = [proxy[r[0]] for r in rows] # 实体代理键
```

**实测 12 个对象的前缀一致性：**

```
对象名            pk             split("_")[0]   一致？
person           person_id      person          OK
org              org_id         org             OK
account          account_id     account         OK
person_identity  identity_id    identity        ❌
transaction      txn_id         txn             ❌
call             call_id        call            OK
trackpoint       track_id       track           ❌
bid_project      project_id     project         ❌
clue             clue_id        clue            OK
decision         decision_id    decision        OK
tipoff           tipoff_id      tipoff          OK
osint_article    article_id     article         ❌
```

**12 个里 5 个前缀与对象名不一致** —— 说明前缀本就是自由命名，与对象名无关。

**真正的隐患**：改 pk 名会静默改变全库主键。

实测同一行数据：

```
pk=txn_id  → prefix=txn   → txn_3028c227e5e4
pk=id      → prefix=id    → id_3028c227e5e4     ← 同一个事件，键变了
```

`pk` 看起来只是"列名"这种显示层配置，**改它却会让语义层所有主键失效**。这正是 Palantir 那条铁律 *Never infer a property from an object's ID* 要防的事。

### 🔴 发现 2：前后端对 pk 的校验完全相反

| 端 | 代码 | 规则 |
|---|---|---|
| 后端 | `core/ontology_loader.py:1147` | `if o["pk"] in props: raise` —— **pk 不得在 properties**（隐式列） |
| 前端 | `frontend/.../OntologyModelerView.vue:656` | `if (!props.includes(o.pk))` 报错 —— **pk 必须在 properties** |

而实测 20 个对象的 pk **全部不在 properties 中**。也就是说前端这条校验**恒报错**，在 `OntologyModelerView` 里编辑任何对象都会卡住。

本次必须一并订正（否则加了 key 之后两处口径更乱）。

### 🟡 发现 3：主键无任何唯一性保障

建表三处（`ontology.py:535 / 650 / 1203`）：

```python
defs = [f'"{otype.pk}" VARCHAR']   # 无 PRIMARY KEY、无 UNIQUE
```

唯一性目前**只由算法保证**：
- 实体型：`_proxy_keys` 按 name 去重后哈希 → 唯一
- 事件型：`_event_proxy_keys` 同内容加 `_02/_03` 后缀 → 唯一
- 自然键：直通，**完全不校验**

自然键那条是敞口的：`clue`/`decision` 的 pk 直接取源列，源表若重复，**语义层就出现重复主键，且无人发现**。

### 🟡 发现 4：`endpoints.ref.key` 与 objects 主键是两处独立声明

实测 `links.json`：

```json
"from": {"col": "from_account_id",
         "ref": {"object": "account", "key": "account_id", "name": "raw_name"}}
```

`ref.key` 手工写着 `account_id` —— 与 `objects.json` 里 account 的 `pk` 是**同一事实的两份声明，无任何一致性校验**。

改了 objects 的 pk，`ref.key` 不会跟着变，也不会报错。这解释了上一轮发现的 `owns` 端点错位（`to.ref.object=account` 但 `to_obj=person`）。

**加 `key` 后，这里可以从"手工写列名"变成"装载期校验一致性"**——不用改结构，只需加校验。

### 🟢 发现 5：代理键是确定性哈希，实质符合 Palantir

```python
f"{prefix}_{sha1(str(n)).hexdigest()[:12]}"
```

Palantir 禁止 runtime uuid（重建会变），要求主键稳定可复现。我们的代理键**重建后完全一致**，这一点实质符合——**缺的只是"这是代理键"没被声明出来**。

---

## 3. 设计方案

### 3.1 新增声明

`objects.json` 每个对象加：

```json
"key": {
  "column":   "person_id",     // 主键列名（= 现有 pk）
  "type":     "string",        // Palantir：主键必须 string
  "strategy": "proxy",         // proxy | natural | composite
  "prefix":   "person",        // 仅 proxy 需要
  "properties": ["raw_name"],  // 键由哪些属性构造（自解释）
  "separator": "|"             // 仅 composite 需要
}
```

### 3.2 三种策略对照现有分支

| strategy | 语义 | 现在由什么推断 | 键的来源 |
|---|---|---|---|
| `proxy` | 哈希代理键 | `kind==event` 或 else 分支 | `prefix + sha1(...)` |
| `natural` | 自然键直通 | `pk == name_property` | 源列原值 |
| `composite` | 多属性拼接（**不 hash**） | 不支持 | `col1\|col2` |

`composite` 是 Palantir 明确推荐的复合主键做法（拼字符串，不做 hash），**与现有 `properties[].composite`（脏数据复合列）完全不同层级**——文档需明确区分，这是第五次撞名。

### 3.3 `pk` 的处置：降为只读别名，不是双写

`key` 是唯一权威源。`pk` 保留为简写别名，装载时：

```
key.column 存在  → 以 key 为准；若同时写了 pk 且不等 → 硬失败
key 缺失        → 回落到 pk（向后兼容），按现有分支推断并告警
```

**为什么不删 pk**：20 个对象 + 十几处代码引用 + 前端契约，一次性删成本高。
**为什么不是双写源**：`pk` 不参与决策，冲突即失败——这跟"两处都能改"的双写有本质区别。我们在前端 store 硬编码五间权重（store 写 1、本体写 4）上吃过双写的亏，不会重蹈。

### 3.4 命名冲突处置

| 冲突 | 处置 |
|---|---|
| `bindings.key`（业务键） | 改名 `business_key`（实测 0 处使用，零破坏） |
| `endpoints.ref.key` | **保留**（它就是"引用对象的主键列名"，语义一致），改为装载期校验与 `objects.key.column` 一致 |
| `properties[].composite` | **保留**（不同层级），文档区分 |

---

## 4. 完整举例

### 例 1：`person`（proxy 实体型，最常见）

**改造前**

```json
{
  "name": "person",
  "pk": "person_id",
  "kind": "entity",
  "name_property": "raw_name",
  "properties": {"raw_name": "string", "id_card": "string"}
}
```

**改造后**

```json
{
  "name": "person",
  "pk": "person_id",
  "key": {
    "column": "person_id",
    "type": "string",
    "strategy": "proxy",
    "prefix": "person",
    "properties": ["raw_name"]
  },
  "kind": "entity",
  "name_property": "raw_name",
  "properties": {"raw_name": "string", "id_card": "string"}
}
```

**装载行为（不变）**

```
源行: raw_name="李志强"
→ proxy = person_<sha1("李志强")[:12]> = person_3f2a9c8b1e04
```

**建表 DDL 变化**

```diff
- CREATE TABLE obj_person ("person_id" VARCHAR, "raw_name" VARCHAR, ...)
+ CREATE TABLE obj_person ("person_id" VARCHAR PRIMARY KEY, "raw_name" VARCHAR, ...)
```

**endpoints 引用（不变，但开始被校验）**

```json
{"object": "person", "key": "person_id", "name": "raw_name"}
                     ↑ 装载期断言 == objects[person].key.column
```

---

### 例 2：`transaction`（proxy 事件型）

**改造后**

```json
{
  "name": "transaction",
  "pk": "txn_id",
  "key": {
    "column": "txn_id",
    "type": "string",
    "strategy": "proxy",
    "prefix": "txn",              // ← 注意：不等于对象名，原样保留现有值
    "properties": ["from_raw", "to_raw", "amount", "date"]
  },
  "kind": "event",
  "name_property": "from_raw",
  "properties": {...}
}
```

**关键点**：`prefix` 是 `txn` 而非 `transaction`。这正是发现 1 的价值——**不显式声明，没人知道前缀是 `txn`，也没人知道改 pk 会毁掉它**。

**装载行为（不变）**

```
行: ["宏业建设","A建材",100,"2024-03-31"]
→ txn_<sha1(行内容)[:12]> = txn_3028c227e5e4
同内容第二行 → txn_3028c227e5e4_02
```

---

### 例 3：`clue`（natural 自然键 —— 唯一性敞口所在）

**改造后**

```json
{
  "name": "clue",
  "pk": "clue_id",
  "key": {
    "column": "clue_id",
    "type": "string",
    "strategy": "natural"
  },
  "kind": "entity",
  "name_property": "clue_id",
  "properties": {"status": "string", "note": "string", ...}
}
```

**装载行为**：`keys = [r[0] for r in rows]` —— 源列直通，**不加工**。

**这是新增唯一性断言的最大受益者**：源表若给两行相同 `clue_id`，现在静默写入重复主键；改造后装载期硬失败并报出重复值。

`decision` 同此例（且带 `runtime: true`）。

---

### 例 4：`case_ledger`（composite 复合主键 —— 新增能力）

reqd_case 现有定义：`pk=case_id`, `name_property=case_no`。

假设业务要求"同一案件编号在不同年度是不同台账"，用复合主键而非 hash：

**改造后**

```json
{
  "name": "case_ledger",
  "pk": "case_id",
  "key": {
    "column": "case_id",
    "type": "string",
    "strategy": "composite",
    "columns": ["case_no", "year"],
    "separator": "|"
  },
  "kind": "entity",
  "name_property": "case_no",
  "properties": {"case_no": "string", "year": "integer", ...}
}
```

**装载行为**

```
case_no="2024-刑-001", year=2024  → case_id = "2024-刑-001|2024"
case_no="2024-刑-001", year=2025  → case_id = "2024-刑-001|2025"
```

**对照 hash 做法**：`case_<sha1("2024-刑-001|2024")[:12]>` = `case_a1b2c3d4e5f6` —— 键不可读、不可推导、无法与外部系统对账。

**这正是 Palantir 说复合 id 不该 hash 的理由**：键要能被人读、能被外部系统构造。

> ⚠️ 当前 20 个对象全是单列主键，**composite 无实际需求**。它的价值不在"能用"，在**堵住"用 hash 拼复合 id"这条歪路**——项目里 hash 是默认手法，且不声明就没人拦得住。

---

### 例 5：`person_identity`（prefix 与对象名不一致）

实测 `pk=identity_id`，`split("_")[0]=identity`，而对象名是 `person_identity`。

**改造后**

```json
"key": {
  "column": "identity_id",
  "type": "string",
  "strategy": "proxy",
  "prefix": "identity"     // ← 显式固化，不再是"意外正确"
}
```

**迁移期必须逐对象固化现有前缀**，否则这 5 个不一致的对象键值会变。

---

### 例 6：links 端点一致性校验（承接上一轮）

以 `owns`（上一轮实测错位）为例：

```json
{
  "name": "owns",
  "from_obj": "account",
  "to_obj": "person",
  "endpoints": {
    "from": {"col": "owner_raw"},                                    // ← 人名，错位
    "to":   {"col": "account_id", "ref": {"object": "account", ...}} // ← 应为 person
  }
}
```

加了 objects.key 后，装载期可新增两条断言：

```
① endpoints.to.ref.object == to_obj
   owns: ref.object=account 但 to_obj=person  → 硬失败
② endpoints.*.ref.key == objects[ref.object].key.column
   校验引用列名与对象主键声明一致
```

这两条就是上一轮 P0-1/P0-2，现在有了 `key` 才真正有地方挂。

---

## 5. 影响分析

### 5.1 代码改动点

| 文件 | 位置 | 改动 | 风险 |
|---|---|---|---|
| `core/ontology_loader.py` | 1147 附近 | 新增 `key` 解析 + 校验 | 低（新增） |
| `core/ontology.py` | 1128 | `pk.split("_")[0]` → `otype.key_prefix` | **中**（核心路径） |
| `core/ontology.py` | 1135 | `pk == name_property` → `strategy == "natural"` | 中 |
| `core/ontology.py` | 535 / 650 / 1203 | DDL 加 `PRIMARY KEY` | **中高**（见 5.4） |
| `core/ontology_loader.py` | 1745 | `b.get("key")` → `b.get("business_key")` | 低（0 处使用） |
| `server/app/routers/etl.py` | 312 | `b.get("key")` → `business_key` | 低 |
| `frontend/.../OntologyModelerView.vue` | 655-656 | 订正 pk 校验（与后端对齐） | 低 |
| `frontend/.../ObjectCentricPanel.vue` | 277 | 展示 key 三态 | 低 |
| `schemas/objects.schema.json` | items | 加 `key` 定义 | 低 |

### 5.2 数据迁移（20 个对象）

全部需补 `key` 声明，**按策略分布**：

| strategy | 数量 | 对象 |
|---|---|---|
| `proxy` | 18 | 除 clue/decision 外全部 |
| `natural` | 2 | clue、decision |

**其中 5 个 prefix 与对象名不一致，必须逐对象固化**：
`person_identity→identity`、`transaction→txn`、`trackpoint→track`、`bid_project→project`、`osint_article→article`

> 这 5 个是迁移期最容易出错的点。若照"prefix = 对象名"想当然生成，键值全变。

### 5.3 测试影响

| 测试 | 影响 |
|---|---|
| `tests/test_ontology.py` | 主键策略测试（127/133/138 行），**策略外显后应重写为按 strategy 断言** |
| `tests/test_dedup_key.py` | `key` → `business_key` 改名同步 |
| `tests/test_m6_govern.py` | 148/155/161 行引用 `dedup_key` 字段，同步 |

**无测试会因行为变化而失败** —— 前提是 prefix 严格固化现有值。

### 5.4 快照与指纹

- `objects.json` 在 22 个配置文件内 → 改了进配置版本审计 ✅
- 进 `_FINGERPRINT_FILES` → 指纹变化，已有提案会提示重评 ✅
- **案件快照**：存量案件的 `objects.json` 需同步补 key（或依赖向后兼容回落 pk）
- **已生成的语义层主键不变** —— prefix 固化即保证

### 5.5 前端

| 组件 | 改动 |
|---|---|
| `ObjectCentricPanel` | 头部 tag 从 `pk person_id` 改为展示策略徽标：`pk person_id · 代理键(前缀 person)` |
| `OntologyModelerView` | 订正 656 行校验；必要时加 key 编辑 |
| `ontologyObject.ts` | 契约补 `key` 字段 |

---

## 6. 分阶段实施

| 阶段 | 项 | 人日 | 说明 |
|---|---|---|---|
| **P0-1** | `key` 一等声明 + schema + 装载校验 | 0.8 | 地基 |
| **P0-2** | 消除 `pk.split("_")[0]`，改读 `key.prefix` | 0.5 | 消除隐式推断 |
| **P0-3** | 主键唯一性：装载期断言 + DDL 约束 | 0.4 | 补敞口 |
| **P0-4** | `bindings.key` → `business_key` | 0.3 | 腾名字（0 处使用，最省） |
| P1-5 | `composite` 复合主键 | 0.8 | 新增能力 |
| P1-6 | endpoints 与 key 联动校验 | 0.6 | 承接上一轮 P0-1/P0-2 |
| P1-7 | 前端：展示 key 三态 + 订正 pk 校验 | 0.3 | |
| P1-8 | 数据迁移：20 个对象补 key | 0.7 | 含 5 个不一致前缀固化 |
| | **合计** | **4.4** | P0 = 2.0，P1 = 2.4 |

**建议顺序**：P0-4 → P0-1 → P0-2 → P1-8 → P0-3 → P1-7 → P1-6 → P1-5

理由：**先把名字腾干净**（P0-4 最省且 0 破坏），再把 `key` 立起来（P0-1），**数据和代码必须同批**（P1-8 与 P0-2 一起上，否则装载不起来）。

---

## 7. 风险与回滚

| 风险 | 等级 | 应对 |
|---|---|---|
| prefix 生成错 → 全库主键变 | 🔴 高 | 迁移脚本逐对象硬编码现有前缀；加断言比对迁移前后键值 |
| DDL 加 PRIMARY KEY 后存量脏数据装载失败 | 🟡 中 | 先跑全量装载验证；必要时降级为 warn |
| `pk` 与 `key.column` 冲突 | 🟡 中 | 硬失败并明确指出两处值 |
| composite 与 `properties[].composite` 混淆 | 🟢 低 | 文档 + schema 分层约束 |

**回滚**：`key` 是新增字段，删除即回落到现有 pk 分支；`business_key` 改名因 0 处使用，回滚零成本。

---

## 8. 待决事项

**① DDL 是否直接加 `PRIMARY KEY` 约束？**
- 加：真正堵住重复主键，但存量脏数据会装载失败
- 不加（仅装载期断言）：温和，但约束不落库

我倾向**先只做装载期断言，观察一个迭代**，确认无脏数据后再落 DDL。理由是：约束一旦落库，失败即整包装载失败，而断言可以先报出来让你看见。

**② `composite` 现在做还是等需求？**
当前 20 个对象无实际需求（P1-5 的 0.8 人日是纯预留）。但它能堵住"用 hash 拼复合 id"这条歪路。

**③ `pk` 最终是否移除？**
本方案保留为只读别名。若你倾向彻底移除，需额外 0.5 人日（20 对象 + 十几处引用 + 前端契约）。

---

## 附：核查脚本

`verify_object_key_design.py` —— 21 项，覆盖上文全部实跑结论（四处 key 占用、前缀一致性、前后端校验矛盾、无唯一约束、业务键使用面），可复跑。
