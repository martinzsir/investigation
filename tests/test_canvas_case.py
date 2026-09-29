"""研判层画布（案件级）领域层测试。

每条需求独立可测（不依赖前序用例的副作用），并配反向验证——
变异体必须让断言失败，否则断言是恒真的摆设。
"""
from __future__ import annotations

import pytest

from server.app import canvas_case as cc


# ----------------------------------------------------------------------
# 假语义层连接：只实现 execute/fetchone/fetchall/description
# ----------------------------------------------------------------------
class _Cur:
    def __init__(self, rows, cols):
        self._rows = rows
        self.description = [(c, None) for c in cols]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """people: {name: [pk, ...]}；locations: {lid: row_dict}"""

    def __init__(self, people=None, locations=None, fail=False):
        self.people = people or {}
        self.locations = locations or {}
        self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        s = " ".join(str(sql).split())
        if "obj_person" in s or "obj_person_identity" in s:
            name = (params or [""])[0]
            pks = self.people.get(name, [])
            return _Cur([(p,) for p in pks], ["person_id"])
        if "obj_location" in s:
            lid = (params or [""])[0]
            row = self.locations.get(lid)
            if not row:
                return _Cur([], ["location_id"])
            cols = ["location_id", "std_address", "lat", "lng",
                    "coord_sys", "geocode_source", "geocode_confidence"]
            return _Cur([tuple(row.get(c) for c in cols)], cols)
        return _Cur([], ["x"])


# ======================================================================
# CAN-01 案件级节点键：与线索级天然不撞
# ======================================================================
def test_case_key_prefix_distinct_from_clue():
    """案件级带 case# 前缀，与线索级 {kind}:{ref} 不可能撞键。"""
    from server.app.canvas_seed import sys_node_id

    case_n = cc.case_node_id("subject", "C1", "person_abc")
    clue_n = sys_node_id("subject", "person_abc")
    assert case_n != clue_n
    assert case_n.startswith(cc.CASE_KEY_PREFIX)
    assert not clue_n.startswith(cc.CASE_KEY_PREFIX)


def test_case_key_scoped_by_case():
    """同一 ref 在不同案件下必须是不同节点。"""
    assert (cc.case_node_id("subject", "C1", "pk1")
            != cc.case_node_id("subject", "C2", "pk1"))


def test_parse_roundtrip_and_reject_legacy():
    d = cc.parse_case_node_id(cc.case_node_id("place", "C9", "loc_1"))
    assert d == {"case_id": "C9", "kind": "place", "ref": "loc_1"}
    # 线索级 id 反解返回 None —— 旧画布"只读不迁"的依据
    assert cc.parse_case_node_id("fact:abc123") is None


def test_case_edge_id_idempotent():
    a = cc.case_edge_id("n1", "支撑", "n2")
    assert a == cc.case_edge_id("n1", "支撑", "n2")
    assert a != cc.case_edge_id("n2", "支撑", "n1")


# ======================================================================
# CAN-02 节点类型：研判对象语义齐备
# ======================================================================
def test_case_node_kinds_complete():
    # item 独立成 kind（不做 subject 子类型），判据是**窗口分化**：
    # 前端 canvas-window.ts 的 KIND_TO_WINDOW 里 subject→relation、item→holding。
    # 若物品压成 subject 子类型，windowKindFor 只看 kind，物品会弹关系窗口，
    # 持有链窗口永远成死代码，蛇形布局也就永远画不出来。
    # 本断言即这条前后端 kind 协议的守卫：任一侧改名都必须同步。
    assert cc.CASE_NODE_KINDS == {"subject", "item", "place", "event",
                                  "analysis_result"}
    assert cc.CASE_NODE_KINDS <= cc.ALL_NODE_KINDS


def test_hold_rel_is_temporal_edge():
    """持有边是时态边：必须带时间区间，否则"甲→车、乙→车"会被读成共用。"""
    assert cc.HOLD_REL == "持有"
    assert cc.HOLD_REL in cc.SYSTEM_RELS_CASE


# ======================================================================
# CAN-03 subject：唯一主键锚定；重名绝不自裁（R-1）
# ======================================================================
def test_subject_unique_pk_anchored():
    conn = FakeConn(people={"李志强": ["person_07ed989d84fd"]})
    n = cc.build_subject_node(case_id="C1", name="李志强", conn=conn)
    assert n["props"]["person_pk"] == "person_07ed989d84fd"
    assert n["props"]["person_pk_ambiguous"] is False
    assert n["props"]["anchored"] is True
    assert n["id"] == "case#C1:subject:person_07ed989d84fd"


