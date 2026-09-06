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

主键策略：内容哈希代理键（person_<sha1[:12]> 式），分两类——
  实体型（kind=entity）：键 = 前缀 + sha1(name_property 值)，同名同键，
  且新增名字不改变既有键（增量重建只重写受影响行的前提）；
  事件型（kind=event，transaction/call/trackpoint 等）：键 = 前缀 + sha1(行内容)，
  同内容多行追加 _02/_03 序号；行集确定性排序保证幂等；
  自引用对象（name_property == pk，如 clue）直通源自然键（runtime 链接按此关联）。

红线不变：
  - 每张语义表带 source_rows（JSON 数组，溯源到 L2/L3 行）——红线 2
  - 语义层无定性字段；clue 状态迁移只经 Action 执行器，file 仅具名正兵——红线 1
  - Function 只读（SQL 实现强制 SELECT/WITH 白名单），不改对象——红线 3
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict

from core.registry import ClueStatusMachine

# ----------------------------------------------------------------------
# 声明式 schema（内存模型；JSON 由 core.ontology_loader 装载为本组 dataclass）
# 类型层（ObjectType/LinkType）只回答"是什么"；管道层（*Binding）回答"怎么来"。
# ----------------------------------------------------------------------

# 属性值类型 → DuckDB 列类型（结构化 source 编译期 CAST 同口径）
# REQ-041 扩展：timestamp / duration_days / enum / json
TYPE_SQL = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "decimal": "DOUBLE",
    "date": "DATE",
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",       # REQ-041: 精确到秒的时间点
    "duration_days": "INTEGER",    # REQ-041: 天数差值，支持比较运算
    "enum": "VARCHAR",             # REQ-041: 枚举值，装载期校验白名单
    "json": "VARCHAR",             # REQ-041: JSON 文本，DuckDB 有 JSON 函数
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
    enum_values: dict[str, list[str]] = field(default_factory=dict)  # REQ-041: enum 属性 → 允许值白名单
    jian: str = ""                     # REQ-G-013：五间归类（生间/内间/反间/死间/生间），声明在类型层
    jian_source: str = ""              # REQ-G-013：该数据源展示名（如 银行流水）；空则回落 title
    # REQ-P-034：元数据/内容属性排除声明——不参与实体连接与画像（content_raw/status/title 等治理字段）
    metadata_props: tuple[str, ...] = ()


@dataclass
class ObjectBinding:
    """对象绑定声明（管道层）：对象类型的数据从哪来、怎么清洗。"""
    object: str                        # 指向 ObjectType.name
    source_sql: str                    # 来源查询（结构化源由 loader 编译生成，含类型 TRY_CAST）
    source_table: str = ""             # 溯源标注用主源表（结构化源自动取 table）
    clean: tuple[str, ...] = ()        # 清洗规则名（见 CLEAN_RULE_NAMES）
    optional: bool = False             # True=源表缺失/缺列时跳过（如 clue 尾部物化）
    typed_raw: tuple = ()              # 结构化源 (别名, 源列, 值类型)×非string——构建期 TRY_CAST 脏值计数用
    projections: tuple = ()            # 结构化源 (别名, 源列, 值类型)×全列——缺列预检后重渲染 NULL 用
    optional_raw: tuple = ()           # 声明为可选的源列名（缺列降级 NULL 不硬失败，鲁棒性 B5-01）


@dataclass
class LinkType:
    """链接类型声明（类型层）：对象间关系是什么。"""
    name: str                          # 语义表名（不带 lnk_ 前缀）
    title: str
    from_obj: str                      # 起点对象（语义名）
    to_obj: str                        # 终点对象
    properties: dict[str, str] = field(default_factory=dict)  # 边属性名 → 值类型
    runtime: bool = False              # True=运行期链接（Action 副作用写入，不参与编译）
    jian: str = ""                     # REQ-G-013：五间归类（链接维度，如过桥→反间）
    jian_source: str = ""              # REQ-G-013：数据源展示名；空则回落 title
    # REQ-G-015：图导出端点列名形式化。{"from": {"col","ref"?:{object,key,name}},
    #   "to": {...}, "extra"?: [直传列名]}；导出器据此通用生成边 SQL，新增链接无需改导出脚本。
    endpoints: dict = field(default_factory=dict)


