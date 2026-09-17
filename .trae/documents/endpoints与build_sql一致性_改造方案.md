# endpoints 与 build_sql 一致性改造方案

> 目标：消灭"图上静默少一条边"这类不可见故障。
> 核查基准：`impl_v4`（default 包 11 条 link / reqd_case 包 2 条 link）

---

## 一、现状：声明有校验，实现无对账

### 1.1 两套声明的分工

| | build_sql | endpoints |
|---|---|---|
| 位置 | `bindings.json` | `links.json` |
| 作用 | **执行**——`CREATE TABLE lnk_X AS <build_sql>` | **描述**——图上怎么画这条边 |
| 谁读 | DuckDB（真执行） | `graph_view.py`、`export_ladybug.py`、`canvas_expand.py` |
| 校验 | 与 `properties` 对账、与 `normalize` 逐字对账 | **仅结构自洽** |

### 1.2 现有校验点（两处，都不覆盖一致性）

**装载期** `core/ontology_loader.py:1251` `_parse_endpoints(l, ctx, obj_names)`

只校验三件事：
- `endpoints.{from,to}.col` 非空
- `ref` 若存在，三键（`object`/`key`/`name`）齐全
- `ref.object` 已在 objects 声明

**不校验**：`ref.object` 是否等于该侧的 `from_obj` / `to_obj`。

**物化期** `core/ontology.py:471-482`

```python
conn.execute(f"CREATE TABLE lnk_{ltype.name} AS {lb.build_sql}")
actual = {d[0] for d in conn.execute(
    f"SELECT * FROM lnk_{ltype.name} LIMIT 0").description}
missing = [p for p in ltype.properties if p not in actual]
if missing:
    raise ValueError(...)   # 只对账 properties
```

**拿到了 `actual` 全集，却只用来对账 `properties`**——endpoints 的 `col` / `extra` 完全没参与。

### 1.3 实测错位清单

静态核查（AS 别名 + ref.object 与 from_obj/to_obj 比对）：

| 包 | link | 问题 | 严重度 |
|---|---|---|---|
| default | **owns** | `from.col=owner_raw`（人名）但 `from_obj=account`；`to.ref.object=account` 但 `to_obj=person` —— **两端整体对调** | 🔴 |
| default | **time_window** | 两端均无 ref，`from.col=title` 是 name 列非主键 `project_id`；`to.col=owner_raw` 是人名非 `txn_id` | 🔴 |
| default | **osint_mentions** | 无 endpoints，但 build_sql 正常产出 `article_id/person_id` | 🟡 |
| default | decision_for | runtime link，无 build_sql | ⚪ 应豁免 |
| reqd_case | transfers / calls_to | 无 endpoints | 🟡 |

正确：transfers、calls_to、org_interest、involved_in、co_located、tipoff_targets_person、tipoff_from_reporter（7 条）

### 1.4 后果（为什么必须改）

`server/app/graph_view.py:97-104`：

```python
eps = lk.endpoints or {}
...
if not f_ref or not t_ref:
    continue   # 端点无对象引用 → 跳过
```

**owns、time_window、osint_mentions 三条边在图上看不见。**
其中 `owns`（谁持有这个账户）对侦查是核心边。而这个过程**没有任何报错**——装载成功、物化成功、接口正常，只是图上少几条线。

---

## 二、根因

一处有硬校验（build_sql ↔ properties），另一处只有结构自洽（endpoints），**两者之间没有任何对账**。

这与此前修过的三处同源：声明了没接线、改了不进指纹、崩了不报错——共同点是**故障静默**。

---

## 三、改造项

### P0-1 装载期：`ref.object` 必须与 `from_obj` / `to_obj` 一致

**文件**：`core/ontology_loader.py`

**改动 1** 函数签名加两个参数（`:1251`）：

```python
def _parse_endpoints(l: dict, ctx: str, obj_names: set[str],
                     from_obj: str, to_obj: str) -> dict:
```

**改动 2** 在 ref 三键校验后加方向校验：

```python
if ref["object"] not in obj_names:
    raise ValueError(
        f"{ctx} endpoints.{side}.ref.object='{ref['object']}' 未在 objects 声明")
# ↓ 新增
expect = from_obj if side == "from" else to_obj
if ref["object"] != expect:
    raise ValueError(
        f"{ctx} endpoints.{side}.ref.object='{ref['object']}' "
        f"与 links.{side}_obj='{expect}' 不一致"
        f"（build_sql 唯一执行源：端点列必须属于声明侧对象）")
```

**改动 3** 调用点（`:1239`）传参：

```python
endpoints = _parse_endpoints(l, f"{ctx}（{name}）", obj_names,
                             l["from_obj"], l["to_obj"])
```

> `from_obj` / `to_obj` 在 `_parse_links` 已是必填（`l["from_obj"]`），无需兜底。

**能抓住**：owns 的 `to.ref.object=account ≠ person`
**抓不住**：owns 的 from 无 ref、time_window 两端无 ref → 靠 P0-2

### P0-2 物化期：endpoints 列必须对账 `actual`

**文件**：`core/ontology.py`（`:471-482`，紧邻 properties 对账之后）