def test_subject_homonym_never_autopick():
    """R-1 核心：两个候选时 pk 必须为空，绝不挑一个。"""
    conn = FakeConn(people={"张卫国": ["person_ecb52c3719fc",
                                       "person_ecb52c3719fc_2"]})
    n = cc.build_subject_node(case_id="C1", name="张卫国", conn=conn)
    assert n["props"]["person_pk"] is None
    assert n["props"]["person_pk_ambiguous"] is True
    assert n["props"]["anchored"] is False
    assert len(n["props"]["pk_candidates"]) == 2
    assert "未锚定" in n["props"]["pk_resolution"]


def test_subject_homonym_mutation_caught():
    """反向验证：若自裁挑第一个，断言必须失败。"""
    conn = FakeConn(people={"张卫国": ["pk_a", "pk_b"]})
    n = cc.build_subject_node(case_id="C1", name="张卫国", conn=conn)
    # 自裁变异体：pk = pks[0]
    assert n["props"]["person_pk"] != "pk_a", "自裁变异未被拦截"


def test_subject_no_conn_not_faked():
    """R-2：无连接不得伪造主键，须声明原因。"""
    n = cc.build_subject_node(case_id="C1", name="王五", conn=None)
    assert n["props"]["person_pk"] is None
    assert n["props"]["anchored"] is False
    assert "未连接语义层" in n["props"]["pk_resolution"]


def test_subject_unknown_name_flagged():
    """语义层未收录 → 未锚定且写明，避免正兵以为工具坏了。"""
    conn = FakeConn(people={})
    n = cc.build_subject_node(case_id="C1", name="查无此人", conn=conn)
    assert n["props"]["anchored"] is False
    assert "未收录" in n["props"]["pk_resolution"]


def test_subject_sub_type_extensible():
    """物品/组织复用同一 subject 类型，靠 sub_type 区分。"""
    n = cc.build_subject_node(case_id="C1", name="浙A12345",
                              sub_type="item_vehicle", conn=None)
    assert n["props"]["sub_type"] == "item_vehicle"
    assert n["kind"] == "subject"


# ======================================================================
# CAN-04 place：坐标必须带精度档自陈
# ======================================================================
def test_place_doorplate_not_degraded():
    conn = FakeConn(locations={"loc_1": {
        "location_id": "loc_1", "std_address": "莫干山路111号",
        "lat": 30.29, "lng": 120.15, "coord_sys": "GCJ-02",
        "geocode_source": "doorplate", "geocode_confidence": 0.95}})
    n = cc.build_place_node(case_id="C1", location_id="loc_1", conn=conn)
    assert n["props"]["mappable"] is True
    assert n["props"]["coord_degraded"] is False
    assert n["props"]["coord_note"] is None
    assert n["props"]["lat"] == 30.29


def test_place_centroid_marked_degraded():
    """质心不得伪装成门牌：必须标 coord_degraded 且附说明。"""
    conn = FakeConn(locations={"loc_2": {
        "location_id": "loc_2", "std_address": "拱墅区",
        "lat": 30.30, "lng": 120.14, "coord_sys": "GCJ-02",
        "geocode_source": "district_centroid", "geocode_confidence": 0.3}})
    n = cc.build_place_node(case_id="C1", location_id="loc_2", conn=conn)
    assert n["props"]["coord_degraded"] is True
    assert n["props"]["coord_note"] and "质心" in n["props"]["coord_note"]


def test_place_centroid_mutation_caught():
    """反向验证：质心被抹掉降级标记必须失败。"""
    conn = FakeConn(locations={"loc_2": {
        "location_id": "loc_2", "std_address": "拱墅区",
        "lat": 30.30, "lng": 120.14, "coord_sys": "GCJ-02",
        "geocode_source": "district_centroid", "geocode_confidence": 0.3}})
    n = cc.build_place_node(case_id="C1", location_id="loc_2", conn=conn)
    assert n["props"]["coord_degraded"] is not False, "降级标记被抹未拦截"


def test_place_missing_coord_not_faked():
    """R-2：取不到坐标不得塞 0，须 mappable=False 且写明。"""
    n = cc.build_place_node(case_id="C1", location_id="loc_x", conn=None)
    assert n["props"]["lat"] is None and n["props"]["lng"] is None
    assert n["props"]["mappable"] is False
    assert n["props"]["coord_note"]