@dataclass
class LinkBinding:
    """链接绑定声明（管道层）：边怎么物化。"""
    link: str                          # 指向 LinkType.name
    build_sql: str                     # 物化 SQL（引用 obj_*/lnk_* 或 L2 表）
    # REQ-P-033：归一 JOIN 声明（v1 校验一致：声明必须在 build_sql 中逐字存在；
    #   build_sql 仍是唯一执行源。v2 远期由编译器从 endpoints 生成 JOIN）
    normalize: tuple = ()


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


@dataclass
class RuleSpec:
    """自然语言检测规则（Rulebook 第六段 rules.json）。

    rule_text 是规则主体（分析师写的判据文本，人/LLM 可读，随线索落产物可审计）；
    function + params 是唯一机器挂钩——确定性执行只认只读 Function，
    自然语言本身不被机器执行（LLM 编排时读取解释，不得自创判据）。
    """
    id: str
    stage: str                     # xu_shi | qi_zheng | yong_jian（决策顺序位置）
    title: str
    rule_text: str                 # 自然语言判据（什么模式/为什么反常/边界排除）
    function: str                  # 绑定的只读 Function 名
    params: dict = field(default_factory=dict)
    hit_when: str = "rows_nonempty"  # rows_nonempty | result_hit
    dimension: str = ""            # 资金 | 通讯 | 行为 | 关系 | 时间
    jian_types: tuple[str, ...] = ()
    assumption: str = ""           # 庙算假设挂钩（如 H1）
    basis_text: str = ""           # 线索"依据"栏短文本
    # REQ-025：互斥与重叠消解
    exclusive_group: str | None = None   # 同组按 primary 命中抑制次级
    primary_rule: bool = False           # 同组内最多一个 primary=True
    overlap_resolution: str | None = None  # drop_if_primary_hit | None
    excludes: tuple[str, ...] = ()       # 与其他规则声明互斥（双向一致性告警）
    # REQ-G-002：零命中语义声明。True=分析师显式声明"本规则空结果属正常（clean_scan）"；
    # 默认 False=空结果标 empty_result_suspect（疑似匹配失效），机器不擅自下"排除"结论。
    zero_is_clean: bool = False

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
    """代理键分配：内容哈希式 person_<sha1(name)[:12]>。

    幂等（同名永远同键）且与插入顺序/其它名字无关——增量插入新名字不会改变
    既有名字的键，这是 REQ-004 增量重建"只重写受影响行"的前提
    （旧的按排序序号分配 person_0001 式会在新名字插入时级联改键）。
    """
    return {n: f"{prefix}_{hashlib.sha1(str(n).encode('utf-8')).hexdigest()[:12]}"
            for n in raw_names}