```python
# 紧接 properties 对账之后
_ep = ltype.endpoints or {}
_ep_missing = []
for _side in ("from", "to"):
    _c = (_ep.get(_side) or {}).get("col")
    if _c and _c not in actual:
        _ep_missing.append(f"endpoints.{_side}.col={_c}")
for _c in (_ep.get("extra") or []):
    if _c and _c not in actual:
        _ep_missing.append(f"endpoints.extra={_c}")
if _ep_missing:
    raise ValueError(
        f"链接 {ltype.name} 声明的 {_ep_missing} 不在 build_sql 输出列 "
        f"{sorted(actual)} 中（endpoints 描述与 build_sql 实现不一致）")
```

**为什么放在物化期而不是装载期**：
build_sql 输出列名无法静态可靠提取——大量列是 `SELECT t.amount` 无别名形式，只有 `CREATE TABLE AS` 执行后 `LIMIT 0` 才拿得到全集（实测 `owns` 静态只提得到 2 个 AS 别名，实际 3 列）。

**成本**：零额外查询——`actual` 已经在算了。

**能抓住**：endpoints.col / extra 写了不存在的列名（含拼错、改名后未同步）

### P0-3 未声明 endpoints 改为显式 opt-out

**问题**：现在 `endpoints` 缺失 = 静默不入图，与"忘了写"无法区分。

**方案**：非 runtime link 若无 endpoints，装载期硬失败，除非显式声明不入图：

```json
{"name": "decision_for", "graph_visible": false, ...}
```

`_parse_endpoints` 开头：

```python
ep = l.get("endpoints")
if ep is None:
    if l.get("runtime") or l.get("graph_visible") is False:
        return {}          # 显式豁免
    raise ValueError(f"{ctx} 未声明 endpoints（图导出端点）"
                     f"；若不参与图，请显式声明 \"graph_visible\": false")
```

**风险**：会破坏 reqd_case（transfers/calls_to 缺）→ 需同步修数据（见第四节）。

> 若你希望先观察，可改为 `warn` 输出；但基于此前多次教训（静默 = 最贵），**建议直接硬失败 + 同步修数据**。

### P1 数据修正（与 P0 同批做，否则包装载不起来）

| 包 | link | 修正 |
|---|---|---|
| default | owns | `from`: `col=account_id`, ref=account/account_id/raw_name<br>`to`: `col=owner_person`, ref=person/person_id/raw_name |
| default | time_window | `from`: `col=project_id`, ref=bid_project/project_id/title<br>`to`: `col=txn_id`, ref=transaction/txn_id/… |
| default | osint_mentions | 补：`from`=article_id ref=osint_article；`to`=person_id ref=person |
| default | decision_for | 加 `"graph_visible": false`（runtime link） |
| reqd_case | transfers / calls_to | 按同构补 endpoints，或声明 `graph_visible: false` |

> owns 修正后需验证 build_sql 输出列确为 `account_id / owner_person / owner_raw`：
> `SELECT a.account_id, p.person_id AS owner_person, a.raw_name AS owner_raw FROM obj_account a JOIN obj_person p ON p.raw_name = a.raw_name` ✅

### P2 可选：`cardinality` 补位

Palantir link type 元数据必备 Cardinality（1:1 / 1:N / M:N），本项目缺失。
补上后可支撑：PlantUML 导出基数标注、"声明 1:N 但数据一对多"的校验、JOIN 优化依据。

**成本 0.5 人日**，建议与 P1 同批——它是导出器的输入，缺了导出的图所有关系线只能画成 `* 对 *`。

---

## 四、验收

### 4.1 反证用例（必须能拦）

| 用例 | 预期 |
|---|---|
| 造 `to.ref.object` ≠ `to_obj` | 装载期硬失败，报出两侧实际值 |
| 造 `endpoints.from.col` 拼错 | 物化期硬失败，列出 actual 全集 |
| 造 `extra` 含不存在列 | 物化期硬失败 |
| 非 runtime link 删掉 endpoints | 硬失败，提示 `graph_visible: false` |
| runtime link 无 endpoints | 放行 |
| 7 条已正确的 link | 零误报 |

### 4.2 正向验证

- 修正后 default 包装载成功，`owns` / `time_window` / `osint_mentions` **入图**
- `graph_view` 入图边数：7 → 10（decision_for 显式豁免）
- 全量回归：`test_ontology` / `test_export_endpoints` / lens 系列与基线逐条一致

### 4.3 迁移检查（改造前先跑）

在全量包与所有存量案件快照上先跑一遍只读检查，列出全部待修项，避免上线即崩。

---

## 五、环境与限制

- **duckdb 当前沙盒不可用**（`ModuleNotFoundError`），P0-2 的物化期校验无法在本地实跑验证，只能静态核对 + 你本地回归
- 静态 AS 别名提取**不可靠**（无别名列提不到），这也是 P0-2 必须放物化期的直接理由
- 前端不涉及

---

## 六、成本汇总

| 项 | 人日 |
|---|---|
| P0-1 装载期方向校验 | 0.3 |
| P0-2 物化期列对账 | 0.3 |
| P0-3 显式 opt-out | 0.4 |
| P1 数据修正（5 处） | 0.5 |
| P2 cardinality（可选） | 0.5 |
| **合计（不含 P2）** | **1.5** |

---

## 七、一句话

**把 endpoints 从"只校验自己写没写对"提升到"与 build_sql 实现对账"——已有 `actual` 列集合就在手边，成本几乎为零，却能把"图上静默少一条边"变成装载期硬失败。**
