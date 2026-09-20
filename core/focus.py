"""
core/focus.py
靶心推导器（镜头必填参数的自动填充源）——确定性、可审计、无人工输入。

解决的问题：
  定向镜头（relation_* / timeline_*）声明了 required 参数（target_subject /
  subject_a+subject_b / project），批量调度阶段因「必填缺失会硬失败」被整体
  跳过，导致自动研判缺一块。此前只能靠人填参数，与"自动研判"目标相悖。

设计（声明式，符合本项目「声明是数据、实现是代码」）：
  镜头在 pack.json 的 params_schema 里声明 auto_from：
      "target_subject": {"type":"string","required":true,
                         "auto_from":{"source":"focus_subjects","rank_by":"evidence_count","max":5}}
  本模块按声明推导取值——推导逻辑只此一处，镜头包不含业务逻辑。

三级靶心源（优先级递减，全部确定性、同输入同输出）：
  1. case_knowledge.focus_subjects  案件显式声明（正兵锁定靶心，最高优先）
  2. clue_subjects                  本轮线索反推，按证据数排序（线索驱动）
  3. semantic_table                 语义表枚举，按度数排序截断（兜底）

组合爆炸防护（关键）：
  双主体镜头（relation_paths / relation_common_neighbors）若做全组合是 O(N²)
  ——N=200 时 19900 次调用。故双参一律走「靶心 × 关联主体」O(N)，且每镜头
  产出的参数组合数受 max 封顶。

红线不变：
  - 靶心只回答「查谁」，不回答「谁有问题」——不出定性结论；
  - 推导不出靶心 → 落健康度诊断标缺口，不静默跳过、不造靶心；
  - 每次自动填充都记 param_source 进线索 detail，全程可审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 线索 source_rows 中承载主体名的字段。
#
# 主口径改读**本体声明**：对象的 name_property + 标注 semantic:subject_name
# 的属性（load_semantic_roles）。见 _subject_keys()。
#
# 下面是**兜底**——仅保留结构化字段名（非中文业务列名）。此前这里的
# ("主体","对方","资金主体","法人"…) 是中文业务列名的猜测词表，换领域即失效
# 且不报错（本体无关化病根之一）。输出规范化后源表行应输出语义属性名，
# 中文兜底随之移除。
_SUBJECT_KEYS = (
    "raw_name", "person_1", "person_2", "owner_raw", "subject_raw",
)
# 主体值为列表的字段
_SUBJECT_LIST_KEYS = ("matched_person",)

# 语义表 → (类型名, 名称列)：兜底枚举源
#
# 本体无关（方案 B）：不再硬编码 person/org/bid_project，改为运行时从
# ontology objects.json 读「实体型(kind=entity)」类型及其 name_property。
# 换本体（如金融领域 fund/manager/listing）自动生效，无需改代码。
# 缓存键为 pack，装载失败回落空元组（不阻断，走其余靶心源）。
_ONTOLOGY_SOURCE_CACHE: dict[tuple[str, str | None], tuple[tuple[str, str, str], ...]] = {}

# 系统/运行期对象：不适合做「查谁」的靶心（即便 kind=entity 也排除）。
# 与业务主体区分——它们不是侦查对象，是侦查产物。
_SYSTEM_OBJECT_HINTS = ("clue", "decision", "image_evidence", "verify_item",
                        "task", "proposal", "review", "audit", "lens_run")


def _ontology_entity_sources(pack: str = "default",
                             base_dir=None) -> tuple[tuple[str, str, str], ...]:
    """从本体声明取「实体型」枚举源：(语义表名, 类型名, name_property)。

    这正是此前硬编码三元组所绕过的信息——objects.json 早就声明了
    kind(entity/event) 与 name_property，直接读即可本体无关。
    """
    key = (pack, str(base_dir) if base_dir else None)
    if key in _ONTOLOGY_SOURCE_CACHE:
        return _ONTOLOGY_SOURCE_CACHE[key]
    out: list[tuple[str, str, str]] = []
    try:
        from core.ontology_loader import load_pack
        spec = load_pack(pack, base_dir=base_dir)
        for o in getattr(spec, "objects", None) or []:
            if getattr(o, "kind", None) != "entity":
                continue
            if getattr(o, "runtime", False):
                continue
            name = getattr(o, "name", "")
            prop = getattr(o, "name_property", "") or ""
            if not name or not prop:
                continue
            if any(h in name.lower() for h in _SYSTEM_OBJECT_HINTS):
                continue
            out.append((f"obj_{name}", name, prop))
    except Exception:
        out = []
    result = tuple(out)
    _ONTOLOGY_SOURCE_CACHE[key] = result
    return result


def _sources_for(pack: str, object_types: list[str] | None,
                 base_dir=None) -> tuple[tuple[str, str, str], ...]:
    """按镜头包声明取枚举源。

    方案 B：镜头包在 auto_from 里声明 `object_types: ["person","org"]`
    ——声明跟着镜头走，不动本体 schema。
    声明缺省 → 按本体的 kind=entity 自动推导兜底（新镜头包不必写全）。
    """
    all_src = _ontology_entity_sources(pack, base_dir)
    if not object_types:
        return all_src
    wanted = {str(t) for t in object_types}
    picked = tuple(s for s in all_src if s[1] in wanted)
    # 声明的类型在本体里不存在（换本体/写错）→ 回落全量实体型，避免静默空集
    if not picked:
        return all_src
    return picked


# 项目类参数源：不再写死 obj_bid_project.title
# 由 auto_from.project_object_type 声明（缺省回落实体型中名称列非 id 的候选）。
_PROJECT_SOURCES: tuple[tuple[str, str], ...] = ()

_DEFAULT_MAX = 5


@dataclass(frozen=True)
class FocusSubject:
    """一个推导出的靶心主体。"""
    name: str
    type: str = "auto"          # person/org/bid_project/...
    score: float = 0.0          # 排序分（证据数/度数）
    source: str = ""            # case_knowledge / clue_subjects / semantic_table
    evidence: int = 0

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "score": self.score,
                "source": self.source, "evidence": self.evidence}


def _rows(store, sql: str) -> list[dict]:
    """只读查询，兼容 Store / ReadOnlyStore / 裸 duckdb 连接。"""
    import duckdb
    if isinstance(store, duckdb.DuckDBPyConnection):
        cur = store.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    q = getattr(store, "query", None)
    if callable(q):
        return q(sql)
    cur = store.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _table_exists(store, table: str) -> bool:
    try:
        r = _rows(store, "SELECT COUNT(*) AS c FROM information_schema.tables "
                         f"WHERE table_name = '{table}'")
        return bool(r) and int(r[0].get("c") or 0) > 0
    except Exception:
        return False


# ----------------------------------------------------------------------
# 一级源：案件知识包显式声明
# ----------------------------------------------------------------------

def _case_knowledge(pack: str = "default", base_dir=None) -> dict:
    """读取案件知识包（缺失 → 空骨架，不阻断）。

    注：案件知识包不在 load_pack() 的八段里，独立装载器是
    core.functions.load_case_knowledge（R5 org_interest_links 同款入口）——
    复用它保证版本校验（REQ-G-016）与快照基目录口径一致。
    """
    try:
        from core.functions import load_case_knowledge
        return load_case_knowledge(pack, base_dir) or {}
    except Exception:
        return {}


def _from_case_knowledge(store, pack: str = "default",
                         base_dir=None) -> list[FocusSubject]:
    """1) case_knowledge.focus_subjects —— 正兵显式锁定靶心，最高优先级。"""
    raw = _case_knowledge(pack, base_dir).get("focus_subjects")
    if not raw:
        return []
    out: list[FocusSubject] = []
    for item in raw:
        if isinstance(item, str):
            out.append(FocusSubject(name=item, type="auto", score=1000.0,
                                    source="case_knowledge", evidence=0))
        elif isinstance(item, dict) and item.get("name"):
            out.append(FocusSubject(
                name=str(item["name"]), type=str(item.get("type") or "auto"),
                score=1000.0, source="case_knowledge",
                evidence=int(item.get("evidence") or 0)))
    return out


def _from_case_aliases(pack: str = "default",
                       base_dir=None) -> list[FocusSubject]:
    """2) case_knowledge.subject_aliases —— 案件已登记的主体（人/关系人）。

    案件包既然登记了「张卫国/李志强/李志强妻弟」，这些就是本案的对象，
    比"哪行数据多"更能代表靶心；按声明序给递减分（声明序即重要性）。
    """
    aliases = _case_knowledge(pack, base_dir).get("subject_aliases") or {}
    if not isinstance(aliases, dict):
        return []
    return [FocusSubject(name=str(k), type="auto", score=900.0 - i,
                         source="case_aliases", evidence=0)
            for i, k in enumerate(aliases.keys())]


# ----------------------------------------------------------------------
# 二级源：前序线索反推（线索驱动）
# ----------------------------------------------------------------------

def _subject_keys(pack: str = "default", base_dir=None) -> tuple[str, ...]:
    """线索 source_rows 中承载主体名的字段集合（本体无关）。

    两部分：
      - 本体声明的 name_property（raw_name/from_raw/title…）——换本体自动跟上；
      - 本体标注 semantic:subject_name 的属性（load_semantic_roles）
        ——显式声明的主体列，换领域由本体负责；
      - 少量结构化字段名兜底（_SUBJECT_KEYS）——非中文业务列名，跨领域稳定。

    中文业务列名（"主体"/"对方"/"资金主体"/"法人"…）**已移除**：那是靠猜，
    换领域即失效。源表行应输出语义属性名（见 Function SQL 规范化），
    否则请在本体补声明，而不是往这个词表里加中文。
    """
    props: set[str] = set()
    try:
        from core.ontology_loader import load_pack
        for o in getattr(load_pack(pack, base_dir=base_dir), "objects", None) or []:
            p = getattr(o, "name_property", "") or ""
            if p:
                props.add(p)
    except Exception:
        pass
    try:
        from core.ontology_loader import load_semantic_roles
        props.update(load_semantic_roles(pack, base_dir).get("subject_name") or ())
    except Exception:
        pass
    return tuple(sorted(props | set(_SUBJECT_KEYS)))


def _from_clues(clues: Iterable[Any] | None, pack: str = "default",
                base_dir=None) -> list[FocusSubject]:
    """从线索的 source_rows 反推主体，按证据出现次数排序。本体无关。"""
    if not clues:
        return []
    keys = _subject_keys(pack, base_dir)
    counts: dict[str, int] = {}
    for c in clues:
        rows = getattr(c, "source_rows", None) or []
        for sr in rows:
            if not isinstance(sr, dict):
                continue
            for k in keys:
                v = sr.get(k)
                if isinstance(v, str) and v.strip():
                    counts[v.strip()] = counts.get(v.strip(), 0) + 1
            for k in _SUBJECT_LIST_KEYS:
                v = sr.get(k)
                if isinstance(v, (list, tuple)):
                    for x in v:
                        xs = str(x).strip()
                        if xs:
                            counts[xs] = counts.get(xs, 0) + 1
    return [FocusSubject(name=n, type="auto", score=float(c),
                         source="clue_subjects", evidence=c)
            for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ----------------------------------------------------------------------
# 三级源：语义表枚举（兜底，按度数排序）
# ----------------------------------------------------------------------

def _from_semantic_table(store, limit: int, pack: str = "default",
                         object_types: list[str] | None = None,
                         base_dir=None) -> list[FocusSubject]:
    """语义表枚举兜底：按出现次数（代理度数）排序截断。

    本体无关：枚举源取自 ontology objects.json 的实体型类型（kind=entity
    + name_property），而非硬编码 person/org/bid_project。换本体自动生效。
    object_types 为镜头包 auto_from 的显式声明，缺省则用全量实体型。
    """
    counts: dict[tuple[str, str], int] = {}
    for table, tname, col in _sources_for(pack, object_types, base_dir):
        if not _table_exists(store, table):
            continue
        try:
            rows = _rows(store, f'SELECT "{col}" AS n, COUNT(*) AS c '
                                f'FROM "{table}" WHERE "{col}" IS NOT NULL '
                                f'GROUP BY "{col}"')
        except Exception:
            continue
        for r in rows:
            n = str(r.get("n") or "").strip()
            if n:
                key = (n, tname)
                counts[key] = counts.get(key, 0) + int(r.get("c") or 0)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))
    return [FocusSubject(name=n, type=t, score=float(c),
                         source="semantic_table", evidence=c)
            for (n, t), c in ranked[:limit]]


def _probe_type(store, name: str, pack: str = "default",
                object_types: list[str] | None = None,
                base_dir=None) -> str:
    """探测主体在语义层中的实体类型（确定性，供同名消歧用）。

    core.graph.resolve_subject 在「多类实体同名」时要求显式 target_type 消歧
    （如 张卫国 同时存在于 obj_person 与 obj_account）。自动填充必须给出真实
    类型，否则定向镜头会撞歧义 ValueError → 被 skill_invoke 隔离成 0 线索。

    本体无关（方案 B）：探测顺序**由本体声明顺序决定**，不写死
    person/org/bid_project。此前的硬编码顺序在换本体后会把「华夏成长」
    （基金名）误判为 person——因为它压根没查过 obj_fund。
    现在：先在语义层逐类型实查（真实归属优先），查不到才回落形态判定。
    """
    # ① 语义层实际归属优先：按本体声明的枚举源逐个实查
    for table, tname, col in _sources_for(pack, object_types, base_dir):
        if _in_table(store, table, col, name):
            return tname

    # ② 事件型（kind=event）也可能承载主体名（如 transaction.from_raw），
    #    一并进行实查，避免跨本体漏判
    try:
        from core.ontology_loader import load_pack
        for o in getattr(load_pack(pack, base_dir=base_dir), "objects", None) or []:
            if getattr(o, "kind", None) != "event":
                continue
            prop = getattr(o, "name_property", "") or ""
            if not prop:
                continue
            if _in_table(store, f"obj_{o.name}", prop, name):
                return o.name
    except Exception:
        pass

    # ③ 兜底：名称形态判定（纯形态，不含领域词表假设时的最后手段）
    #    收窄到单一类型可让 resolve_subject 干净返回 None，而不是撞同名歧义
    #    ValueError 把整个镜头隔离成 0 线索。
    try:
        from core.entity import classify_entity_type
        shape = classify_entity_type(name)
    except Exception:
        shape = "unknown"
    if shape in ("person", "org"):
        return shape
    return "auto"


def _esc(s: str) -> str:
    """单引号转义（主体名来自语义层声明，非外部输入，仅防自拼接破坏）。"""
    return str(s).replace("'", "''")


def _in_table(store, table: str, col: str, name: str) -> bool:
    if not _table_exists(store, table):
        return False
    try:
        r = _rows(store, f'SELECT COUNT(*) AS c FROM "{table}" '
                         f'WHERE "{col}" = \'{_esc(name)}\'')
        return bool(r) and int(r[0].get("c") or 0) > 0
    except Exception:
        return False


def _projects(store, limit: int, pack: str = "default",
              project_object_type: str | None = None,
              base_dir=None) -> list[FocusSubject]:
    """项目类靶心（如 timeline_cross_collision.project）。

    本体无关：项目类型由镜头包 auto_from.project_object_type 声明
    （缺省 bid_project；该类型在本体里不存在时回落到实体型中「名称列不是
     id 列」的候选——项目类名称多为业务名而非代理键）。
    """
    ptype = project_object_type or "bid_project"
    srcs = _sources_for(pack, [ptype], base_dir)
    if not srcs or srcs[0][1] != ptype:
        # 声明的类型不在本体里（换本体）→ 回落实体型中名称列非 id 的候选
        cands = [s for s in _sources_for(pack, None, base_dir)
                 if not str(s[2]).endswith("_id")]
        srcs = cands
    out: list[FocusSubject] = []
    for table, tname, col in srcs:
        if not _table_exists(store, table):
            continue
        try:
            rows = _rows(store, f'SELECT "{col}" AS n, COUNT(*) AS c '
                                f'FROM "{table}" WHERE "{col}" IS NOT NULL '
                                f'GROUP BY "{col}" ORDER BY c DESC, n')
        except Exception:
            continue
        for r in rows:
            n = str(r.get("n") or "").strip()
            if n:
                out.append(FocusSubject(name=n, type=tname,
                                        score=float(r.get("c") or 0),
                                        source="semantic_table",
                                        evidence=int(r.get("c") or 0)))
    return out[:limit]


# ----------------------------------------------------------------------
# 推导入口
# ----------------------------------------------------------------------

def resolve_focus(store, ctx: dict | None = None, pack: str = "default",
                  limit: int = _DEFAULT_MAX, health=None,
                  base_dir=None,
                  object_types: list[str] | None = None) -> list[FocusSubject]:
    """三级靶心推导，返回去重保序的靶心列表（优先级递减）。

    四级靶心源（优先级递减，上一級命中即不再取下一级）：
      1. case_knowledge.focus_subjects  正兵显式锁定（最高）
      2. case_knowledge.subject_aliases 案件已登记主体
      3. clue_subjects                  前序线索反推（按证据数）
      4. semantic_table                 语义表枚举兜底（按行数）

    全部为空 → 返回 [] 并落健康度诊断（不静默跳过）。
    """
    ctx = ctx or {}
    out: list[FocusSubject] = []

    out.extend(_from_case_knowledge(store, pack, base_dir=base_dir))
    if not out:
        out.extend(_from_case_aliases(pack, base_dir))
    if not out:
        out.extend(_from_clues(ctx.get("clues"), pack, base_dir))
    if not out:
        out.extend(_from_semantic_table(store, limit, pack,
                                        object_types, base_dir))

    # 去重保序（同名保留优先级高的）
    seen: set[str] = set()
    uniq: list[FocusSubject] = []
    for f in out:
        if f.name in seen:
            continue
        seen.add(f.name)
        uniq.append(f)
    uniq = uniq[:limit]

    # 补真实实体类型：auto 会让 resolve_subject 在同名多类时抛歧义
    # ValueError → 镜头被隔离成 0 线索。逐主体探测语义层实际归属。
    if store is not None:
        uniq = [FocusSubject(name=f.name,
                             type=(f.type if f.type != "auto"
                                   else _probe_type(store, f.name, pack,
                                                    object_types, base_dir)),
                             score=f.score, source=f.source, evidence=f.evidence)
                for f in uniq]

    if not uniq:
        _record_gap(health, "未能推导任何靶心主体（四级源均为空）")
    return uniq


def resolve_projects(store, pack: str = "default", limit: int = _DEFAULT_MAX,
                     health=None, project_object_type: str | None = None,
                     base_dir=None) -> list[FocusSubject]:
    """项目类靶心推导（时间碰撞锚点）。本体无关：项目类型由声明指定。"""
    out = _projects(store, limit, pack, project_object_type, base_dir)
    if not out:
        _record_gap(health, "未能推导项目锚点（项目类语义表为空或缺失）")
    return out


def _record_gap(health, reason: str) -> None:
    """推导不出靶心 → 落诊断标缺口（不静默跳过）。"""
    if health is None:
        return
    try:
        health.record("focus_unresolved", "warning", source="focus",
                      reason=reason)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 定向镜头参数候选（画布定向调度用，规模可控）
# ----------------------------------------------------------------------
# 设计要点（规模视角）：
#   语义层全量枚举在真实案件可达 2.5 万主体（实测 89ms 查完，但前端下拉
#   渲染 2.5 万项会 DOM 爆炸/卡顿）。故候选**不以全量枚举为主**，而是按
#   「画布上下文 > 案件登记 > 语义层（仅按需搜索）」三级收窄：
#     ① 画布选中节点   0-1 个   → 静默带入，不展示
#     ② 画布可见节点   20-80 个 → 下拉列全（总量常 <100，无需虚拟滚动）
#     ③ 案件登记主体   3-50 个  → 补充进下拉
#     ④ 语义层全量     可达数万 → 不进下拉，仅按需搜索（search param）
#   规模越小用户越省事：画布只有 1 个主体时点开镜头即可直接跑，零填写。

# 下拉可直列的上限（超出降级为搜索框 + 推荐置顶）
LISTABLE_LIMIT = 100


class ParamCandidates:
    """单个参数的候选集（供前端决定呈现方式）。"""

    def __init__(self, param: str, candidates: list[FocusSubject],
                 recommended: FocusSubject | None, source: str = "",
                 searchable: bool = False):
        self.param = param
        self.candidates = candidates
        self.recommended = recommended
        self.source = source
        self.searchable = searchable

    @property
    def count(self) -> int:
        return len(self.candidates)

    @property
    def auto_only(self) -> bool:
        """候选唯一 → 前端静默带入，不展示（用户零填写）。"""
        return self.count == 1

    @property
    def listable(self) -> bool:
        """候选数在可直列范围内 → 下拉（默认选中推荐值）。"""
        return 0 < self.count <= LISTABLE_LIMIT

    def to_dict(self) -> dict:
        return {
            "param": self.param,
            "candidates": [c.to_dict() for c in self.candidates],
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "source": self.source,
            "count": self.count,
            "auto_only": self.auto_only,
            "listable": self.listable,
            "searchable": self.searchable,
        }


def _search_semantic(store, keyword: str, limit: int = 20,
                     pack: str = "default",
                     object_types: list[str] | None = None,
                     base_dir=None) -> list[FocusSubject]:
    """语义层按需搜索（候选过多时的兜底，不进下拉）。本体无关。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    out: list[FocusSubject] = []
    seen: set[str] = set()
    for table, tname, col in _sources_for(pack, object_types, base_dir):
        if not _table_exists(store, table):
            continue
        try:
            rows = _rows(store,
                         f'SELECT "{col}" AS n, COUNT(*) AS c FROM "{table}" '
                         f'WHERE "{col}" IS NOT NULL AND "{col}" '
                         f"LIKE '%{_esc(kw)}%' GROUP BY \"{col}\" "
                         f"ORDER BY c DESC, n LIMIT {int(limit)}")
        except Exception:
            continue
        for r in rows:
            n = str(r.get("n") or "").strip()
            if n and n not in seen:
                seen.add(n)
                out.append(FocusSubject(
                    name=n, type=tname,
                    score=float(r.get("c") or 0),
                    source="semantic_search", evidence=int(r.get("c") or 0)))
    return out[:limit]


