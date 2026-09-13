"""
server/app/canvas_expand.py
RC-103/104 画布反向逐层溯源 expand 服务（纯组装层，可单测）。

溯源四层（PRD RC-103）：

    fact ──涉及──▶ object ──来源行──▶ source_row ──所属文件──▶ source_file
                     │
                     └── lnk 一跳邻居（lnk_* 语义链接，关系名取 links.title）

纪律（与 graph_view 同源，受只读数据源静态契约测试约束）：
  - 只读消费 obj_*/lnk_* 语义表与 core.row_uri 归档层
    （resolve_object_locator 内部仅读 row_archive/row_build_index），
    不直读 Parquet、不生成自由 SQL；表名/列名标识符全部来自
    ontology_loader 校验过的声明；
  - 数据行字段经 source_row_dto + PolicyEngine 遮蔽（RC-104），
    denied 不送明文，mask 渲染由前端 MaskedField 唯一执行；
  - 幂等：节点按 id（<kind>:<ref>）、边按种子同口径 eid 去重，
    同一层重复展开不产生重复节点/边（AC-103-2）；
  - 失败降级：语义层缺失/归档缺行只挂 notice/missing 标记，不抛 500、
    不破坏已渲染图层（AC-103-4）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.row_uri import (
    BOOTSTRAP_PARTITION,
    MalformedUriError,
    RowNotFoundError,
    make_row_uri,
    resolve_object_locator,
    row_id_for,
)

from server.app.canvas_seed import (
    _X_EVIDENCE,
    _X_FACT,
    _X_ROW,
    _X_RULE,
    _Y_GAP,
    sys_node_id,
)
from server.app.graph_view import _q, _table_exists
from server.app.source_row_dto import dataset_of_row, resolve_source_row

# 单次展开上限（防大对象一次灌爆画布；超限时 truncated=true）
OBJECT_SCAN_PER_TYPE = 2000
NEIGHBOR_CAP_PER_LINK = 50
NEW_NODE_CAP = 200

# source_row 列坐标（与 canvas_seed 分层一致；file=960 在 seed 内私有常量外）
_X_FILE = 960

VALID_DIRECTIONS = frozenset({"source", "neighbors", "all"})
# function_result（RC-204）无下一层语义节点，但展开返回「查询自」源节点与入参快照，
# 经源节点继续 RC-103 四层溯源（AC-204-4）
EXPANDABLE_KINDS = frozenset(
    {"fact", "object", "source_row", "source_file", "function_result"})


# ----------------------------------------------------------------------
# 语义索引（从 OntologyPack 抽最小子集；测试可直接构造 dataclass）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ObjType:
    name: str
    title: str
    pk: str
    name_property: str
    kind: str  # entity | event
    prop_cols: frozenset[str] = frozenset()  # 声明属性列（locator 段回粘校验）


@dataclass(frozen=True)
class LnkType:
    name: str
    title: str
    from_obj: str
    to_obj: str
    from_col: str
    to_col: str


def build_index(spec: Any) -> tuple[dict[str, ObjType], list[LnkType]]:
    """从 load_pack() 产物抽 expand 需要的声明（非 runtime、双侧带 ref 的链接）。"""
    objs = {
        o.name: ObjType(
            name=o.name, title=o.title, pk=o.pk,
            name_property=o.name_property, kind=o.kind,
            prop_cols=frozenset({o.name_property, *getattr(o, "properties", {})}))
        for o in spec.objects if not getattr(o, "runtime", False)
    }
    lnks: list[LnkType] = []
    for lk in spec.links:
        if getattr(lk, "runtime", False):
            continue
        eps = lk.endpoints or {}
        f_ep, t_ep = eps.get("from") or {}, eps.get("to") or {}
        f_ref, t_ref = f_ep.get("ref"), t_ep.get("ref")
        # 与 graph_view 同口径：只有双侧带对象引用的链接可做邻居导航
        if not f_ref or not t_ref:
            continue
        lnks.append(LnkType(
            name=lk.name, title=lk.title,
            from_obj=f_ref["object"], to_obj=t_ref["object"],
            from_col=f_ep["col"], to_col=t_ep["col"]))
    return objs, lnks


# ----------------------------------------------------------------------
# locator 解析（obj_*.source_rows：["dataset:c=v,c=v"]）
# ----------------------------------------------------------------------
def parse_locator(loc: str, known_cols: set[str] | None = None
                  ) -> tuple[str, list[tuple[str, str]]]:
    """拆 obj 行 source_rows locator。

    值含逗号时（长文本列）按"段首键名必须是已知列"回粘到上一值。
    """
    dataset, rest = loc.split(":", 1)
    pairs: list[tuple[str, str]] = []
    for seg in rest.split(","):
        key, _, val = seg.partition("=")
        if known_cols is not None and key not in known_cols and pairs:
            # 逗号是值的一部分：回粘
            prev_k, prev_v = pairs[-1]
            pairs[-1] = (prev_k, f"{prev_v},{seg}")
            continue
        pairs.append((key, val))
    return dataset, pairs


def _norm(v: Any) -> str:
    return str("" if v is None else v).strip()


def _norm_eq(a: Any, b: Any) -> bool:
    sa, sb = _norm(a), _norm(b)
    if sa == sb:
        return True
    try:
        return float(sa) == float(sb)
    except ValueError:
        return False


def _is_table_summary(row: dict[str, Any]) -> bool:
    return isinstance(row, dict) and row.get("粒度") == "表级汇总"


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def expand_layer(*, conn, objects: dict[str, ObjType], links: list[LnkType],
                 doc: dict[str, Any], node_id: str,
                 clue_rows: dict[str, dict[str, Any]] | None = None,
                 registered_sources: dict[str, dict[str, Any]] | None = None,
                 access=None, pack_id: str = "default",
                 base_dir=None, build_id: str | None = None,
                 direction: str = "all") -> dict[str, Any]:
    """展开一个节点的下一层。

    参数：
      conn               : CaseStore.read_conn（DuckDB 只读）；None=语义层不可用
      objects/links      : build_index(load_pack(...)) 声明子集
      doc                : 当前画布文档（节点/边去重与定位依赖）
      clue_rows          : 本线索 source_row 节点 ref(uri) → 原始行 dict
                           （种子伪 URI 与 evidence_builder._make_source_ref 同口径）
      registered_sources: ingest 登记 dataset 名 → case_sources 行
      access             : AccessContext（RC-104 遮蔽）；None=system 旁路
      build_id           : 当前语义版本（归档取回/真实 row_uri 拼装）
      direction          : source | neighbors | all

    返回：
      {nodes, edges, details, notices, truncated, leaf}
      details: {node_id: 抽屉负载}——source_row=SourceRowDto(+archived)、
               source_file={registered, file?, dataset}
    """
    clue_rows = clue_rows or {}
    registered_sources = registered_sources or {}
    node = next((n for n in doc.get("nodes", []) if n.get("id") == node_id), None)
    if node is None:
        return {"nodes": [], "edges": [], "details": {},
                "notices": ["node_not_found"], "truncated": False,
                "leaf": False}
    kind = node.get("kind")
    if kind not in EXPANDABLE_KINDS:
        return {"nodes": [], "edges": [], "details": {},
                "notices": ["unsupported_kind"], "truncated": False,
                "leaf": False}

    ctx = _Ctx(conn=conn, objects=objects, links=links, doc=doc,
               clue_rows=clue_rows, sources=registered_sources,
               access=access, pack_id=pack_id, base_dir=base_dir,
               build_id=build_id)
    if kind == "fact":
        out = _expand_fact(ctx, node)
    elif kind == "object":
        out = _expand_object(ctx, node, direction)
    elif kind == "source_row":
        out = _expand_row(ctx, node)
    elif kind == "function_result":
        out = _expand_function_result(ctx, node)
    else:
        # source_file：叶子
        return {"nodes": [], "edges": [], "details": {},
                "notices": ["leaf"], "truncated": False, "leaf": True}
    # 返回即带列坐标（前端免二次布局；落库 merge 时按库内现状幂等复核）
    position_new_nodes(doc, out["nodes"])
    return out


class _Ctx:
    """单次展开的可变收集器（节点/边按 id 去重，跨层共享预算）。"""

    def __init__(self, *, conn, objects, links, doc, clue_rows, sources,
                 access, pack_id, base_dir, build_id):
        self.conn = conn
        self.objects = objects
        self.links = links
        self.doc = doc
        self.clue_rows = clue_rows
        self.sources = sources
        self.access = access
        self.pack_id = pack_id
        self.base_dir = base_dir
        self.build_id = build_id
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.details: dict[str, Any] = {}
        self.notices: list[str] = []
        self.truncated = False
        self._node_ids = {n["id"] for n in doc.get("nodes", [])}
        self._edge_ids = {e["id"] for e in doc.get("edges", [])}
        self._new_ids: set[str] = set()

    def semantic_available(self) -> bool:
        if self.conn is None:
            return False
        try:
            n = self.conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name LIKE 'obj_%'").fetchone()[0]
            return int(n) > 0
        except Exception:
            return False

    def add_node(self, n: dict[str, Any]) -> bool:
        nid = n["id"]
        if nid in self._node_ids:
            return False
        if len(self._new_ids) >= NEW_NODE_CAP:
            self.truncated = True
            return False
        self._node_ids.add(nid)
        self._new_ids.add(nid)
        self.nodes.append(n)
        return True

    def add_edge(self, source: str, target: str, rel: str,
                 system: bool = True) -> None:
        eid = f"e:{source}--{rel}--{target}"
        if eid in self._edge_ids:
            return
        self._edge_ids.add(eid)
        self.edges.append({"id": eid, "source": source, "target": target,
                           "rel": rel, "system": system})

    def result(self, *, leaf: bool = False) -> dict[str, Any]:
        return {"nodes": self.nodes, "edges": self.edges,
                "details": self.details, "notices": self.notices,
                "truncated": self.truncated, "leaf": leaf}

    # -- 行 DTO（RC-104 遮蔽唯一口径，与线索详情 source_row_dto 同源）----
    def row_dto(self, *, source_row: dict[str, Any],
                uri: str | None = None) -> dict[str, Any]:
        dto = resolve_source_row(
            source_row=source_row, conn=self.conn,
            pack_id=self.pack_id, base_dir=self.base_dir,
            access=self.access)
        if uri:
            dto["row_uri"] = uri
        return dto


# ----------------------------------------------------------------------
# fact ──涉及──▶ object（实体 + 事件，按本事实已连的数据行匹配）
# ----------------------------------------------------------------------
def _connected_row_uris(doc: dict[str, Any], fact_id: str) -> list[str]:
    out = []
    for e in doc.get("edges", []):
        if e.get("source") == fact_id and e.get("rel") == "来源行":
            n = next((x for x in doc["nodes"] if x["id"] == e["target"]), None)
            if n and n.get("ref"):
                out.append(n["ref"])
    return out


def _row_values(row: dict[str, Any]) -> set[str]:
    vals: set[str] = set()
    for v in row.values():
        if isinstance(v, (list, dict)):
            continue
        s = _norm(v)
        if s:
            vals.add(s)
    return vals


def _expand_fact(ctx: _Ctx, node: dict[str, Any]) -> dict[str, Any]:
    uris = _connected_row_uris(ctx.doc, node["id"])
    rows = [ctx.clue_rows[u] for u in uris if u in ctx.clue_rows]
    if not rows:
        # M1 历史画布/聚合线索：uri 对不上时回退本线索全部行级行
        rows = [r for r in ctx.clue_rows.values() if not _is_table_summary(r)]
    if not ctx.semantic_available():
        ctx.notices.append("semantic_unavailable")
        return ctx.result()

    by_ds: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        ds = dataset_of_row(r, pack_id=ctx.pack_id, base_dir=ctx.base_dir)
        if ds and ds not in ("未知数据源", "数据行"):
            by_ds.setdefault(ds, []).append(r)

    for ot in ctx.objects.values():
        tbl = f"obj_{ot.name}"
        if not _table_exists(ctx.conn, tbl):
            continue
        try:
            recs = ctx.conn.execute(
                f"SELECT {_q(ot.pk)}, {_q(ot.name_property)}, "
                f"{_q('source_rows')} FROM {_q(tbl)} "
                f"LIMIT {OBJECT_SCAN_PER_TYPE}").fetchall()
        except Exception:
            continue
        for pk, label, locs_json in recs:
            if pk is None:
                continue
            if not _object_matches(locs_json, ot, by_ds):
                continue
            nid = _object_node(ot, str(pk), str(label) if label is not None
                               else str(pk), ctx)
            if nid:
                ctx.add_edge(node["id"], nid, "涉及")
    return ctx.result()


def _object_matches(locs_json: str, ot: ObjType,
                    by_ds: dict[str, list[dict[str, Any]]]) -> bool:
    """obj 行任一 locator 命中本事实行集即关联。"""
    try:
        locs = json.loads(locs_json or "[]")
    except (ValueError, TypeError):
        return False
    for loc in locs:
        try:
            ds, pairs = parse_locator(str(loc), ot.prop_cols)
        except ValueError:
            continue
        candidates = by_ds.get(ds)
        if not candidates:
            continue
        for row in candidates:
            if ot.kind == "entity":
                value_set = _row_values(row)
                if any(_norm(v) in value_set for _, v in pairs if _norm(v)):
                    return True
            else:  # event：locator 每个 col=v 都在行内（数值归一）
                if pairs and all(
                        k in row and _norm_eq(v, row[k])
                        for k, v in pairs):
                    return True
    return False


def _object_node(ot: ObjType, pk: str, label: str, ctx: _Ctx) -> str | None:
    ref = f"{ot.name}:{pk}"
    nid = sys_node_id("object", ref)
    if nid in ctx._node_ids:
        return nid
    ctx.add_node({
        "id": nid, "kind": "object", "ref": ref,
        "label": label or pk, "system": True, "pinned": False,
        "props": {"type": ot.name, "type_title": ot.title, "pk": pk},
    })
    return nid if nid in ctx._node_ids else None


# ----------------------------------------------------------------------
# object：lnk 一跳邻居 + object ──来源行──▶ source_row
# ----------------------------------------------------------------------
def _expand_object(ctx: _Ctx, node: dict[str, Any], direction: str) -> dict[str, Any]:
    ref = str(node.get("ref") or "")
    otype_name, _, pk = ref.partition(":")
    ot = ctx.objects.get(otype_name)
    if ot is None:
        ctx.notices.append("unknown_object_type")
        return ctx.result()

    if direction in ("neighbors", "all"):
        if not ctx.semantic_available():
            ctx.notices.append("semantic_unavailable")
        else:
            _expand_neighbors(ctx, ot, pk)
    if direction in ("source", "all"):
        if not ctx.semantic_available():
            ctx.notices.append("semantic_unavailable")
        else:
            _expand_object_rows(ctx, node, ot, pk)
    return ctx.result()


def _expand_neighbors(ctx: _Ctx, ot: ObjType, pk: str) -> None:
    for lk in ctx.links:
        tbl = f"lnk_{lk.name}"
        if not _table_exists(ctx.conn, tbl):
            continue
        if ot.name not in (lk.from_obj, lk.to_obj):
            continue
        try:
            recs = ctx.conn.execute(
                f"SELECT {_q(lk.from_col)}, {_q(lk.to_col)} FROM {_q(tbl)} "
                f"WHERE {_q(lk.from_col)} = ? OR {_q(lk.to_col)} = ? "
                f"LIMIT {NEIGHBOR_CAP_PER_LINK + 1}", [pk, pk]).fetchall()
        except Exception:
            continue
        if len(recs) > NEIGHBOR_CAP_PER_LINK:
            ctx.truncated = True
            recs = recs[:NEIGHBOR_CAP_PER_LINK]
        # 收集需补标签的邻居 pk（按类型批量取 name_property）
        wanted: dict[str, set[str]] = {}
        pairs: list[tuple[str, str, str, str]] = []
        for fv, tv in recs:
            if ot.name == lk.from_obj and _norm(fv) == pk:
                pairs.append((lk.from_obj, str(fv), lk.to_obj,
                              "" if tv is None else str(tv)))
            if ot.name == lk.to_obj and _norm(tv) == pk:
                pairs.append((lk.to_obj, str(tv), lk.from_obj,
                              "" if fv is None else str(fv)))
        for _src_t, _src_pk, t_type, t_pk in pairs:
            if t_pk:
                wanted.setdefault(t_type, set()).add(t_pk)
        labels = _load_labels(ctx, wanted)
        for src_t, src_pk, t_type, t_pk in pairs:
            if not t_pk or t_type not in ctx.objects:
                continue
            src_id = (sys_node_id("object", f"{src_t}:{src_pk}")
                      if src_pk == pk else None)
            tot = ctx.objects[t_type]
            tgt = _object_node(tot, t_pk, labels.get(t_type, {}).get(
                t_pk, t_pk), ctx)
            if src_id and tgt:
                ctx.add_edge(src_id, tgt, lk.title)


def _load_labels(ctx: _Ctx, wanted: dict[str, set[str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for t_type, pks in wanted.items():
        ot = ctx.objects.get(t_type)
        if ot is None or not pks or not _table_exists(
                ctx.conn, f"obj_{t_type}"):
            continue
        ph = ", ".join("?" for _ in pks)
        try:
            recs = ctx.conn.execute(
                f"SELECT {_q(ot.pk)}, {_q(ot.name_property)} "
                f"FROM {_q(f'obj_{t_type}')} WHERE {_q(ot.pk)} IN ({ph})",
                list(pks)).fetchall()
        except Exception:
            continue
        out[t_type] = {str(rp): str(lb) if lb is not None else str(rp)
                       for rp, lb in recs}
    return out


def _expand_object_rows(ctx: _Ctx, node: dict[str, Any], ot: ObjType,
                        pk: str) -> None:
    tbl = f"obj_{ot.name}"
    try:
        row = ctx.conn.execute(
            f"SELECT {_q('source_rows')} FROM {_q(tbl)} "
            f"WHERE {_q(ot.pk)} = ?", [pk]).fetchone()
    except Exception:
        return
    if row is None:
        ctx.notices.append("object_gone")
        return
    try:
        locs = json.loads(row[0] or "[]")
    except (ValueError, TypeError):
        locs = []
    for loc in locs:
        try:
            ds, pairs = parse_locator(str(loc), ot.prop_cols)
        except ValueError:
            continue
        _attach_locator_row(ctx, node["id"], ot, ds, pairs)


def _attach_locator_row(ctx: _Ctx, object_id: str, ot: ObjType,
                        dataset: str, pairs: list[tuple[str, str]]) -> None:
    """对象 locator → 行节点：先认领线索种子行（伪 URI），再回落归档/缺失。"""
    # 1) 本线索种子行：locator 键值与线索原始行匹配（实体值包含 / 事件全等）。
    #    for/else：命中即本 locator 处理完毕（其余 locator 仍由调用方继续）。
    for uri, raw in ctx.clue_rows.items():
        if _is_table_summary(raw):
            continue
        if dataset_of_row(raw, pack_id=ctx.pack_id,
                          base_dir=ctx.base_dir) != dataset:
            continue
        hit = (any(_norm(v) and _norm(v) in _row_values(raw)
                   for _, v in pairs) if ot.kind == "entity"
               else all(k in raw and _norm_eq(v, raw[k])
                        for k, v in pairs))
        if hit:
            rid = sys_node_id("source_row", uri)
            if rid in ctx._node_ids:
                ctx.add_edge(object_id, rid, "来源行")
                _detail_clue_row(ctx, rid, raw)
            break
    else:
        _attach_archive_or_missing_row(ctx, object_id, dataset, pairs)


def _attach_archive_or_missing_row(
        ctx: _Ctx, object_id: str, dataset: str,
        pairs: list[tuple[str, str]]) -> None:
    """locator 未认领先索行 → 归档取回；落空挂缺失标记（不炸、不触 Parquet）。"""
    pair_dict = dict(pairs)
    # 2) 归档取回（真实 URI；RC-104 遮蔽 DTO）
    if ctx.build_id and ctx.conn is not None:
        try:
            resolved = resolve_object_locator(
                ctx.conn, dataset=dataset, pairs=pairs,
                build_id=ctx.build_id)
            uri = make_row_uri(
                dataset, ctx.build_id, resolved.get("partition")
                or BOOTSTRAP_PARTITION, resolved["rowid"])
            dto = ctx.row_dto(source_row={"row_uri": uri}, uri=uri)
            rid = _add_row_node(ctx, uri=uri, source=dataset, archived=True,
                                granularity="")
            if rid:
                ctx.details[rid] = {**dto, "archived": True,
                                    "granularity": ""}
                ctx.add_edge(object_id, rid, "来源行")
            return
        except (RowNotFoundError, MalformedUriError):
            pass

    # 3) 归档缺失：URI 原文 + 缺失标记（AC-103 异常表；字段取 locator pairs
    #    并过遮蔽口径，明文不进持久化 doc——仅在本次 details 回传）
    rid0 = row_id_for(dataset, [k for k, _ in pairs],
                      [v for _, v in pairs])
    attempt_uri = None
    if ctx.build_id:
        try:
            attempt_uri = make_row_uri(dataset, ctx.build_id,
                                       BOOTSTRAP_PARTITION, rid0)
        except MalformedUriError:
            attempt_uri = None
    node_ref = attempt_uri or f"{dataset}@local#locator/{rid0}"
    rid = _add_row_node(ctx, uri=node_ref, source=dataset, archived=False,
                        granularity="", missing=True)
    if rid:
        dto = ctx.row_dto(source_row=pair_dict, uri=node_ref)
        ctx.details[rid] = {**dto, "archived": False, "missing": True,
                            "granularity": ""}
        ctx.add_edge(object_id, rid, "来源行")


def _detail_clue_row(ctx: _Ctx, node_id: str, raw: dict[str, Any]) -> None:
    if node_id in ctx.details:
        return
    dto = ctx.row_dto(source_row=raw,
                      uri=node_id.split("source_row:", 1)[-1])
    ctx.details[node_id] = {**dto, "archived": True, "granularity": "",
                            "from_clue": True}


# ----------------------------------------------------------------------
# source_row ──所属文件──▶ source_file
# ----------------------------------------------------------------------
def _expand_row(ctx: _Ctx, node: dict[str, Any]) -> dict[str, Any]:
    uri = str(node.get("ref") or "")
    props = node.get("props") or {}
    # dataset 优先取节点声明 source（表级汇总 URI 前缀是语义表名，
    # ingest 登记的是原始上传表名，以声明 source 为准）
    dataset = (str(props.get("source") or "")
               or (uri.split("@", 1)[0] if "@" in uri else ""))
    granularity = str(props.get("granularity") or "")
    archived = bool(props.get("archived", True))

    # 抽屉字段负载（种子伪行→线索原始行；真实 URI→归档；缺失行→标记）
    raw = ctx.clue_rows.get(uri)
    if raw is None and "#row/" in uri:
        # 前缀漂移兜底：数据源声明修正（图章）只改 URI 的 dataset 前缀，
        # rowid（内容哈希）不变——按 rowid 尾段重配本线索原始行，
        # 旧画布已 seed 节点不因前缀修正丢失抽屉字段。
        tail = uri.rsplit("#row/", 1)[-1]
        raw = next((r for u, r in ctx.clue_rows.items()
                    if u.rsplit("#row/", 1)[-1] == tail and u != uri), None)
    if raw is not None:
        dto = ctx.row_dto(source_row=raw, uri=uri)
        ctx.details[node["id"]] = {
            **dto, "archived": True,
            "granularity": granularity or ("表级汇总"
                                           if _is_table_summary(raw) else ""),
            "from_clue": True}
    elif ctx.build_id and "#row/" in uri and ctx.conn is not None \
            and not uri.endswith("#row/"):
        try:
            dto = ctx.row_dto(source_row={"row_uri": uri}, uri=uri)
            ctx.details[node["id"]] = {**dto, "archived": archived,
                                       "granularity": granularity}
        except (RowNotFoundError, MalformedUriError):
            ctx.details[node["id"]] = _missing_detail(uri, dataset)
    else:
        ctx.details[node["id"]] = _missing_detail(uri, dataset)

    if not dataset:
        ctx.notices.append("leaf")
        return ctx.result(leaf=True)
    _attach_file(ctx, node["id"], dataset)
    return ctx.result()


def _missing_detail(uri: str, dataset: str) -> dict[str, Any]:
    return {"row_uri": uri, "source": dataset or None, "fields": [],
            "archived": False, "missing": True, "granularity": ""}


def _attach_file(ctx: _Ctx, row_node_id: str, dataset: str) -> None:
    src = ctx.sources.get(dataset)
    if not src:
        ref = f"unregistered:{dataset}"
        fid = sys_node_id("source_file", ref)
        ctx.add_node({
            "id": fid, "kind": "source_file", "ref": ref,
            "label": f"{dataset}（未登记数据源）",
            "system": True, "pinned": False,
            "props": {"dataset": dataset, "registered": False},
        })
        if fid in ctx._node_ids:
            ctx.details[fid] = {"registered": False, "dataset": dataset}
            ctx.add_edge(row_node_id, fid, "所属文件")
        return
    upload_id = str(src.get("upload_id") or "")
    fid = sys_node_id("source_file", upload_id)
    file_card = {
        "upload_id": upload_id,
        "filename": str(src.get("filename") or ""),
        "format": str(src.get("fmt") or src.get("format") or ""),
        "rows": src.get("rows"),
        "uploaded_by": str(src.get("created_by") or ""),
        "uploaded_at": str(src.get("created_at") or ""),
        "dataset": dataset,
    }
    ctx.add_node({
        "id": fid, "kind": "source_file", "ref": upload_id,
        "label": file_card["filename"] or upload_id,
        "system": True, "pinned": False,
        "props": {"dataset": dataset, "registered": True,
                  "upload_id": upload_id},
    })
    if fid in ctx._node_ids:
        ctx.details[fid] = {"registered": True, "dataset": dataset,
                            "file": file_card}
        ctx.add_edge(row_node_id, fid, "所属文件")


# ----------------------------------------------------------------------
# 行节点
# ----------------------------------------------------------------------
def _add_row_node(ctx: _Ctx, *, uri: str, source: str, archived: bool,
                  granularity: str, missing: bool = False) -> str | None:
    nid = sys_node_id("source_row", uri)
    label = source or "数据源"
    if granularity == "表级汇总":
        label = f"{source}（表级汇总）"
    ctx.add_node({
        "id": nid, "kind": "source_row", "ref": uri,
        "label": label, "system": True, "pinned": False,
        "props": {"row_uri": uri, "source": source,
                  "granularity": granularity, "registered": True,
                  "archived": archived, "missing": missing},
    })
    return nid if nid in ctx._node_ids else None


# ----------------------------------------------------------------------
# RC-204：function_result 结果节点溯源（回查询源 + 入参/输入表快照；
# 源节点本身可继续 RC-103 四层展开，无下一层语义节点、不写库）
# ----------------------------------------------------------------------
def _expand_function_result(ctx: _Ctx,
                        node: dict[str, Any]) -> dict[str, Any]:
    props = node.get("props") or {}
    nid = node.get("id")
    source_ids = [str(e.get("source")) for e in ctx.doc.get("edges", [])
                  if e.get("target") == nid and e.get("rel") == "查询自"]
    fname = str(props.get("function") or "")
    input_tables: list[str] = []
    if fname:
        try:
            from core.ontology_loader import load_pack
            fspec = load_pack(
                ctx.pack_id, base_dir=ctx.base_dir).functions.get(fname)
            if fspec is not None:
                input_tables = list(getattr(fspec, "inputs", None) or [])
        except Exception:
            # 案件包损坏/快照缺失：抽屉仍展示节点自带入参快照，仅缺输入表声明
            ctx.notices.append("pack_load_failed")
    summary_keys = ("kind", "row_count", "columns", "preview_rows",
                    "meta", "report")
    ctx.details[nid] = {
        "kind": "function_result",
        "function": fname,
        "function_title": props.get("function_title") or "",
        "params": props.get("params") or {},
        "executed_at": props.get("executed_at") or "",
        "executed_by": props.get("executed_by") or "",
        "output_type": props.get("output_type") or "",
        "summary": {k: props[k] for k in summary_keys if k in props},
        "source_node_ids": source_ids,
        "input_tables": input_tables,
    }
    return {"nodes": [], "edges": [], "details": ctx.details,
            "notices": list(ctx.notices), "truncated": False,
            "leaf": not source_ids}


# ----------------------------------------------------------------------
# 合并 + 新节点定位（只给新增节点排坐标，既有节点坐标不动）
# ----------------------------------------------------------------------
def position_new_nodes(doc: dict[str, Any],
                       new_nodes: list[dict[str, Any]]) -> None:
    """新节点按 kind 列追加到该列现有最大 y 之下（确定性，无布局依赖）。"""
    col_x = {
        "rule": _X_RULE,
        "fact": _X_FACT,
        "verify_item": _X_FACT,
        "object": _X_FACT + 220,
        "evidence": _X_EVIDENCE,
        "source_row": _X_ROW,
        "source_file": _X_FILE,
    }
    used: dict[str, int] = {}
    shared = 0
    for n in doc.get("nodes", []):
        col = n.get("kind")
        if col in ("fact", "verify_item"):
            shared += 1
        used[col] = used.get(col, 0) + 1
    for n in new_nodes:
        col = n["kind"]
        if col in ("fact", "verify_item"):
            n["x"] = col_x.get(col, 0)
            n["y"] = shared * _Y_GAP
            shared += 1
        else:
            idx = used.get(col, 0)
            n["x"] = col_x.get(col, 0)
            n["y"] = idx * _Y_GAP
        used[col] = used.get(col, 0) + 1


def merge_expansion(doc: dict[str, Any], nodes: list[dict[str, Any]],
                    edges: list[dict[str, Any]]
                    ) -> tuple[list[str], list[str]]:
    """把 expand 结果并入文档（原地）；返回新增 node_id/edge_id。"""
    existing = {n["id"] for n in doc["nodes"]}
    new_nodes = [n for n in nodes if n["id"] not in existing]
    added_nodes = [n["id"] for n in new_nodes]
    position_new_nodes(doc, new_nodes)
    doc.setdefault("nodes", []).extend(new_nodes)
    existing_e = {e["id"] for e in doc["edges"]}
    new_edges = [e for e in edges if e["id"] not in existing_e]
    added_edges = [e["id"] for e in new_edges]
    doc.setdefault("edges", []).extend(new_edges)
    return added_nodes, added_edges
