"""
core/ontology.py
语义层（Palantir Ontology 风格裁剪版）：Object Types + Link Types + Action Types + Function Types。

声明分两层（schema_version=2，案件包五段）：
  类型层（本体定义，"是什么"）：
    objects.json —— Object Types：name/pk/kind(entity|event)/name_property/properties{属性:值类型}，
                    不含任何数据来源信息；编译器物化为 obj_* 表；
    links.json   —— Link Types：from_obj/to_obj + 边属性类型，不含 SQL；物化为 lnk_* 表；
  管道层（绑定与变换，"怎么来"）：
    bindings.json —— object_bindings（source/source_sql/clean/optional）+
                     link_bindings（build_sql）；
  actions.json   —— Action Types：受控写回（verify/reset/exclude/confirm/file），
                    由 ActionExecutor 统一执行（角色/参数/状态机/副作用/审计）；
  functions.json —— Function Types：只读计算，由 FunctionExecutor 执行，只读不写对象。

值类型驱动物化：properties 的值类型（string/integer/decimal/date/boolean）经 TYPE_SQL
映射为 DuckDB 列类型；结构化 source 编译期 CAST，obj_* 不再全 VARCHAR。

声明与实现分离（元数据化）：
  - 声明是 JSON 数据（ontology/<pack>/*.json，案件包可切换），可审阅/可版本化；
  - 实现是 Python：清洗规则（CLEAN_RULE_NAMES）、Function py 实现（core.functions）、
    Action 副作用（core.action_executor）均按名注册，加载时校验引用存在性，未知名硬失败。

主键策略：代理键（person_0001 式），分两类——
  实体型（kind=entity）：按 name_property 值排序分配，同输入同键（幂等）；
  事件型（kind=event，transaction/call/trackpoint 等）：同一主体对应多行，
  代理键按行分配，行集做确定性排序保证幂等。

红线不变：
  - 每张语义表带 source_rows（JSON 数组，溯源到 L2/L3 行）——红线 2
  - 语义层无定性字段；clue 状态迁移只经 Action 执行器，file 仅具名正兵——红线 1
  - Function 只读（SQL 实现强制 SELECT/WITH 白名单），不改对象——红线 3
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from core.registry import ClueStatusMachine

# ----------------------------------------------------------------------
# 声明式 schema（内存模型；JSON 由 core.ontology_loader 装载为本组 dataclass）
# 类型层（ObjectType/LinkType）只回答"是什么"；管道层（*Binding）回答"怎么来"。
# ----------------------------------------------------------------------

# 属性值类型 → DuckDB 列类型（结构化 source 编译期 CAST 同口径）
TYPE_SQL = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "decimal": "DOUBLE",
    "date": "DATE",
    "boolean": "BOOLEAN",
}
TYPE_NAMES = tuple(TYPE_SQL)
OBJECT_KINDS = ("entity", "event")   # entity=实体型（按 name_property 发代理键）；event=事件型（按行）


@dataclass
class ObjectType:
    """对象类型声明（类型层）：现实实体是什么、有哪些带类型的属性。"""
    name: str                          # 语义表名（不带 obj_ 前缀）
    title: str                         # 中文名
    pk: str                            # 代理键列名
    kind: str                          # entity | event（见 OBJECT_KINDS）
    name_property: str                 # 身份/展示属性名（旧 name_col；event 仅作列序）
    properties: dict[str, str] = field(default_factory=dict)  # 属性名 → 值类型（TYPE_NAMES）
    runtime: bool = False              # True=运行期对象（Action 副作用创建，编译器不物化）


@dataclass
class ObjectBinding:
    """对象绑定声明（管道层）：对象类型的数据从哪来、怎么清洗。"""
    object: str                        # 指向 ObjectType.name
    source_sql: str                    # 来源查询（结构化源由 loader 编译生成，含类型 CAST）
    source_table: str = ""             # 溯源标注用主源表（结构化源自动取 table）
    clean: tuple[str, ...] = ()        # 清洗规则名（见 CLEAN_RULE_NAMES）
    optional: bool = False             # True=源表缺失/缺列时跳过（如 clue 尾部物化）


@dataclass
class LinkType:
    """链接类型声明（类型层）：对象间关系是什么。"""
    name: str                          # 语义表名（不带 lnk_ 前缀）
    title: str
    from_obj: str                      # 起点对象（语义名）
    to_obj: str                        # 终点对象
    properties: dict[str, str] = field(default_factory=dict)  # 边属性名 → 值类型
    runtime: bool = False              # True=运行期链接（Action 副作用写入，不参与编译）


@dataclass
class LinkBinding:
    """链接绑定声明（管道层）：边怎么物化。"""
    link: str                          # 指向 LinkType.name
    build_sql: str                     # 物化 SQL（引用 obj_*/lnk_* 或 L2 表）


@dataclass
class ParamSpec:
    """Action 参数声明。"""
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""


@dataclass
class ActionSpec:
    """动作类型声明（受控写回）。allowed_from 由 ClueStatusMachine 反向派生——单一事实来源。"""
    name: str
    target_status: str
    allowed_from: tuple[str, ...]
    parameters: tuple[ParamSpec, ...] = ()
    requires_role: str = "any"     # any=AI/正兵均可；human=仅具名正兵
    side_effects: tuple[str, ...] = ()
    terminal: bool = False
    description: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["parameters"] = [asdict(p) for p in self.parameters]
        return d


@dataclass
class FunctionSpec:
    """函数类型声明（只读计算）。impl=sql 时执行 sql；impl=py 时调用 core.functions 注册实现。"""
    name: str
    title: str
    inputs: tuple[str, ...]        # 消费的 obj_*/lnk_* 表（声明式依赖）
    output_type: str               # rows / scalar / report
    impl: str                      # sql | py
    parameters: dict = field(default_factory=dict)
    impl_ref: str = ""             # py 实现名（core.functions.FUNCTION_IMPLS 键）
    sql: str = ""                  # sql 实现文本（只读白名单校验）
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---- 清洗规则库（声明引用名；实现集中在此，可扩展） ----
CLEAN_RULE_NAMES = {"strip", "exclude_org_tokens"}

ORG_KEYWORDS = ("公司", "局", "厂", "中心", "部", "建材", "建设", "银行",
                "财政", "集团", "院", "所", "处", "队")
SUMMARY_TOKENS = ("现金存入", "工资", "代发", "利息", "转账", "存款", "取现")


def clean_strip(v: str) -> str:
    return (v or "").strip()


def clean_exclude_org_tokens(v: str, org_names: set[str]) -> bool:
    """True = 判定为组织/摘要 token，应从 person 候选中排除。"""
    if v in org_names:
        return True
    return any(k in v for k in ORG_KEYWORDS) or v in SUMMARY_TOKENS


def _default_org_names(conn=None) -> set[str]:
    """从 L2 工商信息读登记名作为 org 名单（编译时动态获取，不硬编码）。"""
    try:
        if conn is None:
            from core.store import Store
            conn = Store().conn
        rows = conn.execute("SELECT 主体 FROM 工商信息").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


# ----------------------------------------------------------------------
# Action allowed_from 派生（_TRANSITIONS[from]={可达目标} 的反向视图）
# ----------------------------------------------------------------------
def reverse_reach(target: str) -> tuple[str, ...]:
    """能一步迁移到 target 的源状态。"""
    T = ClueStatusMachine._TRANSITIONS
    return tuple(src for src, tgts in T.items() if target in tgts)


# ----------------------------------------------------------------------
# 编译器
# ----------------------------------------------------------------------
def _proxy_keys(raw_names: list[str], prefix: str) -> dict[str, str]:
    """代理键分配：排序后 person_0001 式（幂等：同输入同键）。"""
    return {n: f"{prefix}_{i:04d}"
            for i, n in enumerate(sorted(raw_names), 1)}


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()[0] > 0


def build_ontology(conn, pack: str = "default") -> dict:
    """
    从 ontology/<pack> 声明编译 obj_* / lnk_* 语义表（幂等，可重跑）。
    类型层（objects/links）决定 schema（值类型→列类型），管道层（bindings）决定数据来源。
    返回 {"objects": {name: n_rows}, "links": {name: n_rows}, "skipped": [...]}。
    runtime 对象/链接（如 decision）不由编译器物化，仅由 ensure_runtime_tables 建空表。
    """
    from core.ontology_loader import load_pack
    spec = load_pack(pack)

    stats: dict = {"objects": {}, "links": {}, "skipped": []}
    org_names = _default_org_names(conn)

    conn.execute("BEGIN TRANSACTION")
    try:
        # ---- 1) Object 物化（runtime 对象跳过；每个非 runtime 对象必有 binding）----
        for otype in spec.objects:
            if otype.runtime:
                continue
            b = spec.object_bindings[otype.name]
            src_table = b.source_table or _guess_source_table(b.source_sql)
            if not _table_exists(conn, src_table):
                tag = "(源表缺失,optional)" if b.optional else "(源表缺失)"
                stats["skipped"].append(f"obj_{otype.name}{tag}")
                continue
            try:
                q = (f"SELECT {otype.name_property}, * EXCLUDE ({otype.name_property}) "
                     f"FROM ({b.source_sql})")
                rows = conn.execute(q).fetchall()
                cols = [d[0] for d in conn.execute(q + " LIMIT 0").description]
            except Exception as e:
                # 典型：L2 表存在但缺新 schema 列（如旧版公开OSINT仅2列）
                if b.optional:
                    stats["skipped"].append(
                        f"obj_{otype.name}(编译失败,源列缺失已跳过: {type(e).__name__})")
                    continue
                raise
            if b.clean:
                rows = _apply_clean(rows, cols, otype, b, org_names)
            prefix = otype.pk.split("_")[0]
            if otype.kind == "event":
                # 事件型：代理键按行分配；行集确定性排序保证幂等
                rows = sorted(rows, key=lambda r: tuple(str(v) for v in r))
                keys = [f"{prefix}_{i:04d}" for i in range(1, len(rows) + 1)]
            else:
                # 实体型：按 name_property 值排序分配（同输入同键）
                proxy = _proxy_keys(sorted({r[0] for r in rows}), prefix)
                keys = [proxy[r[0]] for r in rows]
            rest_cols = [c for c in cols if c != otype.name_property]
            same = otype.pk == otype.name_property
            _create_obj_table(conn, otype, same, rest_cols)
            for key, r in zip(keys, rows):
                raw, *props = r
                if otype.kind == "event":
                    src = [f"{src_table}:"
                           + ",".join(f"{c}={v}" for c, v in zip(cols, r))]
                else:
                    src = [f"{src_table}:{otype.name_property}={raw}"]
                if same:
                    ph = ", ".join(["?"] * (len(rest_cols) + 1))
                    vals = [key, *props, json_dumps(src)]
                else:
                    ph = ", ".join(["?"] * (len(rest_cols) + 2))
                    vals = [key, raw, *props, json_dumps(src)]
                conn.execute(f"INSERT INTO obj_{otype.name} VALUES (?, {ph})", vals)
            stats["objects"][otype.name] = len(rows)

        # ---- 2) Link 物化（runtime 链接跳过；声明边属性与输出列对账）----
        for ltype in spec.links:
            if ltype.runtime:
                continue
            lb = spec.link_bindings[ltype.name]
            try:
                conn.execute(f"DROP TABLE IF EXISTS lnk_{ltype.name}")
                conn.execute(f"CREATE TABLE lnk_{ltype.name} AS {lb.build_sql}")
            except Exception as e:
                stats["skipped"].append(
                    f"lnk_{ltype.name}(编译失败已跳过: {type(e).__name__})")
                continue
            actual = {d[0] for d in conn.execute(
                f"SELECT * FROM lnk_{ltype.name} LIMIT 0").description}
            missing = [p for p in ltype.properties if p not in actual]
            if missing:
                # 声明与实现不一致 = 声明错误，硬失败（不准带病编译）
                raise ValueError(
                    f"链接 {ltype.name} 声明边属性 {missing} 不在 build_sql 输出列 "
                    f"{sorted(actual)} 中（检查 links.json 与 bindings.json）")
            n = conn.execute(f"SELECT COUNT(*) FROM lnk_{ltype.name}").fetchone()[0]
            stats["links"][ltype.name] = n

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # ---- 3) runtime 对象/链接：按类型声明建空表（IF NOT EXISTS，不碰既有数据）----
    ensure_runtime_tables(conn, spec)
    return stats


def ensure_runtime_tables(conn, pack: "str | object" = "default") -> None:
    """
    按 runtime 类型声明建 obj_*/lnk_* 空表（CREATE IF NOT EXISTS）。
    Action 副作用（core.action_executor）的唯一建表入口；build_ontology 编译后也会调用。
    runtime 链接端点列约定：<from_obj>_id / <to_obj>_id（loader 已校验端点 pk 同形）。
    """
    from core.ontology_loader import load_pack
    spec = load_pack(pack) if isinstance(pack, str) else pack
    for otype in spec.objects:
        if not otype.runtime:
            continue
        defs = [f'"{otype.pk}" VARCHAR']
        defs += [f'"{p}" {TYPE_SQL[t]}' for p, t in otype.properties.items()]
        defs.append('"source_rows" VARCHAR')
        conn.execute(f'CREATE TABLE IF NOT EXISTS obj_{otype.name} ({", ".join(defs)})')
    for ltype in spec.links:
        if not ltype.runtime:
            continue
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS lnk_{ltype.name} '
            f'("{ltype.from_obj}_id" VARCHAR, "{ltype.to_obj}_id" VARCHAR)')


def _guess_source_table(source_sql: str) -> str:
    """source_sql 兜底时的溯源表名：取 FROM 后第一个标识符（粗粒度，仅用于存在性判断/溯源串）。"""
    import re
    m = re.search(r"FROM\s+([^\s()]+)", source_sql or "")
    return m.group(1) if m else ""


def _apply_clean(rows: list, cols: list[str], otype: ObjectType,
                 binding: ObjectBinding, org_names: set[str]) -> list:
    """按 binding 声明应用清洗规则（当前：person 的 exclude_org_tokens）。"""
    if otype.name != "person" or "exclude_org_tokens" not in binding.clean:
        return rows
    return [r for r in rows
            if not clean_exclude_org_tokens(clean_strip(str(r[0])), org_names)]


def _create_obj_table(conn, otype: ObjectType, same: bool,
                      rest_cols: list[str]) -> None:
    """按类型声明建 obj_* 表：pk VARCHAR + 类型化属性列 + source_rows VARCHAR。"""
    conn.execute(f"DROP TABLE IF EXISTS obj_{otype.name}")
    defs = [f'"{otype.pk}" VARCHAR']
    if not same:
        t = otype.properties.get(otype.name_property, "string")
        defs.append(f'"{otype.name_property}" {TYPE_SQL[t]}')
    for c in rest_cols:
        t = otype.properties.get(c, "string")
        defs.append(f'"{c}" {TYPE_SQL[t]}')
    defs.append('"source_rows" VARCHAR')
    conn.execute(f'CREATE TABLE obj_{otype.name} ({", ".join(defs)})')


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ----------------------------------------------------------------------
# Action 注册表查询（disposal / MCP 消费）
# ----------------------------------------------------------------------
def get_action(name: str, pack: str = "default") -> ActionSpec:
    from core.ontology_loader import load_pack
    actions = load_pack(pack).actions
    if name not in actions:
        raise KeyError(f"未注册的 Action：{name}，可用 {sorted(actions)}")
    return actions[name]


def actions_report(pack: str = "default") -> list[dict]:
    from core.ontology_loader import load_pack
    return [a.to_dict() for a in load_pack(pack).actions.values()]