def resolve_param_candidates(spec, store, *, canvas_nodes=None,
                             selected_node=None, ctx=None, pack="default",
                             keyword=None, base_dir=None) -> dict[str, ParamCandidates]:
    """推导镜头各必填参数的候选（规模可控，供画布定向调度）。

    canvas_nodes  : 画布当前可见主体名列表（规模主力，典型 20-80）
    selected_node : 画布选中主体（最高优先，唯一时静默带入）
    keyword       : 候选过多时的按需搜索词

    返回 {参数名: ParamCandidates}；无候选的参数也返回（count=0，
    searchable=True），前端据此只展示手填/搜索框，不做静默跳过。
    """
    ctx = dict(ctx or {})
    schema = spec.params_schema or {}
    required = [k for k, v in schema.items() if (v or {}).get("required")]

    # 项目类参数与主体类参数分开取候选（避免人名被灌进 project）
    proj_keys = [k for k in required
                 if ((schema[k] or {}).get("auto_from") or {}).get("source") == "projects"]
    subj_keys = [k for k in required if k not in proj_keys]

    # 本体无关（方案 B）：候选类型范围由镜头包声明，缺省按本体 kind=entity 推导
    obj_types = _declared_object_types(
        {k: (schema[k] or {}).get("auto_from") for k in subj_keys})

    out: dict[str, ParamCandidates] = {}

    # ---- 主体类参数 ----
    if subj_keys:
        ranked = _ranked_subjects(store, canvas_nodes, selected_node, ctx, pack,
                                  object_types=obj_types, base_dir=base_dir)
        for i, k in enumerate(subj_keys):
            # 第二主体（subject_b）从靶心顺位取，起点仍是首选靶心
            cands = ranked if i == 0 else ranked
            rec = cands[i] if i < len(cands) else (cands[0] if cands else None)
            src = (f"focus:{rec.source}#{i + 1}:{rec.name}" if rec else "")
            out[k] = ParamCandidates(k, cands, rec, src,
                                     searchable=True)

    # ---- 项目类参数 ----
    proj_type = _declared_project_type({k: (schema[k] or {}).get("auto_from")
                                        for k in proj_keys})
    for k in proj_keys:
        projs = resolve_projects(store, pack, LISTABLE_LIMIT,
                                 project_object_type=proj_type)
        rec = projs[0] if projs else None
        out[k] = ParamCandidates(
            k, projs, rec,
            f"focus:projects#1:{rec.name}" if rec else "", searchable=True)

    # ---- 按需搜索（候选超限时前端传 keyword 增量检索）----
    if keyword and subj_keys:
        hits = _search_semantic(store, keyword, 20, pack, obj_types)
        for k in subj_keys:
            pc = out.get(k)
            if pc:
                merged = list(hits) + [c for c in pc.candidates
                                       if c.name not in {h.name for h in hits}]
                out[k] = ParamCandidates(k, merged[:LISTABLE_LIMIT],
                                         (hits or merged)[:1][0] if merged else None,
                                         pc.source, searchable=True)
    return out


