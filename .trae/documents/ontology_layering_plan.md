# 本体分层重构实施方案：类型层（objects/links）与管道层（bindings）分离

## 〇、已确认决策（用户 2026-09-03 拍板）

1. **分层落点**：新增 `bindings.json`，案件包由四段变五段；objects/links 回归纯类型层，管道声明全部进 bindings。
2. **类型化物化本期就做**：属性值类型驱动物化——DDL 按类型生成、结构化 source 编译期 CAST（amount→DOUBLE、date→DATE、times→BIGINT）；links/functions 中现有 CAST 保留为幂等空操作，本期不清理。
3. **版本策略**：schema_version 升 2，**只支持新格式**（不做 v1 兼容）；同步改造 default 包，使 `build_ontology` / `run_all` 在 v2 下执行成功；测试临时包一并改写。

## 一、调研结论

### 现状
`ontology/default/objects.json` 与 `links.json` 把 Palantir Foundry 的三个层压在一份声明里：

| 层 | Foundry 位置 | 当前 JSON 字段 | 现状问题 |
|---|---|---|---|
| 本体类型定义 | Object/Link Type | `name`/`title`/`pk` | 属性无值类型；`properties` 是死字段（全库 grep `.properties` 零命中，编译器从不读），且与 `source.columns` 信息重复 |
| 数据源绑定 | backing datasource | `source.table` + `source.columns` | 内联在类型声明中 |
| 数据管道 | Transforms（独立产物） | `source_sql`（person 跨 6 表 UNION）、`clean`、`row_key`、`optional`、links 的 `build_sql` | 管道关注点与类型定义混在一起；links 的 `build_sql` 里还漏进检测判据（time_window 的 `% 10000 = 0`、`NOT LIKE '%公司%'`） |