def test_place_db_error_degrades_visibly():
    conn = FakeConn(locations={}, fail=True)
    n = cc.build_place_node(case_id="C1", location_id="loc_1", conn=conn)
    assert n["props"]["mappable"] is False
    assert n["props"]["coord_note"]


# ======================================================================
# CAN-05 event：时刻与精度档必须同时具备
# ======================================================================
def test_event_minute_ready():
    n = cc.build_event_node(case_id="C1", event_ref="ev1",
                            occurred_at="2020-03-24 14:35:00",
                            precision="minute")
    assert n["props"]["time_axis_ready"] is True
    assert n["props"]["can_overlap"] is True
    assert n["props"]["incomplete"] is False
    assert n["props"]["precision_weight"] == 1.0


def test_event_date_not_overlappable():
    """date 档不得能判重叠——否则"同地异时"被当成"同框"。"""
    n = cc.build_event_node(case_id="C1", event_ref="ev2",
                            occurred_at="2020-03-24", precision="date")
    assert n["props"]["can_overlap"] is False
    assert n["props"]["precision_weight"] == 0.2


def test_event_missing_precision_incomplete():
    n = cc.build_event_node(case_id="C1", event_ref="ev3",
                            occurred_at="2020-03-24", precision=None)
    assert n["props"]["incomplete"] is True
    assert n["props"]["precision_note"]


def test_event_missing_time_incomplete():
    n = cc.build_event_node(case_id="C1", event_ref="ev4",
                            precision="minute")
    assert n["props"]["time_axis_ready"] is False
    assert n["props"]["incomplete"] is True


# ======================================================================
# CAN-06 analysis_result：统一命名，来源写进 props
# ======================================================================
def test_result_unified_kind_with_origin():
    n = cc.build_result_node(case_id="C1", result_ref="r1", lens_id="geo_accompany",
                             assumption="H6", precision="minute",
                             dims=["space", "time"])
    assert n["kind"] == "analysis_result"
    assert n["props"]["origin"] == "lens"
    assert n["props"]["assumption"] == "H6"
    assert n["props"]["dim_count"] == 2


def test_result_function_origin():
    n = cc.build_result_node(case_id="C1", result_ref="r2",
                             function_id="overpass_two_hop")
    assert n["props"]["origin"] == "function"


# ======================================================================
# CAN-08 提升：object → subject 系统边
# ======================================================================
def test_promote_edge_is_system_and_preserves_source():
    """提升是新建+连边，原证据节点保留（保真，随时可钻回）。"""
    subj = cc.build_subject_node(case_id="C1", name="李志强", conn=None)
    e = cc.promote_edge(case_id="C1", object_node_id="object:person_1",
                        subject_node=subj)
    assert e["system"] is True
    assert e["rel"] == cc.PROMOTE_REL == "提升为"
    assert e["source"] == "object:person_1"     # 原节点未被改写
    assert e["target"] == subj["id"]


def test_promote_idempotent_by_ref():
    """同一 pk 重复提升不产生第二个节点。"""
    conn = FakeConn(people={"李志强": ["person_07ed989d84fd"]})
    a = cc.build_subject_node(case_id="C1", name="李志强", conn=conn)
    b = cc.build_subject_node(case_id="C1", name="李志强", conn=conn)
    assert a["id"] == b["id"]


# ======================================================================
# SYM-01 精度档：木桶效应不得被 unknown 吞掉
# ======================================================================
def test_weaker_precision_bucket():
    assert cc.weaker_precision("minute", "date") == "date"
    assert cc.weaker_precision("date", "minute") == "date"
    assert cc.weaker_precision("second", "minute") == "minute"


def test_weaker_precision_first_hit_adopted():
    """首次命中直接采用——否则 unknown 初始值永远赢，把真实精度吞掉。"""
    assert cc.weaker_precision(None, "minute") == "minute"
    assert cc.weaker_precision("date", None) == "date"


def test_weaker_precision_mutation_caught():
    """反向验证：unknown 吞掉真实精度的旧 bug 必须被拦。"""
    got = cc.weaker_precision(None, "minute")
    assert got == "minute", "unknown 吞掉真实精度未被拦截"


def test_norm_precision_alias_and_unknown():
    assert cc.norm_precision("min") == "minute"
    assert cc.norm_precision("日期级") == "date"
    assert cc.norm_precision("火星历") == "unknown"
    assert cc.norm_precision(None) == "unknown"