def _ranked_subjects(store, canvas_nodes, selected_node, ctx, pack,
                     object_types: list[str] | None = None,
                     base_dir=None) -> list[FocusSubject]:
    """主体候选排序：选中 > 画布可见 > 案件登记 > 线索反推 > 语义层截断。

    注意：语义层全量枚举在此**截断**到 LISTABLE_LIMIT，不做全量返回——
    真实案件可达数万主体，全量进下拉会 DOM 爆炸；超出部分走按需搜索。

    本体无关：类型探测与语义层枚举都按本体声明走，不硬编码中文类型名。
    """
    out: list[FocusSubject] = []
    seen: set[str] = set()

    def _add(items, tag):
        for f in items:
            if f.name in seen:
                continue
            seen.add(f.name)
            out.append(FocusSubject(name=f.name,
                                    type=(f.type if f.type != "auto"
                                          else _probe_type(store, f.name, pack,
                                                           object_types, base_dir)),
                                    score=f.score, source=tag or f.source,
                                    evidence=f.evidence))

    # ① 画布选中（最高优先；唯一时前端静默带入）
    if selected_node:
        _add([FocusSubject(name=str(selected_node), type="auto",
                           score=10000.0, source="canvas_selected")],
             "canvas_selected")
    # ② 画布可见节点
    if canvas_nodes:
        _add([FocusSubject(name=str(n), type="auto", score=5000.0 - i,
                           source="canvas_visible")
              for i, n in enumerate(canvas_nodes)], "canvas_visible")
    # ③ 案件登记
    _add(_from_case_knowledge(store, pack), "case_knowledge")
    _add(_from_case_aliases(pack), "case_aliases")
    # ④ 线索反推
    _add(_from_clues(ctx.get("clues"), pack, base_dir), "clue_subjects")
    # ⑤ 语义层截断兜底（不做全量）
    if len(out) < LISTABLE_LIMIT:
        _add(_from_semantic_table(store, LISTABLE_LIMIT - len(out), pack,
                                  object_types, base_dir),
             "semantic_table")
    return out[:LISTABLE_LIMIT]