def _event_proxy_keys(rows: list, prefix: str) -> list[str]:
    """事件型代理键：行内容哈希 + 同内容序号后缀。

    内容完全相同的多行（如同日同额两笔）按确定性顺序追加 _02/_03…，
    保证逐行唯一；旧行键只取决于自身内容，增量追加新行时不变。
    """
    seen: dict[str, int] = {}
    keys: list[str] = []
    for r in rows:
        digest = hashlib.sha1(
            json.dumps(list(r), ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:12]
        n = seen.get(digest, 0) + 1
        seen[digest] = n
        keys.append(f"{prefix}_{digest}" if n == 1 else f"{prefix}_{digest}_{n:02d}")
    return keys


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

    stats: dict = {"objects": {}, "links": {}, "skipped": [], "dirty": [], "degraded": []}
    org_names = _default_org_names(conn)
    entity_mapping = _load_entity_mapping(conn)   # REQ-016 受保护归并映射

    conn.execute("BEGIN TRANSACTION")
    try:
        # ---- 1) Object 物化（runtime 对象跳过；每个非 runtime 对象必有 binding）----
        snap_batches = []   # REQ-008：(obj_type, dataset, cols, rows) 与 source_rows 同源
        for otype in spec.objects:
            if otype.runtime:
                continue
            b = spec.object_bindings[otype.name]
            computed = _compute_object_rows(conn, otype, b, org_names, stats,
                                            mapping=entity_mapping)
            if computed is None:
                continue
            cols, rows, keys, src_table = computed
            rest_cols = [c for c in cols if c != otype.name_property]
            same = otype.pk == otype.name_property
            _create_obj_table(conn, otype, same, rest_cols)
            _insert_object_rows(conn, otype, same, rest_cols, cols,
                                rows, keys, src_table)
            stats["objects"][otype.name] = len(rows)
            if src_table:
                snap_batches.append((otype.name, src_table, cols, rows))

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

    # ---- 3.5) REQ-046 物化 Object Views（v_<name>）----
    # 视图 = SELECT over obj_<base> 的列子集；runtime 对象若 Action 副作用
    # 未建表则按 optional 跳过（与 lnk_* 编译失败跳过同口径）。
    try:
        from core.views import all_views, ViewMaterializer
        views = all_views(pack)
        vm = ViewMaterializer(conn)
        view_status = vm.materialize_all(views, skip_missing_base=True)
        stats["views"] = {
            n: ("ok" if s != "_skipped" else "skipped")
            for n, s in view_status.items()
        }
    except Exception as e:
        stats["views"] = {}
        stats["skipped"].append(f"views(物化失败已跳过: {type(e).__name__}: {e})")

    # ---- 4) 记录版本时钟（REQ-001）：每次 build 写一条 is_current=true ----
    from core.ontology_version import compute_version, record_version
    ver = compute_version(conn, pack, spec)
    record_version(conn, ver)

    # ---- 5) REQ-008：源行内容寻址归档（全量 = bootstrap，不继承旧索引）----
    from core.row_uri import snapshot_source_rows, BOOTSTRAP_PARTITION
    stats["row_snapshot"] = snapshot_source_rows(
        conn, ver.build_id, snap_batches, partition=BOOTSTRAP_PARTITION)

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
    return json.dumps(obj, ensure_ascii=False)


# ----------------------------------------------------------------------
# 共享物化逻辑（全量 build 与增量 materialize_changed 同口径）
# ----------------------------------------------------------------------
def _load_entity_mapping(conn) -> dict:
    """加载正兵 accept 的实体归并映射（variant → canonical）。

    entity_mapping 是受保护表：编译器只 DROP/CREATE obj_*/lnk_*，
    语义层重建/增量物化都不会清除人工确认结果（REQ-004 AC5 / REQ-016）。
    """
    if not _table_exists(conn, "entity_mapping"):
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT variant, canonical FROM entity_mapping").fetchall()}
    except Exception:
        return {}


def _apply_entity_mapping(rows: list, cols: list[str], mapping: dict) -> list:
    """把名字列（raw_name / *_raw 约定）的变体名归并为 canonical。

    对象/事件双侧都归并：lnk_* 的 JOIN（p.raw_name = c.caller_raw）
    两端同时命中 canonical，不会因合并丢边。
    """
    if not mapping:
        return rows
    name_idx = [i for i, c in enumerate(cols)
                if c == "raw_name" or c.endswith("_raw")]
    if not name_idx:
        return rows
    out = []
    for r in rows:
        r = list(r)
        for i in name_idx:
            v = r[i]
            if isinstance(v, str) and v in mapping:
                r[i] = mapping[v]
        out.append(tuple(r))
    return out


def _rerender_source_sql(b: "ObjectBinding", missing: set) -> str:
    """可选源列缺失后重渲染结构化源 SQL：缺失列投影为类型化 NULL（列集/列序不变）。"""
    parts = []
    for alias, raw, t in b.projections:
        if raw in missing:
            parts.append(f'CAST(NULL AS {TYPE_SQL[t]}) AS {alias}')
        elif t == "string":
            parts.append(f'"{raw}" AS {alias}')
        else:
            parts.append(f'TRY_CAST("{raw}" AS {TYPE_SQL[t]}) AS {alias}')
    return f"SELECT {', '.join(parts)} FROM {b.source_table}"


def _compute_object_rows(conn, otype: ObjectType, b: ObjectBinding,
                         org_names: set[str], stats: dict | None = None,
                         mapping: dict | None = None):
    """按 binding 计算对象物化行，返回 (cols, rows, keys, src_table)。

    跳过场景（源表缺失/编译失败且 optional）返回 None，并把原因记入 stats["skipped"]。
    键策略：事件型=行内容哈希（同内容加序号后缀）；实体型=name_property 内容哈希；
    name_property 即 pk 的自引用对象（clue）直通源自然键（runtime 链接按此关联）。
    mapping：正兵确认的 variant→canonical 归并（REQ-016），在代理键分配**之前**应用，
    使合并后的实体共享同一代理键。
    """
    src_table = b.source_table or _guess_source_table(b.source_sql)
    if not _table_exists(conn, src_table):
        if stats is not None:
            tag = "(源表缺失,optional)" if b.optional else "(源表缺失)"
            stats["skipped"].append(f"obj_{otype.name}{tag}")
        return None
    # 列级预检（鲁棒性 B5-01/03）：结构化源在编译 SQL 前先 DESCRIBE 源表——
    # 必填列缺失 → 硬失败但报错直指缺失列名（不再裸抛 BinderException）；
    # 声明为 optional_columns 的可选列缺失 → 该投影重渲染为类型化 NULL，
    # 列集保持稳定、不破坏 obj_* 物化 schema，并降级留痕（source_column_missing）。
    if b.projections and src_table:
        try:
            avail = {r[0] for r in conn.execute(f'DESCRIBE "{src_table}"').fetchall()}
        except Exception:
            avail = None
        if avail is not None:
            missing = list(dict.fromkeys(
                raw for _, raw, _ in b.projections if raw not in avail))
            if missing:
                req_missing = [c for c in missing if c not in b.optional_raw]
                if req_missing:
                    if b.optional:
                        # 整对象可选：源表存在但缺列（如旧版公开OSINT 仅 4 列），
                        # 维持 optional 跳过语义（与编译失败跳过同口径，预检前置而已）
                        if stats is not None:
                            stats["skipped"].append(
                                f"obj_{otype.name}(源列缺失已跳过: {req_missing})")
                        return None
                    raise ValueError(
                        f"obj_{otype.name} 必填源列缺失 {req_missing}（源表 {src_table} "
                        f"实际列 {sorted(avail)}）——补数据，或在 bindings.json 该 binding "
                        f"声明 optional_columns（仅限可空属性）")
                b.source_sql = _rerender_source_sql(b, set(missing))
                if stats is not None:
                    stats["degraded"].append(
                        f"obj_{otype.name} 可选源列缺失 {missing}（已降级类型化 NULL）")
    # TRY_CAST 降级留痕：源列非空但 TRY_CAST 为 NULL 的行数（结构化源才带 typed_raw）。
    # 脏值（如 2024-02-30）在编译期已降级 NULL 不中断 build，此处按列计数进 stats["dirty"]，
    # 由调用方经 run_health.record_build_dirty 落 run_diagnostic（source_value_cast_failed）。
    if stats is not None and b.typed_raw and src_table:
        for alias, raw, t in b.typed_raw:
            try:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{src_table}" WHERE "{raw}" IS NOT NULL '
                    f'AND TRY_CAST("{raw}" AS {TYPE_SQL[t]}) IS NULL').fetchone()[0]
            except Exception:
                continue   # 计数失败不拖垮物化（列可能已被 clean/视图改名等边缘情形）
            if n:
                stats["dirty"].append(
                    f"obj_{otype.name}.{alias}<-{raw}: {n} 行不可转 {t}（已置 NULL）")
    try:
        q = (f"SELECT {otype.name_property}, * EXCLUDE ({otype.name_property}) "
             f"FROM ({b.source_sql})")
        rows = conn.execute(q).fetchall()
        cols = [d[0] for d in conn.execute(q + " LIMIT 0").description]
    except Exception as e:
        # 典型：L2 表存在但缺新 schema 列（如旧版公开OSINT仅2列）
        if b.optional:
            if stats is not None:
                stats["skipped"].append(
                    f"obj_{otype.name}(编译失败,源列缺失已跳过: {type(e).__name__})")
            return None
        raise
    if b.clean:
        rows = _apply_clean(rows, cols, otype, b, org_names)
    # REQ-016：正兵确认的归并映射在代理键分配前应用（对象+事件双侧名字列）
    rows = _apply_entity_mapping(rows, cols, mapping or {})
    prefix = otype.pk.split("_")[0]
    if otype.kind == "event":
        rows = sorted(rows, key=lambda r: tuple(str(v) for v in r))
        keys = _event_proxy_keys(rows, prefix)
    elif otype.pk == otype.name_property:
        keys = [r[0] for r in rows]          # 自引用自然键，直通不重映射
    else:
        # 实体型：归并后变体行折叠为同一 canonical，按 name_property 去重
        seen: set = set()
        deduped = []
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            deduped.append(r)
        rows = deduped
        proxy = _proxy_keys(sorted({r[0] for r in rows}), prefix)
        keys = [proxy[r[0]] for r in rows]
    return cols, rows, keys, src_table