def test_can_overlap_only_fine_grained():
    assert cc.can_overlap("minute") and cc.can_overlap("second")
    assert not cc.can_overlap("date") and not cc.can_overlap("unknown")


# ======================================================================
# SYM-02 符号口径：date 与 minute 在图上必须长得不一样
# ======================================================================
def test_line_style_differs_by_precision():
    m, d = cc.line_style("minute"), cc.line_style("date")
    assert m["stroke_dasharray"] is None and d["stroke_dasharray"] == "4 3"
    assert m["line_width"] > d["line_width"]


def test_line_style_mutation_caught():
    """反向验证：date 档画成实线的变异必须失败。"""
    d = cc.line_style("date")
    assert d["stroke_dasharray"] is not None, "date 档被画成实线未拦截"


def test_symbol_solid_flag():
    assert cc.symbol("minute")["solid"] is True
    assert cc.symbol("date")["solid"] is False
    assert cc.symbol("minute")["weight"] > cc.symbol("date")["weight"]


def test_symbol_weight_never_rewards_count():
    """12 条 date 档的合计权重不得反超 3 条 minute 档。"""
    assert 12 * cc.symbol("date")["weight"] < 3 * cc.symbol("minute")["weight"]


# ======================================================================
# HYP-01/02 假设节点自动入图
# ======================================================================
def _result(case_id, ref, assumption=None, assumptions=None, precision="minute"):
    return cc.build_result_node(case_id=case_id, result_ref=ref,
                                lens_id="geo_accompany",
                                assumption=assumption,
                                precision=precision,
                                extra={"assumptions": assumptions} if assumptions else None)


def test_hypothesis_auto_created_from_results():
    nodes = [_result("C1", "r1", assumption="H6"),
             _result("C1", "r2", assumption="H3")]
    layer = cc.build_hypothesis_layer(case_id="C1", nodes=nodes)
    assert set(layer["ids"]) == {"H6", "H3"}
    assert len(layer["nodes"]) == 2
    for n in layer["nodes"]:
        assert n["kind"] == "hypothesis"
        assert n["id"].startswith("case#C1:hypothesis:")
        # 标题取本体，不手写
        assert n["props"]["title"]


def test_hypothesis_dedup_one_node_per_id():
    """三条证据指向同一假设，只建一个假设节点。"""
    nodes = [_result("C1", f"r{i}", assumption="H6") for i in range(3)]
    layer = cc.build_hypothesis_layer(case_id="C1", nodes=nodes)
    assert len(layer["nodes"]) == 1


def test_hypothesis_unknown_id_flagged_not_faked():
    """本体未声明的假设：不得编造证伪条件，须写明。"""
    nodes = [_result("C1", "r1", assumption="H99")]
    layer = cc.build_hypothesis_layer(case_id="C1", nodes=nodes)
    n = layer["nodes"][0]
    assert n["props"]["known_in_ontology"] is False
    assert n["props"]["falsification"] is None
    assert "未在本体模式库声明" in n["props"]["note"]
    assert "H99" in layer["missing"]


def test_hypothesis_title_from_ontology_not_handwritten():
    """反向验证：标题若被手写替换，必须能被发现。"""
    nodes = [_result("C1", "r1", assumption="H1")]
    n = cc.build_hypothesis_node(case_id="C1", hypothesis_id="H1")
    assert "收受财物" in (n["props"]["title"] or ""), "标题未取本体"


# ======================================================================
# HYP-03/04 系统推断边
# ======================================================================
def test_infer_edge_created_and_is_system():
    r = _result("C1", "r1", assumption="H6")
    edges = cc.build_infer_edges(case_id="C1", result_nodes=[r])
    assert len(edges) == 1
    e = edges[0]
    assert e["rel"] == cc.INFER_REL == "推断为"
    assert e["system"] is True          # 可删不可改
    assert e["target"] == "case#C1:hypothesis:H6"


def test_infer_edge_never_emits_refute():
    """系统只出「推断为」——反驳方向留人工，机器没有这个知识。"""
    r = _result("C1", "r1", assumption="H6")
    edges = cc.build_infer_edges(case_id="C1", result_nodes=[r])
    assert all(e["rel"] not in ("查否", "证实") for e in edges)


def test_infer_edge_multi_assumption_no_autopick():
    """多归属出多条边，绝不静默挑一条（与 R-1 同源）。"""
    r = _result("C1", "r1", assumptions=["H1", "H6"])
    edges = cc.build_infer_edges(case_id="C1", result_nodes=[r])
    assert len(edges) == 2
    assert {e["target"] for e in edges} == {"case#C1:hypothesis:H1",
                                            "case#C1:hypothesis:H6"}