def _declared_object_types(declared: dict) -> list[str] | None:
    """取 auto_from.object_types 声明（主体类候选的本体类型白名单）。

    方案 B：声明跟着镜头包走，不动本体 schema。未声明 → None，
    由 _sources_for 回落到本体 kind=entity 全量实体型。
    """
    for a in declared.values():
        if isinstance(a, dict) and isinstance(a.get("object_types"), list):
            v = [str(t) for t in a["object_types"] if t]
            if v:
                return v
    return None


def _declared_project_type(declared: dict) -> str | None:
    """取 auto_from.project_object_type 声明（项目类候选的本体类型）。"""
    for a in declared.values():
        if isinstance(a, dict) and a.get("project_object_type"):
            return str(a["project_object_type"])
    return None


def auto_fill_params(spec, store, ctx: dict | None = None, pack: str = "default",
                     base_dir=None, health=None) -> tuple[list[dict], list[str]]:
    """按 params_schema.auto_from 声明，推导该镜头的参数组合。

    返回 (param_combos, sources)：
      - param_combos：参数字典列表。单主体镜头 1 组/靶心（受 max 封顶）；
        双主体镜头走「靶心 × 关联主体」O(N)，**不做全组合**；
      - sources：形如 "focus:clue_subjects#1"，随线索落 detail 可审计。

    无 auto_from 声明的必填参数 → 返回 ([], []) 并落诊断，交由调度器跳过。
    """
    ctx = dict(ctx or {})
    if health is not None:
        ctx["health"] = health
    schema = spec.params_schema or {}
    declared = {k: (v or {}).get("auto_from")
                for k, v in schema.items() if (v or {}).get("required")}
    if not declared:
        return [], []

    # 未声明 auto_from 的必填参数：无法自动填充
    missing = [k for k, a in declared.items() if not a]
    if missing:
        _record_gap(ctx.get("health"),
                    f"镜头 {spec.skill_id} 必填参数 {missing} 未声明 auto_from，无法自动填充")
        return [], []

    limit = _DEFAULT_MAX
    for a in declared.values():
        if isinstance(a, dict) and isinstance(a.get("max"), int):
            limit = min(limit, int(a["max"]))

    # 本体无关（方案 B）：枚举源类型由镜头包 auto_from 声明
    #   object_types        ——「查谁」的候选来自哪些本体类型
    #   project_object_type ——「哪个项目」的候选来自哪个本体类型
    # 声明缺省 → 按本体 kind=entity 自动推导兜底（新镜头包不必写全）。
    obj_types = _declared_object_types(declared)
    proj_type = _declared_project_type(declared)

    # 项目类参数（project）
    proj_keys = [k for k, a in declared.items()
                 if isinstance(a, dict) and a.get("source") == "projects"]
    subj_keys = [k for k in declared if k not in proj_keys]

    if proj_keys and not subj_keys:
        projs = resolve_projects(store, pack, limit, ctx.get("health"),
                                 project_object_type=proj_type,
                                 base_dir=base_dir)
        combos = []
        for i, p in enumerate(projs, 1):
            d = {k: p.name for k in proj_keys}
            d["_param_source"] = f"focus:projects#{i}:{p.name}"
            combos.append(d)
        return combos, [c["_param_source"] for c in combos]

    focus = resolve_focus(store, ctx, pack, max(limit, 2), ctx.get("health"),
                          base_dir, object_types=obj_types)

    if len(subj_keys) == 1:
        k = subj_keys[0]
        combos = []
        for i, f in enumerate(focus, 1):
            d = {k: f.name}
            if "target_type" in schema and not schema["target_type"].get("required"):
                d["target_type"] = f.type
            d["_param_source"] = f"focus:{f.source}#{i}:{f.name}"
            combos.append(d)
        return combos, [c["_param_source"] for c in combos]

    # 双主体：靶心 × 关联主体（O(N)，不做全组合 O(N²)）
    if len(subj_keys) >= 2 and len(focus) >= 2:
        ka, kb = subj_keys[0], subj_keys[1]
        head, rest = focus[0], focus[1:]
        combos = []
        for i, other in enumerate(rest, 1):
            d = {ka: head.name, kb: other.name}
            # 分体类型：同名多类实体（张卫国 同在 person/account）会让
            # resolve_subject 抛歧义 ValueError → 镜头被隔离成 0 线索。
            # 探测到的真实类型随参下发（仅当镜头声明了该参数）。
            for pkey, subj in (("target_type_a", head),
                               ("target_type_b", other)):
                if pkey in schema and subj.type != "auto":
                    d[pkey] = subj.type
            d["_param_source"] = (f"focus:pair#{i}:{head.name}×{other.name}"
                                  f"（源 {head.source}/{other.source}）")
            combos.append(d)
        return combos, [c["_param_source"] for c in combos]

    _record_gap(ctx.get("health"),
                f"镜头 {spec.skill_id} 靶心不足（需 ≥2 主体，实得 {len(focus)}）")
    return [], []