def _object_row_values(otype: ObjectType, same: bool, rest_cols: list[str],
                       cols: list[str], row, key, src_table: str) -> list:
    """组装一行 obj_* 写入值（列序：pk[, name_property], *rest, source_rows）。"""
    raw, *props = row
    if otype.kind == "event":
        src = [f"{src_table}:"
               + ",".join(f"{c}={v}" for c, v in zip(cols, row))]
    else:
        src = [f"{src_table}:{otype.name_property}={raw}"]
    if same:
        return [key, *props, json_dumps(src)]
    return [key, raw, *props, json_dumps(src)]


def _insert_object_rows(conn, otype: ObjectType, same: bool, rest_cols: list[str],
                        cols: list[str], rows: list, keys: list[str],
                        src_table: str, target: str | None = None,
                        *, chunk: int = 500) -> None:
    """批量 INSERT（全量 build 写入 obj_*；增量路径写入同构暂存表）。

    target 默认 obj_<name>；每行占位符数 = pk + (name_property?) + rest + source_rows。
    分块多行 VALUES（10 万行场景比逐行 execute 快两个数量级，语义不变）。
    """
    target = target or f"obj_{otype.name}"
    n_cols = (len(rest_cols) + 2) if same else (len(rest_cols) + 3)
    all_vals: list = []
    for key, r in zip(keys, rows):
        all_vals.extend(_object_row_values(otype, same, rest_cols, cols, r, key, src_table))
    for start in range(0, len(rows), chunk):
        size = min(chunk, len(rows) - start)
        ph = ", ".join(["(" + ", ".join(["?"] * n_cols) + ")"] * size)
        conn.execute(
            f"INSERT INTO {target} VALUES {ph}",
            all_vals[start * n_cols:(start + size) * n_cols])


