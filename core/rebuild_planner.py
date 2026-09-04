"""
core/rebuild_planner.py
受影响范围计算（REQ-018）。

输入：变更源（新到达分区 / 显式种子主键；review 决定由 REQ-016 经 plan_from_seeds 接入）
输出：RebuildPlan —— 受影响对象主键（种子 + 一跳邻域）、受影响链接、受影响规则、
      规模估计与执行模式（skip / incremental / batch）。

依赖来源（全部来自 ontology 声明，不硬编码业务表）：
  - 对象 ← 源表：bindings.json object_bindings（source_table，或 source_sql 中出现的表名）
  - 链接端点：links.json from_obj/to_obj；邻接行在 lnk_* 物化表中
  - 规则 ← 语义表：rules.json → functions.json 的 inputs 声明

防级联放大：
  - 只扩一跳邻域（one_hop_neighbors）；
  - 每条链接只处理一次（visited），环状 link 不无限扩散（AC4）；
  - 影响集规模预估超阈值 → mode="batch"，交批处理通道而非单次重建（AC2）；
  - 空影响集 → mode="skip"，不触发重建、不产生事件（AC5）。

性能（AC3）：全部走索引/半连接（IN 子查询），10 万行图 < 1s。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from core.ontology_loader import load_pack


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class RebuildPlan:
    """一次增量重建的影响范围与执行计划。"""
    reason: str                                   # "partition" | "seed" | "review"
    pack: str = "default"
    seed_pks: dict[str, set[str]] = field(default_factory=dict)      # 直接命中的对象主键
    affected_pks: dict[str, set[str]] = field(default_factory=dict)  # 含一跳邻域
    affected_objects: list[str] = field(default_factory=list)       # 需重物化的对象类型
    affected_links: list[str] = field(default_factory=list)         # 需重物化的链接
    affected_rules: list[str] = field(default_factory=list)         # 需重算的规则 id
    mode: str = "incremental"                     # skip | incremental | batch
    estimated_rows: int = 0                       # 预估重写行数
    one_hop_neighbors: bool = True
    partition: str = ""                           # REQ-008：触发分区（partition_id；seed/review 为空）
    elapsed_ms: float = 0.0

    def is_empty(self) -> bool:
        return not self.affected_pks and not self.affected_objects

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "pack": self.pack,
            "mode": self.mode,
            "estimated_rows": self.estimated_rows,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "seed_pks": {k: sorted(v) for k, v in self.seed_pks.items()},
            "affected_pks": {k: sorted(v) for k, v in self.affected_pks.items()},
            "affected_objects": self.affected_objects,
            "affected_links": self.affected_links,
            "affected_rules": self.affected_rules,
            "one_hop_neighbors": self.one_hop_neighbors,
            "partition": self.partition,
        }


# 规模阈值：预估重写行数超过它 → 批处理模式（AC2）
DEFAULT_BATCH_THRESHOLD = 5000


# ----------------------------------------------------------------------
# 声明级依赖：对象 ← 源表 / 链接端点 / 规则 ← 语义表
# ----------------------------------------------------------------------
def objects_for_dataset(spec, dataset: str) -> list[str]:
    """binding 直接消费该源表的对象类型（source_table 精确匹配，或表名出现在 source_sql）。"""
    out: list[str] = []
    for otype in spec.objects:
        if otype.runtime:
            continue
        b = spec.object_bindings.get(otype.name)
        if not b:
            continue
        if b.source_table == dataset or (dataset and dataset in (b.source_sql or "")):
            out.append(otype.name)
    return out


def _col_refs_object(col: str, obj: str, pk: str | None = None) -> bool:
    """列是否引用某对象的主键（约定与 bindings.json build_sql 输出一致）：

      - 列名等于对象 pk（person_id / txn_id / track_id ...）；
      - 列名以 _<对象名> 结尾（from_person / to_account / owner_person）；
      - 列名形如 <对象名>_<数字>（person_1 / person_2）；
      - 列名形如 <pk>_<数字>（track_id_1：pk 与对象名不同形时）；
      - 列名形如 <对象名>_id_<数字>。
    """
    if pk and col == pk:
        return True
    if col == f"{obj}_id":
        return True
    if re.fullmatch(rf"\w+_{obj}", col):          # from_person / to_account / owner_person
        return True
    if re.fullmatch(rf"{obj}_\d+", col):           # person_1 / person_2
        return True
    if pk and re.fullmatch(rf"{re.escape(pk)}_\d+", col):  # track_id_1 / track_id_2
        return True
    if re.fullmatch(rf"{obj}_id_\d+", col):
        return True
    return False


def _all_pk_columns(spec, columns: list[str]) -> dict[str, list[str]]:
    """表中所有"引用某对象主键"的列（不限于端点）——用于从受影响链接行回收事件外键。"""
    mapping: dict[str, list[str]] = {}
    for otype in spec.objects:
        for col in columns:
            if _col_refs_object(col, otype.name, otype.pk):
                mapping.setdefault(otype.name, []).append(col)
    return mapping


def rules_for_tables(spec, tables: set[str]) -> list[str]:
    """规则经其 function 的 inputs 声明消费语义表；表受影响 → 规则需重算。"""
    out: list[str] = []
    for rid, r in spec.rules.items():
        f = spec.functions.get(r.function)
        if f and set(f.inputs) & tables:
            out.append(rid)
    return sorted(out)


# ----------------------------------------------------------------------
# 规划入口
# ----------------------------------------------------------------------
def _link_referenced_objects(spec, ltype, lb) -> set[str]:
    """链接声明/物化 SQL 引用到的对象：端点 + build_sql 中出现的 obj_* 名。"""
    valid = {o.name for o in spec.objects}
    refs = {ltype.from_obj, ltype.to_obj}
    if lb is not None:
        refs |= set(re.findall(r"obj_([A-Za-z_]+)", lb.build_sql or ""))
    return refs & valid


def plan_from_seeds(conn, seeds: dict[str, set[str]], *,
                    pack: str = "default", reason: str = "seed",
                    batch_threshold: int = DEFAULT_BATCH_THRESHOLD,
                    include_objects: "list[str] | None" = None,
                    partition: str = "") -> RebuildPlan:
    """从显式种子主键出发计算影响范围（review 回边 / 人工触发用）。

    seeds: {对象名: {主键, ...}}，如 {"person": {"person_xxx"}}。
    include_objects: 声明级必受影响对象（即使尚无种子主键——如新数据产生的
        全新名字/事件尚未物化），其依赖链接一并纳入重物化。
    partition: REQ-008 源行归档的触发分区标识（plan_from_partition 传
        partition_id；seed/review 触发留空，归档侧回落 "incremental"）。
    """
    t0 = time.perf_counter()
    spec = load_pack(pack)
    valid_objs = {o.name for o in spec.objects}
    seed_pks: dict[str, set[str]] = {
        k: set(v) for k, v in seeds.items() if k in valid_objs and v
    }
    plan = RebuildPlan(reason=reason, pack=pack, seed_pks=seed_pks,
                       partition=partition)

    affected: dict[str, set[str]] = {k: set(v) for k, v in seed_pks.items()}
    for name in include_objects or []:
        if name in valid_objs:
            affected.setdefault(name, set())   # 声明受影响，主键集暂可为空

    affected_links: set[str] = set()
    touching_rows = 0

    # ---- 一跳邻域扩展：受影响对象 → 声明依赖链接 → 邻接行内其它主键 ----
    for ltype in spec.links:
        if ltype.runtime:
            continue
        lb = spec.link_bindings.get(ltype.name)
        refs = _link_referenced_objects(spec, ltype, lb)
        if not (refs & set(affected)):
            continue  # 链接与当前受影响对象无声明依赖
        affected_links.add(ltype.name)

        lnk_table = f"lnk_{ltype.name}"
        try:
            cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info('{lnk_table}')").fetchall()]
        except Exception:
            continue  # 链接表未物化（全新对象的边在物化阶段新建）
        if not cols:
            continue
        pk_cols = _all_pk_columns(spec, cols)
        # 仅对"已有种子主键"的对象做邻接行查询（空集对象是全新数据，无旧边可回收）
        seed_objs = [o for o in pk_cols if affected.get(o)]
        if not seed_objs:
            continue
        clauses: list[str] = []
        params: list = []
        for o in seed_objs:
            vals = sorted(affected[o])
            for c in pk_cols[o]:
                placeholders = ", ".join(["?"] * len(vals))
                clauses.append(f'"{c}" IN ({placeholders})')
                params.extend(vals)
        try:
            rows = conn.execute(
                f"SELECT {', '.join(f'\"{c}\"' for c in cols)} "
                f"FROM {lnk_table} WHERE {' OR '.join(clauses)}",
                params,
            ).fetchall()
        except Exception:
            rows = _fallback_link_rows(conn, lnk_table, cols, pk_cols, affected, seed_objs)
        touching_rows += len(rows)
        # 行内每个主键列的值 → 对应对象的受影响主键集（一跳邻居 + 事件外键）
        col_idx = {c: i for i, c in enumerate(cols)}
        for obj, cs in pk_cols.items():
            vals = {r[col_idx[c]] for c in cs for r in rows
                    if r[col_idx[c]] is not None}
            if vals:
                affected.setdefault(obj, set()).update(vals)

    plan.affected_pks = affected
    plan.affected_objects = sorted(affected)
    plan.affected_links = sorted(affected_links)

    tables = ({f"obj_{o}" for o in plan.affected_objects}
              | {f"lnk_{l}" for l in plan.affected_links})
    plan.affected_rules = rules_for_tables(spec, tables)
    plan.estimated_rows = sum(len(v) for v in affected.values()) + touching_rows

    if not plan.affected_objects and not plan.affected_links:
        plan.mode = "skip"
    elif plan.estimated_rows > batch_threshold:
        plan.mode = "batch"
    else:
        plan.mode = "incremental"
    plan.elapsed_ms = (time.perf_counter() - t0) * 1000
    return plan


def plan_from_review(conn, decision, *, pack: str = "default",
                     batch_threshold: int = DEFAULT_BATCH_THRESHOLD) -> RebuildPlan:
    """review.decided(accept) → 受影响范围（REQ-016）。

    decision：core.review.ReviewDecision（entity_type person/org，canonical + variants）。
    种子 = canonical 与全部 variants 在语义层中已物化的实体主键（重建**前**的图，
    变体尚未折叠，一跳邻域同时覆盖合并双方，不漏边）。
    reject 决策不应进入本函数（只写 feedback_event，不触发重建）。
    """
    spec = load_pack(pack)
    valid = {o.name for o in spec.objects}
    obj = getattr(decision, "entity_type", None)
    if obj not in valid:
        raise ValueError(
            f"review 决策实体类型 {obj!r} 不在 ontology 对象清单 {sorted(valid)}")
    otype = next(o for o in spec.objects if o.name == obj)
    names = {decision.canonical, *getattr(decision, "variants", [])}
    names.discard(None)

    seeds: dict[str, set[str]] = {obj: set()}
    try:
        placeholders = ", ".join(["?"] * len(names))
        rows = conn.execute(
            f'SELECT "{otype.pk}" FROM obj_{obj} '
            f'WHERE "{otype.name_property}" IN ({placeholders})',
            sorted(names)).fetchall()
        seeds[obj] = {r[0] for r in rows if r[0]}
    except Exception:
        pass  # 语义层未物化：种子为空，include_objects 仍声明该对象重建
    return plan_from_seeds(conn, seeds, pack=pack, reason="review",
                           batch_threshold=batch_threshold,
                           include_objects=[obj])


def plan_from_partition(conn, part, *, pack: str = "default",
                        batch_threshold: int = DEFAULT_BATCH_THRESHOLD) -> RebuildPlan:
    """新分区到达：源表 → 直接受影响对象 → 种子主键 → 一跳邻域。

    part.dataset 决定直接受影响对象（binding 声明）；
    种子主键 = 这些对象在新分区数据中出现的既有物化主键：
      - 实体型：name_property 值命中分区文本列（新名字尚未物化，由 affected_objects
        驱动重算补入）；
      - 事件型：日期晚于上次语义层 watermark 的既有事件。
    """
    spec = load_pack(pack)
    direct = objects_for_dataset(spec, part.dataset)
    seeds: dict[str, set[str]] = {}

    from core.ontology_version import current_version
    ver = current_version(conn, pack)
    last_wm = ver.ontology_watermark if ver else ""

    for name in direct:
        otype = next(o for o in spec.objects if o.name == name)
        tbl = f"obj_{name}"
        try:
            conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
        except Exception:
            continue  # 未物化（bootstrap 前）→ 无种子，对象仍在 direct 清单中
        if otype.kind == "event":
            date_props = [p for p, t in otype.properties.items() if t == "date"]
            if date_props and last_wm:
                dcol = date_props[0]
                rows = conn.execute(
                    f'SELECT "{otype.pk}" FROM {tbl} '
                    f'WHERE CAST("{dcol}" AS VARCHAR) > ?',
                    [last_wm],
                ).fetchall()
                seeds[name] = {r[0] for r in rows if r[0]}
            else:
                seeds[name] = set()
        else:
            seeds[name] = _seed_entities_from_partition(
                conn, tbl, otype.pk, otype.name_property, part.partition_id)

    # direct 对象经 include_objects 进入声明级受影响集：
    # 即使种子集为空（全新名字/全新事件尚未物化），其声明依赖链接也会在
    # plan_from_seeds 内一并标记重物化；mode（skip/incremental/batch）由此统一判定。
    return plan_from_seeds(conn, seeds, pack=pack, reason="partition",
                           batch_threshold=batch_threshold,
                           include_objects=direct,
                           partition=part.partition_id)


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _seed_entities_from_partition(conn, obj_table: str, pk: str,
                                  name_property: str, partition_view: str) -> set[str]:
    """实体对象中，name_property 值出现在分区视图任一文本列里的既有主键。"""
    try:
        part_cols = [r[1] for r in conn.execute(
            f"PRAGMA table_info('{partition_view}')").fetchall()]
    except Exception:
        return set()
    if not part_cols:
        return set()
    # 分区文本列并集作为名字池（主体/对方/对端/被举报人...）
    union = " UNION ALL ".join(
        f'SELECT CAST("{c}" AS VARCHAR) AS nm FROM {partition_view} '
        f'WHERE "{c}" IS NOT NULL' for c in part_cols)
    try:
        rows = conn.execute(
            f'SELECT o."{pk}" FROM {obj_table} o '
            f'WHERE o."{name_property}" IN (SELECT DISTINCT nm FROM ({union}))'
        ).fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _fallback_link_rows(conn, lnk_table, cols, pk_cols, affected, seed_objs):
    """unnest 不可用时的退化路径：逐对象拼 IN 字面量。"""
    rows = []
    for o in seed_objs:
        vals = sorted(affected[o])
        if not vals:
            continue
        in_list = ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals)
        col_list = ", ".join(f'"{c2}"' for c2 in cols)
        for c in pk_cols[o]:
            try:
                rows.extend(conn.execute(
                    f'SELECT {col_list} FROM {lnk_table} '
                    f'WHERE "{c}" IN ({in_list})'
                ).fetchall())
            except Exception:
                continue
    return rows


def _count_link_rows(conn, link_name: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM lnk_{link_name}").fetchone()[0]
    except Exception:
        return 0