其他事实：
- 物化列**全部 VARCHAR**（[_drop_create](file:///d:/dev/inves_duckdb/core/ontology.py#L270-L273)），`amount`/`date` 的 CAST 散落在 links.json / functions.json 的 SQL 里。
- runtime 对象 `decision` 在 objects.json 里**没有属性定义**，其 8 列 DDL 硬编码在 [action_executor.py](file:///d:/dev/inves_duckdb/core/action_executor.py#L82-L91)。
- 代理键两类策略（实体按 raw_name / 事件按行）由 `row_key` 布尔表达。

### 消费点清单（已逐个核实）
| 消费方 | 消费内容 | 本次是否受影响 |
|---|---|---|
| [core/ontology_loader.py](file:///d:/dev/inves_duckdb/core/ontology_loader.py) | 装载+校验全部字段 | **重写 objects/links 段** |
| [core/ontology.py](file:///d:/dev/inves_duckdb/core/ontology.py) | `ObjectSpec`/`LinkSpec` dataclass、`build_ontology()` 编译器、清洗规则、`reverse_reach` | **dataclass 拆分 + 编译器改读 bindings** |
| [core/functions.py](file:///d:/dev/inves_duckdb/core/functions.py) | 只读 `pack.functions`；py 实现仅用 `COUNT(*)` 与字符串列，不直接对 amount/date 做字符串操作 | 不改逻辑 |
| [core/action_executor.py](file:///d:/dev/inves_duckdb/core/action_executor.py) | 只读 `pack.actions`；硬编码 obj_decision/lnk_decision_for DDL | **DDL 改由类型声明生成** |
| [core/graph.py](file:///d:/dev/inves_duckdb/core/graph.py) | 读 lnk_transfers，`float(amt)`/`str(d)` 对类型天然容忍 | 不改 |
| [scripts/build_ontology.py](file:///d:/dev/inves_duckdb/scripts/build_ontology.py) | CLI，读 `pack.objects/links/functions` 计数 | 仅计数口径不变，无需改 |
| [run_all.py](file:///d:/dev/inves_duckdb/run_all.py#L139-L140) | 调 `build_ontology(conn)`（签名保持不变） | 不改 |
| [tests/test_ontology.py](file:///d:/dev/inves_duckdb/tests/test_ontology.py) | 34 项；临时坏包/旧版包按旧 shape 写 JSON | **改临时包 shape + 加新校验用例** |
| scripts/mcp_server.py / mcp_client_test | 不暴露本体目录、只查语义表 | 不改 |

### 行为不变量（重构后必须全部保持）
1. `obj_*`/`lnk_*` 表名、列名、行数与重构前完全一致；
2. 代理键幂等（实体按 raw_name 码点序、事件按行确定性排序）；
3. `source_rows` 溯源串格式不变（`表:name_col=值` / 事件行全字段）；
4. runtime 对象（decision）编译器跳过、重建语义层不丢决策；
5. optional 源表缺失跳过不崩；清洗规则效果不变；
6. `build_ontology(conn, pack=)` 签名与 stats 结构（`objects/links/skipped`）不变。

## 二、目标结构（schema_version 升到 2，硬切换）

案件包目录由四段变五段：

```
ontology/<pack>/
  objects.json    # 【类型层】对象类型：属性 + 值类型，无任何数据来源信息
  links.json      # 【类型层】链接类型：端点对象 + 边属性类型，无 SQL
  bindings.json   # 【管道层·新增】对象绑定（source/清洗/optional）+ 链接绑定（build_sql）
  actions.json    # 不变
  functions.json  # 不变（inputs 引用 obj_*/lnk_*，天然是类型层消费者）
```

### objects.json（v2，纯类型）
```json
{
  "schema_version": 2,
  "objects": [
    {
      "name": "transaction",
      "title": "交易",
      "pk": "txn_id",
      "kind": "event",                    // entity=实体型(按 name_property 发代理键) | event=事件型(按行)，取代 row_key
      "name_property": "from_raw",        // 取代 name_col；必须是已声明属性（clue 这类自引用允许等于 pk）
      "properties": {
        "from_raw": "string",
        "to_raw": "string",
        "amount": "decimal",              // 新：值类型 string|integer|decimal|date|boolean
        "date": "date"
      }
    },
    {
      "name": "decision",
      "title": "处置决定",
      "pk": "decision_id",
      "kind": "entity",
      "runtime": true,                    // runtime 对象现在必须声明完整属性（供 Action 副作用生成 DDL）
      "name_property": "decision_id",
      "properties": {
        "decision_type": "string", "clue_id": "string", "legal_basis": "string",
        "operator": "string", "note": "string", "created_at": "string"
      }
    }
  ]
}
```

### links.json（v2，纯类型）
```json
{
  "schema_version": 2,
  "links": [
    {
      "name": "transfers",
      "title": "转账关系",
      "from_obj": "account",
      "to_obj": "account",
      "properties": {"amount": "decimal", "date": "date"}   // 边属性类型；端点 FK 列名仍由 binding 的 SQL 决定
    },
    {
      "name": "decision_for",
      "title": "决定针对线索",
      "from_obj": "decision",
      "to_obj": "clue",
      "runtime": true,
      "properties": {}
    }
  ]
}
```

### bindings.json（v2，新增·管道层）
```json
{
  "schema_version": 2,
  "object_bindings": [
    {
      "object": "transaction",
      "source": {"table": "银行流水",
                 "columns": {"from_raw": "主体", "to_raw": "对方", "amount": "金额", "date": "日期"}},
      "clean": []
    },
    {
      "object": "person",
      "source_sql": "SELECT 主体 AS raw_name FROM 通话记录 UNION ... ",
      "source_table": "通话记录",
      "clean": ["strip", "exclude_org_tokens"]
    },
    {
      "object": "clue",
      "source": {"table": "clue_disposal_status", "columns": {...}},
      "optional": true
    }
  ],
  "link_bindings": [
    {"link": "transfers",
     "build_sql": "SELECT txn_id, from_raw AS from_account, to_raw AS to_account, amount, date FROM obj_transaction"}
  ]
}
```

字段归属总表：

| 旧字段 | 新位置 |
|---|---|
| `name`/`title`/`pk` | objects/links 类型层（不动） |
| `properties`（死字段） | 类型层，改为 `{属性名: 值类型}`，**变为强校验输入** |
| `name_col` | 类型层 `name_property` |
| `row_key: true` | 类型层 `kind: "event"`（默认 `entity`） |
| `runtime` | 类型层（不动） |
| `source` / `source_sql` / `source_table` | bindings.json 的 object_bindings |
| `clean` / `optional` | bindings.json 的 object_bindings |
| links 的 `build_sql` | bindings.json 的 link_bindings（SQL 文本**原样搬迁**，本期不改写） |
| `_note` | 允许保留（loader 忽略下划线开头键） |

## 三、文件与模块改动

- [core/ontology.py](file:///d:/dev/inves_duckdb/core/ontology.py)：
  - 新增 `TYPE_SQL = {"string":"VARCHAR","integer":"BIGINT","decimal":"DOUBLE","date":"DATE","boolean":"BOOLEAN"}`；
  - dataclass 拆为 `ObjectType` / `ObjectBinding` / `LinkType` / `LinkBinding`（`OntologyPack` 对外仍暴露 `.objects`/`.links` 列表 + `.object_bindings`/`.link_bindings` 字典，`actions`/`functions` 不变）；
  - 编译器 `build_ontology()` 改为：遍历类型层（runtime 跳过）→ 查对应 binding → 执行 `binding.source_sql` → 按**类型层属性类型**生成 DDL 与 CAST → 清洗/代理键/source_rows 逻辑原样保留；
  - links 物化后校验：声明的边属性必须存在于实际输出列，否则硬失败；
  - 新增 `ensure_runtime_tables(conn, pack)`：按 runtime 类型声明生成 `CREATE TABLE IF NOT EXISTS`（对象：pk + 类型化属性 + source_rows；链接：端点列约定 `<from_obj>_id`/`<to_obj>_id`，现有 pk 全部是 `<名>_id` 形态，decision_for 即 decision_id/clue_id）；
  - `get_action`/`actions_report`/清洗规则库/`reverse_reach` 不动。
- [core/ontology_loader.py](file:///d:/dev/inves_duckdb/core/ontology_loader.py)：
  - `SCHEMA_VERSION = 2`；v1 包报清晰错误（不做双版本兼容：仅 default 一个包 + 测试临时包，硬切换符合项目"未知名硬失败"哲学）；
  - `_compile_structured_source` 升级为类型感知：`"源列" AS alias`（string）或 `CAST("源列" AS <SQL类型>) AS alias`（decimal/date/integer）；
  - 新增两段交叉校验（见下"校验规则"）；
  - actions 装载增加：`create_decision` 副作用要求 `decision` 对象已在类型层声明。
- [core/action_executor.py](file:///d:/dev/inves_duckdb/core/action_executor.py)：`_create_decision` 里两段硬编码 DDL 改为调 `ensure_runtime_tables()`（列序与现在完全一致：decision_id, decision_type, clue_id, legal_basis, operator, note, created_at, source_rows → INSERT 不动）。
- `ontology/default/objects.json` / `links.json`：改写为 v2 纯类型；新增 `ontology/default/bindings.json`（管道字段原样搬迁）。
- [tests/test_ontology.py](file:///d:/dev/inves_duckdb/tests/test_ontology.py)：临时包（坏包/旧版包）改 v2 shape 并补 bindings.json；新增校验用例（见验证节）；既有 34 项断言全部保留。
- [AGENTS.md](file:///d:/dev/inves_duckdb/AGENTS.md)：语义层段落"objects/links/actions/functions 四段"更新为五段（types 与 bindings 分离）的描述。

### loader v2 校验规则（任一不满足硬失败）
1. schema_version=2；对象/链接名唯一；`kind ∈ {entity, event}`；
2. 每个属性值类型 ∈ TYPE_SQL；`pk` 不得出现在 properties 中；`name_property` 必须是已声明属性（或等于 pk，兼容 clue 自引用）；
3. 每个**非 runtime** 对象恰好有一条 object_binding；**runtime** 对象不得有 binding；链接同理；
4. 结构化 source 的别名集合必须 ⊆ 该对象属性集合，且覆盖 `name_property`；`clean` 引用已注册规则；
5. link 的 from_obj/to_obj 必须是已声明对象；runtime 链接端点列按 `<obj>_id` 约定；
6. functions 的 inputs 引用 obj_*/lnk_* 校验（不变）；actions 校验（不变）+ create_decision 目标对象存在；
7. 链接边属性在物化后与实际输出列对账（编译期运行时校验，free SQL 无法静态检查）。

## 四、实施步骤（依赖序）

1. **core/ontology.py**：加 TYPE_SQL；拆四个 dataclass；`OntologyPack` 扩 bindings 容器；加 `ensure_runtime_tables()`；编译器改读 bindings + 类型化 DDL/CAST（清洗、代理键、source_rows、stats 结构原样）。
2. **core/ontology_loader.py**：v2 装载（objects/links 纯类型 + bindings.json）+ 上述 7 条校验；结构化 source 编译加 CAST。
3. **core/action_executor.py**：接 `ensure_runtime_tables()`，删硬编码 DDL。
4. **迁移 default 包**：objects.json/links.json 改写为 v2；新建 bindings.json 搬迁全部管道字段（build_sql 逐字原样，含 time_window 现有判据——本期不动语义）。
   - 类型标注要点：amount→decimal、各 date/pub_date/submit_date→date、times→integer，其余 string；clue 表全 string；decision 全 string（与现 DDL 逐字同构）。
5. **tests/test_ontology.py**：临时包改 v2；新增用例：
   - 类型化列：`obj_transaction.amount` 为 DOUBLE、`date` 为 DATE（查 information_schema）；
   - 非 runtime 对象缺 binding → 硬失败；binding 别名不在属性集 → 硬失败；未知值类型 → 硬失败；链接边属性不在 SQL 输出 → 硬失败；runtime 对象存在 binding → 硬失败；
   - `ensure_runtime_tables` 生成 obj_decision 且列与 INSERT 一致；
   - 结构化 source 生成 CAST 的单元断言（替换原 `_compile_structured_source` 用例）。
6. **AGENTS.md** 语义层段落小改。
7. WSL 全量验证（见下）。

## 五、依赖与注意事项

- **类型提升的兼容性已审计**：py 函数只用 COUNT 与字符串列；graph.py `float(amt)`/`str(d)` 对 DOUBLE/DATE 天然容忍；links/functions 现有 SQL 里的 `CAST(... AS DATE/BIGINT/DOUBLE)` 在已类型化列上是幂等空操作，**本期一律不删**（零风险；清理留后续）。
- **事件行幂等排序不受影响**：排序发生在 fetchall 之后、INSERT 之前，值的 Python 类型与重构前一致（DOUBLE→float、VARCHAR→str 的 `str()` 表示相同，如 `100000.0`、`2021-09-28`）。
- person/account 的 `source_sql` 是全字符串 UNION，不包 CAST，行为不变。
- `lnk_*` 由 `CREATE TABLE AS` 自动继承类型（transfers.amount 随之变 DOUBLE），消费方均容忍。
- 脏数据风险：若某日期字符串 CAST DATE 失败，编译报错面清晰；兜底手段是把该属性改回 `string`（改 JSON 即可，不动代码）。
- **本期明确不做**（列为后续 backlog）：① 把 time_window build_sql 里的检测判据（整数/排除公司）上移到 function 层；② 清理 links/functions SQL 中冗余 CAST；③ 链接端点列名形式化（from_col/to_col）；④ MCP 暴露本体类型目录。

## 六、验证

WSL 环境（默认）：
```bash
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.mcp_client_test"
```
- 8 组测试（mcp/miaosuan/graph/org/review/disposal/ontology/e2e）全绿；mcp_client_test 40/40；
- 既有断言即行为对账：代理键、行数、溯源串、co_located/time_window 语义、decision 副作用全部不变；
- 新增校验用例全绿；
- `python -m scripts.build_ontology` 手工冒烟：统计行数与重构前一致、无 skipped 异常。

## 七、风险与回退

| 风险 | 处置 |
|---|---|
| CAST 在真实 parquet 数据上失败（脏日期/金额） | 编译即报错且定位到具体 binding；回退手段是该属性改 `string`（纯数据改动） |
| v2 硬切换遗漏某消费方 | 消费点已全量清点（本表第一节），测试网覆盖；`build_ontology` 签名不变是保险 |
| runtime DDL 生成与现有 INSERT 列序不一致 | decision 属性按现 DDL 列序声明，测试 `test_合法立案创建决策对象` 直接覆盖 |
| 重构中途状态不可用 | 全部改动在一个分支内完成、测试全绿前不合并；无数据迁移（语义表随时可 drop 重建，决策表有 IF NOT EXISTS 且重建语义层本就不丢决策） |