def _ensure_obj_table(conn, otype: ObjectType, same: bool,
                      rest_cols: list[str]) -> None:
    """增量路径：obj_* 表不存在时按声明建空表（存在则不动）。"""
    defs = [f'"{otype.pk}" VARCHAR']
    if not same:
        t = otype.properties.get(otype.name_property, "string")
        defs.append(f'"{otype.name_property}" {TYPE_SQL[t]}')
    for c in rest_cols:
        t = otype.properties.get(c, "string")
        defs.append(f'"{c}" {TYPE_SQL[t]}')
    defs.append('"source_rows" VARCHAR')
    conn.execute(f'CREATE TABLE IF NOT EXISTS obj_{otype.name} ({", ".join(defs)})')


def _diff_apply(conn, target: str, stage: str) -> tuple[int, int]:
    """通用行级 diff：stage（新结果集，列包含 target 全部列）→ target。

    全行比对（IS NOT DISTINCT FROM，NULL 安全）：
      删除 target 中在 stage 不存在的行（消失/变更），
      插入 stage 中在 target 不存在的行（新增/变更）。
    未变化行不重写——增量场景 10 万全量 + 100 新行只写约 100 行。
    返回 (inserted, deleted)。
    """
    cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info('{target}')").fetchall()]
    # stage 可能是 TEMP TABLE：用 result description 取列名，跨表类型最稳
    stage_cols = {d[0] for d in conn.execute(
        f"SELECT * FROM {stage} LIMIT 0").description}
    missing = [c for c in cols if c not in stage_cols]
    if missing:
        raise ValueError(
            f"增量物化 {target}：新结果集缺列 {missing}（build_sql/source_sql 输出与声明不一致）")

    def cond(left: str, right: str) -> str:
        return " AND ".join(
            f'{left}."{c}" IS NOT DISTINCT FROM {right}."{c}"' for c in cols)

    deleted = conn.execute(
        f"SELECT COUNT(*) FROM {target} t WHERE NOT EXISTS "
        f"(SELECT 1 FROM {stage} s WHERE {cond('t', 's')})"
    ).fetchone()[0]
    inserted = conn.execute(
        f"SELECT COUNT(*) FROM {stage} s WHERE NOT EXISTS "
        f"(SELECT 1 FROM {target} t WHERE {cond('t', 's')})"
    ).fetchone()[0]
    conn.execute(
        f"DELETE FROM {target} WHERE NOT EXISTS "
        f"(SELECT 1 FROM {stage} s WHERE {cond(target, 's')})")
    col_list = ", ".join(f'"{c}"' for c in cols)
    conn.execute(
        f"INSERT INTO {target} ({col_list}) "
        f"SELECT {col_list} FROM {stage} s WHERE NOT EXISTS "
        f"(SELECT 1 FROM {target} t WHERE {cond('t', 's')})")
    return inserted, deleted