def test_infer_edge_precision_carried():
    """精度档必须跟随：date 档虚线细边，不得与 minute 档同形。"""
    fine = cc.build_infer_edges(case_id="C1",
                                result_nodes=[_result("C1", "r1", "H6", precision="minute")])[0]
    coarse = cc.build_infer_edges(case_id="C1",
                                  result_nodes=[_result("C1", "r2", "H6", precision="date")])[0]
    assert fine["stroke_dasharray"] is None
    assert coarse["stroke_dasharray"] == "4 3"
    assert fine["line_width"] > coarse["line_width"]


def test_infer_edge_mutation_caught():
    """反向验证：date 档边被画成实线的变异必须失败。"""
    e = cc.build_infer_edges(case_id="C1",
                             result_nodes=[_result("C1", "r", "H6", precision="date")])[0]
    assert e["stroke_dasharray"] is not None, "date 档边被画成实线未拦截"


# ======================================================================
# CAN-07 人工可加主体（未收录人员）
# ======================================================================
def test_manual_subject_allowed_and_unanchored():
    """数据里没有的人也能摆上图，但必须显式声明未锚定。"""
    from server.app.canvas_edit import (MANUAL_NODE_KINDS,
                                        validate_node_props)
    assert "subject" in MANUAL_NODE_KINDS
    props = validate_node_props("subject", {"name": "走访听到的某人"})
    assert props["name"] == "走访听到的某人"
    assert props["anchored"] == "false"      # 未锚定，跑不出研判
    assert props["person_pk"] == ""


def test_manual_subject_name_required():
    from server.app.canvas_edit import validate_node_props, CanvasEditError
    with pytest.raises(CanvasEditError):
        validate_node_props("subject", {"name": "   "})
    with pytest.raises(CanvasEditError):
        validate_node_props("subject", {"content": "只有说明"})


def test_manual_subject_sub_type():
    """主体子类型可扩展（物品/组织复用同一 subject）。"""
    from server.app.canvas_edit import validate_node_props
    p = validate_node_props("subject", {"name": "浙A12345",
                                        "sub_type": "item_vehicle"})
    assert p["sub_type"] == "item_vehicle"


def test_result_can_infer_to_hypothesis():
    """研判结论能挂假设——它是挂假设的主力证据。"""
    from server.app.canvas_edit import can_connect, allowed_rels
    ok, why = can_connect("analysis_result", "hypothesis", "推断为")
    assert ok, why
    assert "推断为" in allowed_rels("analysis_result", "hypothesis")


def test_subject_can_infer_to_hypothesis():
    from server.app.canvas_edit import can_connect
    ok, why = can_connect("subject", "hypothesis", "推断为")
    assert ok, why


def test_note_direction_unchanged():
    """既有矩阵不被破坏：备注只能作起点。"""
    from server.app.canvas_edit import can_connect
    assert can_connect("fact", "note", "补充说明")[0] is False
    assert can_connect("note", "fact", "补充说明")[0] is True


def test_identity_conflict_short_rows_no_indexerror():
    """身份证据表只有 id_card 列（无 phone）时不得 IndexError。

    这是「证号列索引写死 row[2]」那次静默越界的同类复发：
    ``{... for r in rows if r and r[1]}`` 里的 r[1] 在判空之前就被求值，
    单行元组时直接抛 IndexError——而且发生在**重名裁决**路径上，
    症状是张卫国这类同名主体在建节点时直接崩，不是"标不出来"那么温和。
    """
    class _Conn:
        def execute(self, _sql, _args=None):
            class _Cur:
                def fetchall(self):
                    # 只有一列：装载批次不同时 phone 列可能不存在
                    return [("3301...01234",), ("3301...45678",)]
            return _Cur()

    cf = cc.identity_conflict(_Conn(), "张卫国")
    assert cf["checked"] is True
    assert cf["conflict"] is True          # 两个证号互斥仍要判出来
    assert "身份证号互斥" in cf["evidence"]
    assert "手机号" not in cf["evidence"]   # 缺列不得编造手机冲突


def test_identity_conflict_short_rows_mutation_caught():
    """反向验证：把 _col 换回裸索引，上面的用例必须失败。"""
    src = open("server/app/canvas_case.py", encoding="utf-8").read()
    assert "_col(r, 0)" in src, "缺列保护被移除后本用例应当先失败"