# ----------------------------------------------------------------------
# 增量重建（REQ-004）：只重物化 plan 命中的对象/链接，行级 diff
# ----------------------------------------------------------------------
_STAGE = "_rebuild_stage"


def materialize_changed(conn, plan, *, pack: str = "default",
                        bus=None, actor: str = "system") -> dict:
    """按 RebuildPlan 增量物化语义层，返回统计（含 rewritten_rows）。

    build_ontology() 保留为 bootstrap 全量；日常增量只调本函数：
      对象先于链接（链接 build_sql 引用 obj_*）；未变化行经 _diff_apply 跳过；
      完成后推进版本时钟（is_current=true）并发布 ontology.materialized 事件。
    entity_mapping（review accept 的 variant→canonical）是受保护表：
      编译器只重建 obj_*/lnk_*，归并结果在全量/增量重建后均生效且不被清除（REQ-016）。
    """
    from core.ontology_loader import load_pack
    from core.ontology_version import compute_version, record_version
    spec = load_pack(pack)
    org_names = _default_org_names(conn)
    entity_mapping = _load_entity_mapping(conn)   # REQ-016 受保护归并映射
    stats: dict = {"objects": {}, "links": {}, "skipped": [], "dirty": [], "degraded": [],
                   "rewritten_rows": 0, "plan_mode": plan.mode}

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(f"DROP TABLE IF EXISTS {_STAGE}")
        snap_batches = []   # REQ-008：(obj_type, dataset, cols, rows) 与 source_rows 同源
        # ---- 1) 对象：重算 binding → 暂存 → 行级 diff ----
        for name in plan.affected_objects:
            otype = next((o for o in spec.objects if o.name == name), None)
            if otype is None or otype.runtime:
                continue
            b = spec.object_bindings.get(name)
            if b is None:
                continue
            computed = _compute_object_rows(conn, otype, b, org_names, stats,
                                            mapping=entity_mapping)
            if computed is None:
                continue
            cols, rows, keys, src_table = computed
            rest_cols = [c for c in cols if c != otype.name_property]
            same = otype.pk == otype.name_property
            _ensure_obj_table(conn, otype, same, rest_cols)
            # 暂存新结果集（与目标表同构），复用全量插入口径
            conn.execute(f"CREATE TEMP TABLE {_STAGE} AS "
                         f"SELECT * FROM obj_{name} WHERE 1=0")
            _insert_object_rows(conn, otype, same, rest_cols, cols,
                                rows, keys, src_table, target=_STAGE)
            ins, dele = _diff_apply(conn, f"obj_{name}", _STAGE)
            conn.execute(f"DROP TABLE {_STAGE}")
            stats["objects"][name] = conn.execute(
                f"SELECT COUNT(*) FROM obj_{name}").fetchone()[0]
            stats["rewritten_rows"] += ins + dele
            if src_table:
                snap_batches.append((name, src_table, cols, rows))

        # ---- 2) 链接：重算 build_sql → 暂存 → 行级 diff ----
        for name in plan.affected_links:
            ltype = next((l for l in spec.links if l.name == name), None)
            if ltype is None or ltype.runtime:
                continue
            lb = spec.link_bindings.get(name)
            if lb is None:
                continue
            target = f"lnk_{name}"
            if not _table_exists(conn, target):
                # 从未全量 build 过：按 build_sql 直接建表
                try:
                    conn.execute(f"CREATE TABLE {target} AS {lb.build_sql}")
                    stats["links"][name] = conn.execute(
                        f"SELECT COUNT(*) FROM {target}").fetchone()[0]
                    stats["rewritten_rows"] += stats["links"][name]
                    continue
                except Exception as e:
                    stats["skipped"].append(
                        f"lnk_{name}(增量编译失败已跳过: {type(e).__name__})")
                    continue
            cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info('{target}')").fetchall()]
            col_list = ", ".join(f'"{c}"' for c in cols)
            try:
                conn.execute(
                    f"CREATE TEMP TABLE {_STAGE} AS "
                    f"SELECT {col_list} FROM ({lb.build_sql})")
            except Exception as e:
                stats["skipped"].append(
                    f"lnk_{name}(增量编译失败已跳过: {type(e).__name__})")
                conn.execute(f"DROP TABLE IF EXISTS {_STAGE}")
                continue
            ins, dele = _diff_apply(conn, target, _STAGE)
            conn.execute(f"DROP TABLE {_STAGE}")
            stats["links"][name] = conn.execute(
                f"SELECT COUNT(*) FROM {target}").fetchone()[0]
            stats["rewritten_rows"] += ins + dele

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute(f"DROP TABLE IF EXISTS {_STAGE}")
        raise

    # ---- 3) 推进版本时钟 ----
    from core.ontology_version import current_version
    prev = current_version(conn, pack)   # 记录新版本前的上一版（REQ-008 索引用）
    ver = compute_version(conn, pack, spec)
    record_version(conn, ver)
    stats["build_id"] = ver.build_id

    # ---- 3.5) REQ-008：源行内容寻址归档（增量；按对象类型粒度继承上版本索引）----
    if snap_batches:
        from core.row_uri import snapshot_source_rows
        stats["row_snapshot"] = snapshot_source_rows(
            conn, ver.build_id, snap_batches,
            partition=plan.partition or "incremental",
            prev_build_id=prev.build_id if prev else None)

    # ---- 4) 事件：物化完成（空影响集不产生事件，见 plan mode=skip）----
    if bus is not None:
        bus.publish("ontology.materialized", {
            "pack": pack,
            "build_id": ver.build_id,
            "reason": plan.reason,
            "mode": plan.mode,
            "affected_objects": plan.affected_objects,
            "affected_links": plan.affected_links,
            "affected_rules": plan.affected_rules,
            "rewritten_rows": stats["rewritten_rows"],
            "source_watermark": ver.source_watermark,
        }, actor=actor)
    return stats


def rebuild_from_partition(conn, part, *, pack: str = "default",
                           bus=None, actor: str = "system",
                           batch_threshold: int | None = None):
    """分区到达的完整增量链路：影响范围（REQ-018）→ 增量物化（REQ-004）。

    返回 (plan, stats)。空影响集（mode=skip）不物化、不产生事件。
    """
    from core.rebuild_planner import plan_from_partition
    kw = {} if batch_threshold is None else {"batch_threshold": batch_threshold}
    plan = plan_from_partition(conn, part, pack=pack, **kw)
    if plan.mode == "skip" or plan.is_empty():
        return plan, {"objects": {}, "links": {}, "skipped": [],
                      "rewritten_rows": 0, "plan_mode": "skip"}
    stats = materialize_changed(conn, plan, pack=pack, bus=bus, actor=actor)
    return plan, stats


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
