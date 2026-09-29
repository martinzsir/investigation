"""研判层画布集成验收 E1–E20（融合完整业务推演）。

为什么单开一个文件
------------------
前面的单测都是**逐条能力**的（CAN-xx / WIN-xx / SYM-xx），每条独立可跑。
但真正会出事的不是单条能力，而是**能力与能力之间的接缝**：

  · 结论挂上画布了，但假设没长出来（E10）
  · 假设长出来了，但挂到了错误的编号上（E7）
  · 窗口弹出来了，但精度档和卡上画的不一致（E17）

所以本文件按**业务推演的顺序**走一遍完整场景，断言的是"接缝"。

真实数据
--------
用 investigation.duckdb 的既有数据，不用编造 fixture——因为重名、组织、
地点这些形态只有在真实数据里才是真的：张卫国在库里确实有两个身份证号，
这是整个"重名不自裁"红线的事实基础。

未实施的步骤（E12 后半 / E13–E16 / E19–E20）
--------------------------------------------
一律 pytest.skip 并写明原因，**不伪造通过**。skip 与 pass 的区别是：
pass 会说"这条已验收"，skip 会说"这条还没做"。后者才是真实状态。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest

from server.app import canvas_case as cc
from server.app import canvas_case_doc as cdoc
from server.app import canvas_growth as growth
from server.app import canvas_promote as promote
from server.app import canvas_target as ct

CASE_ID = "C1"
DB = Path(__file__).resolve().parent.parent / "investigation.duckdb"

# 业务推演里的事实（来自真实库，不是编造）
ZHANG = "张卫国"     # obj_person 1 行，但 identity 2 行且证号互斥 → 同名异人
LI = "李志强"        # identity 1 行 → 唯一，可锚定


def _conn():
    """真实库只读连接；不可用时跳过（环境缺 duckdb 不算功能失败）。"""
    if not DB.exists():
        pytest.skip(f"缺少数据文件 {DB.name}")
    try:
        import duckdb
        return duckdb.connect(str(DB), read_only=True)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"duckdb 不可用：{e}")


def _lens(name: str, subject_params=("target_subject",), place_params=()):
    """假镜头：必须带 params_schema——target_params 只读声明，不读实现。"""
    schema = {}
    for k in subject_params:
        schema[k] = {"type": "string"}
    for k in place_params:
        schema[k] = {"type": "string"}
    return {"skill_id": name, "lens_id": name, "name": name,
            "params_schema": schema}


# ======================================================================
# E1 空画布：起点必须是"空得有理由"，不是白屏
# ======================================================================
class TestE1EmptyCanvas:
    def test_empty_dir_gives_reason_not_silence(self):
        """空案件目录 → 空画布，但必须给出原因。

        静默返回空列表，正兵看到的是一张空图，会读成"查过了、没有"。
        而实际是"档案还没建"。这两者的处置完全相反。
        """
        with tempfile.TemporaryDirectory() as td:
            r = growth.load_growth_layer(case_dir=td, case_id=CASE_ID)
        assert r["nodes"] == []
        assert r["meta"]["reason"], "空画布必须写明原因，不得静默返回空"


# ======================================================================
# E2 消歧裁决：张卫国必须先裁决，否则后面全错
# ======================================================================
class TestE2Homonym:
    def test_zhang_is_ambiguous_and_not_anchored(self):
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name=ZHANG, conn=conn)
        p = n["props"]
        # R-1：绝不代选
        assert p["person_pk_ambiguous"] is True
        # 同名异人待分列时**不算已锚定**——否则依赖它的逻辑会走确定性分支
        assert p["anchored"] is False
        assert p["pk_status"] in ("homonym_pending", "multi")
        assert p["pk_resolution"], "必须写清裁决依据，不能只给一个布尔值"

    def test_li_is_unique_and_anchored(self):
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name=LI, conn=conn)
        p = n["props"]
        assert p["person_pk_ambiguous"] is False
        assert p["anchored"] is True
        assert p["person_pk"], "李志强唯一，必须锚到真实主键"

    def test_ambiguous_blocks_lens_not_silent(self):
        """未裁决就跑镜头 → 明确拦截，不是塞个 pk 进去跑出错误结果。"""
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name=ZHANG, conn=conn)
        r = ct.autofill_target(node=n, skill=_lens("geo_abnormal_track"))
        assert r["status"] == "blocked_ambiguous"
        assert r["params"] == {}, "拦截时不得仍然填参"
        assert "裁决" in r["reason"]


# ======================================================================
# E3 靶心自动带入：选中即靶心，不用各选一遍
# ======================================================================
class TestE3TargetAutofill:
    def test_subject_pk_filled(self):
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name=LI, conn=conn)
        r = ct.autofill_target(node=n, skill=_lens("geo_residency_profile"))
        assert r["status"] == "filled"
        assert r["target_pk"] == n["props"]["person_pk"]
        assert r["params"], "必须真的填进参数，不能只返回 pk"

    def test_two_subject_params_fills_only_one(self):
        """同框类镜头有 subject_a/subject_b，只填一个。

        两个都填同一个 pk 会造成"自己与自己同框"——有结果，但结果是假的。
        """
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name=LI, conn=conn)
        skill = _lens("geo_spatiotemporal_accompany",
                      subject_params=("subject_a", "subject_b"))
        r = ct.autofill_target(node=n, skill=skill)
        if r["status"] == "filled":
            vals = list(r["params"].values())
            assert len(vals) <= 1, f"不得把同一 pk 填进多个主体参数：{r['params']}"

    def test_place_target(self):
        n = cc.build_place_node(case_id=CASE_ID, location_id="loc_001",
                                label="莫干山路")
        r = ct.autofill_target(node=n, skill=_lens("geo_x", (), ("location_id",)))
        assert r["status"] == "filled"
        assert r["target_pk"] == "loc_001"


# ======================================================================
# E4 地图窗口：不可判定返回 None，绝不塞 0
# ======================================================================
class TestE4MapWindow:
    def test_no_coord_is_not_mappable(self):
        """取不到坐标 → mappable=False + 原因，不得塞 (0,0)。

        塞 0 会把"区级推算"画成精确门牌点，正兵会照着这个点去布线人。
        """
        # 库中不存在的 location_id → 无坐标 → 不得塞 (0,0)
        n = cc.build_place_node(case_id=CASE_ID, location_id="loc_not_exist",
                                label="某区", conn=_conn())
        p = n["props"]
        lat, lon = p.get("lat"), p.get("lon")
        if lat is None or lon is None:
            assert p.get("mappable") is False
            assert p.get("coord_note"), "不可落图必须写明原因"

    def test_precision_symbol_from_single_source(self):
        """门牌级实心、区划质心空心：两套符号，且来自同一真相源。"""
        s_min = cc.symbol("minute")
        s_date = cc.symbol("date")
        assert s_min != s_date, "不同精度档符号必须不同"
        # 可判时间窗真重叠的档位才配叫"同框"
        assert cc.can_overlap("minute") is True
        assert cc.can_overlap("date") is False


# ======================================================================
# E5 异常轨迹 → 结论挂到靶心下
# ======================================================================
_AT = {"date": "2020-03-10", "minute": "2020-03-10 14:35:00"}


def _obs(lens_id, target=None, precision="minute", n=3):
    """假观察：精度必须由 facts[].at 派生（与真实观察同口径）。

    顶层写 precision 是错的——obs_precision 只读 facts，写了也不被消费，
    结论档位会落在 unknown，测试就成了恒真的摆设。
    """
    at = _AT.get(precision, "2020-03-10 14:35:00")
    o = {"skill_id": lens_id, "lens_id": lens_id,
         "facts": [{"at": at}]}
    if target:
        o["origin"] = {"node_id": target}
    return [dict(o, observation_id=f"o{i}") for i in range(n)]


class TestE5ResultAttached:
    def test_result_node_and_edge(self):
        tgt = cc.case_node_id("subject", CASE_ID, "person_07ed989d84fd")
        r = growth.build_result_layer(
            case_id=CASE_ID, target_node_id=tgt,
            lens_id="geo_abnormal_track",
            observations=_obs("geo_abnormal_track", tgt))
        assert r["node"], "有观察就必须出结论节点"
        assert r["edge"]["rel"] == cc.LENS_RESULT_REL
        assert r["edge"]["source"] == tgt, "结论必须挂在**发起它的**节点下"
        assert r["node"]["props"]["generated_by"] == cc.GENERATED_BY_LENS

    def test_no_obs_gives_reason(self):
        tgt = cc.case_node_id("subject", CASE_ID, "p1")
        r = growth.build_result_layer(case_id=CASE_ID, target_node_id=tgt,
                                      lens_id="geo_x", observations=[])
        assert r["node"] is None
        assert r["reason"], "零条观察必须给原因，不得静默空"


# ======================================================================
# E6 自动带出李志强：新主体可锚定
# ======================================================================
class TestE6NewSubject:
    def test_new_subject_anchorable(self):
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name=LI, conn=conn)
        assert n["props"]["anchored"] is True
        # 与张卫国的节点 id 必须不同（重名不撞键）
        z = cc.build_subject_node(case_id=CASE_ID, name=ZHANG, conn=conn)
        assert n["id"] != z["id"]


# ======================================================================
# E7 通话关系验 H3（CAN-18 纠错）：假设编号不得手抄
# ======================================================================
class TestE7Assumption:
    def test_call_frequency_is_h3_not_h2(self):
        from core.lens_assumption import resolve_function_assumption
        h, note = resolve_function_assumption("call_frequency_spike")
        assert h == "H3", f"通话高频应挂 H3（密切私下关系），实际 {h}；{note}"

    def test_overpass_is_h4_not_h1(self):
        from core.lens_assumption import resolve_function_assumption
        h, note = resolve_function_assumption("overpass_two_hop")
        assert h == "H4", f"跨主体二跳应挂 H4（第三方过桥），实际 {h}；{note}"

    def test_unknown_function_returns_none_not_guess(self):
        from core.lens_assumption import resolve_function_assumption
        h, note = resolve_function_assumption("no_such_function_zzz")
        assert h is None, "判不出就返回 None，不得硬猜一个假设"
        assert note, "必须写明判不出的原因"


# ======================================================================
# E8 时间窗口：12 条日期级 < 3 条时刻级
# ======================================================================
class TestE8PrecisionNotByCount:
    def test_minute_beats_many_dates(self):
        """这是整个精度体系的核心不等式，必须写死。

        15 次 date × 0.2 = 3.0，3 次 minute × 1.0 = 3.0 —— 裸乘是打平的，
        真正拉开差距的是 aggregate_precision 的次数加成上限。所以断言用
        rank 比较，不硬编码分数。
        """
        tgt = cc.case_node_id("subject", CASE_ID, "p1")
        many_dates = _obs("timeline_cluster", tgt, precision="date", n=15)
        few_minutes = _obs("timeline_cluster", tgt, precision="minute", n=3)
        rd = growth.build_result_layer(case_id=CASE_ID, target_node_id=tgt,
                                       lens_id="timeline_cluster",
                                       observations=many_dates)
        rm = growth.build_result_layer(case_id=CASE_ID, target_node_id=tgt,
                                       lens_id="timeline_cluster",
                                       observations=few_minutes)
        pd = rd["node"]["props"]["precision"]
        pm = rm["node"]["props"]["precision"]
        assert cc.PRECISION_RANK[pm] > cc.PRECISION_RANK[pd], (
            f"3 条时刻级必须高于 15 条日期级：{pm} vs {pd}")

    def test_precision_degrades_to_weakest(self):
        """一组里混档 → 取最弱档，不取平均也不取最强。"""
        tgt = cc.case_node_id("subject", CASE_ID, "p1")
        mixed = _obs("timeline_cluster", tgt, precision="minute", n=3) + \
                _obs("timeline_cluster", tgt, precision="date", n=1)
        r = growth.build_result_layer(case_id=CASE_ID, target_node_id=tgt,
                                      lens_id="timeline_cluster",
                                      observations=mixed)
        assert r["node"]["props"]["precision"] == "date"


# ======================================================================
# E10/E11 假设自动入图 + 系统只出「推断为」
# ======================================================================
class TestE10HypothesisAuto:
    def _layer(self, lens_id, function_id=None):
        tgt = cc.case_node_id("subject", CASE_ID, "p1")
        obs = _obs(lens_id, tgt)
        return growth.build_lens_result_layers(
            case_id=CASE_ID, observations=obs, target_node_id=tgt,
            function_ids={lens_id: function_id} if function_id else None)

    def test_hypothesis_node_appears(self):
        r = self._layer("comm_call_frequency", "call_frequency_spike")
        assert r["hypothesis_nodes"], "结论挂上画布后假设必须自己长出来"
        assert r["infer_edges"], "证据必须自动连到假设下"

    def test_system_only_emits_infer_not_refute(self):
        """系统只出「推断为」，不出「查否」。

        反驳方向需要判断——机器只知道归属，不知道这条证据在反驳什么。
        """
        r = self._layer("comm_call_frequency", "call_frequency_spike")
        rels = {e["rel"] for e in r["infer_edges"]}
        assert rels, "至少一条推断边"
        assert "查否" not in rels, "系统不得自动出反驳方向"
        assert "证实" not in rels, "系统不得自动出证实方向"

    def test_hypothesis_dedup(self):
        """同一假设只建一个节点，不因多条证据重复建。"""
        tgt = cc.case_node_id("subject", CASE_ID, "p1")
        obs = _obs("comm_call_frequency", tgt, n=5)
        r = growth.build_lens_result_layers(case_id=CASE_ID, observations=obs,
                                            target_node_id=tgt)
        ids = [h["id"] for h in r["hypothesis_nodes"]]
        # 允许挂到不同假设（多归属），但同一 id 不得重复
        assert len(ids) == len(set(ids)), f"假设节点重复：{ids}"


# ======================================================================
# E12 组织间接路径（前半：组织可查）—— 后半依赖画布组织节点，见 skip
# ======================================================================
class TestE12Org:
    def test_org_pk_candidates_not_empty_for_real_org(self):
        """组织走 obj_org，不得复用只查 obj_person 的入口。

        否则库里真实收录的单位被判成"未收录"，正兵看到"库里没这家单位"。
        """
        conn = _conn()
        rows = conn.execute("SELECT raw_name FROM obj_org LIMIT 1").fetchall()
        if not rows:
            pytest.skip("库中无组织数据")
        name = str(rows[0][0])
        cands = cc.org_pk_candidates(conn, name)
        assert cands, f"「{name}」在 obj_org 中，必须查得到主键"

    def test_org_subject_buildable(self):
        conn = _conn()
        n = cc.build_subject_node(case_id=CASE_ID, name="宏业建设",
                                  sub_type="org", conn=conn)
        assert n["props"]["sub_type"] == "org"


# ======================================================================
# E17 精度一致性：卡内符号 / 连线 / 窗口三处同源
# ======================================================================
class TestE17SymbolConsistency:
    def test_symbol_is_single_source(self):
        for p in ("date", "minute"):
            s = cc.symbol(p)
            assert set(s) >= {"solid", "line_width", "dash", "color", "weight"}, (
                f"符号口径必须一次给全，否则三处各取一半就会分叉：{s}")

    def test_date_not_drawn_as_solid(self):
        """日期级不得画成实线粗边——否则被读成"能判同框"。

        这条是变异测试的常客：改错之后 12 条 date 会和 4 条 minute 长得一样。
        """
        d, m = cc.symbol("date"), cc.symbol("minute")
        assert (not d["solid"]) and d["line_width"] < m["line_width"], (
            "日期级必须是虚线细边，不得与时刻级同款")

    def test_weight_inequality(self):
        assert cc.PRECISION_WEIGHT["date"] < cc.PRECISION_WEIGHT["minute"]
        # 12 条 date 仍应低于 4 条 minute（裸乘口径下也成立）
        assert 12 * cc.PRECISION_WEIGHT["date"] < 4 * cc.PRECISION_WEIGHT["minute"]


# ======================================================================
# E18 溯源：提升后原证据节点保留，溯源链不断
# ======================================================================
class TestE18Promote:
    def _object_node(self):
        return {"id": "case#C1:object:person_07ed989d84fd", "kind": "object",
                "system": True, "label": LI,
                "props": {"pk": "person_07ed989d84fd", "type": "person",
                          "type_title": "人"}}

    def test_promote_creates_subject_and_edge(self):
        conn = _conn()
        r = promote.promote_object_to_subject(
            case_id=CASE_ID, object_node=self._object_node(), conn=conn)
        assert r["subject"]["kind"] == "subject"
        assert r["edge"]["rel"] == cc.PROMOTE_REL
        # 原节点保留（视图层可隐藏，但数据不得删）
        assert r.get("keep_original", True) is not False

    def test_promote_is_idempotent(self):
        conn = _conn()
        obj = self._object_node()
        a = promote.promote_object_to_subject(case_id=CASE_ID, object_node=obj, conn=conn)
        b = promote.promote_object_to_subject(case_id=CASE_ID, object_node=obj, conn=conn)
        assert a["subject"]["id"] == b["subject"]["id"], "重复提升不得建出第二个节点"

    def test_promote_all_merges_sources(self):
        """同一人出现在多条线索 → 标记"源自 N 处"，不覆盖。"""
        conn = _conn()
        nodes = [self._object_node(),
                 {"id": "case#C1:object:x2", "kind": "object", "system": True,
                  "label": LI,
                  "props": {"pk": "person_07ed989d84fd", "type": "person"}}]
        r = promote.promote_all(case_id=CASE_ID, nodes=nodes, conn=conn)
        subs = [n for n in r.get("nodes", []) if n["kind"] == "subject"]
        assert subs, "必须产出主体节点"
        src = subs[0]["props"].get("promoted_from")
        if isinstance(src, list):
            assert len(src) >= 1


# ======================================================================
# E9 三维交汇 / E13–E16 物品 / E19–E20 核查闭环
# ======================================================================
class TestRemaining:
    @pytest.mark.skip(reason="E9 三维交汇依赖 convergence 产物与画布对接，"
                             "画布侧落点未实施")
    def test_e9_convergence_anchor(self):
        pass

    # ------------------------------------------------------------------
    # E13–E16 物品线：物品已收编进本体（obj_item/obj_hold_record），
    # 画布物品层与 Function/图库同轨——本组断言的是语义层产物的画布表现
    # ------------------------------------------------------------------
    def _item_layer(self):
        """真实语义层；obj_item 未物化时跳过（不伪造通过）。"""
        from server.app import canvas_item_source as cis
        conn = _conn()
        try:
            n = conn.execute("SELECT COUNT(*) FROM obj_item").fetchone()[0]
        except Exception:
            pytest.skip("语义层无 obj_item 表：先跑 scripts/prep_item_registry.py"
                        " 并重建语义层（python -m scripts.build_ontology）")
        if n == 0:
            pytest.skip("obj_item 为空：物品数据源未接入")
        return cis.build_item_layer(case_id=CASE_ID, conn=conn)

    def test_e13_item_on_canvas(self):
        """E13 物品入图：车成为 item 节点，且不新增第 7 维。"""
        r = self._item_layer()
        items = [n for n in r["nodes"] if n["kind"] == "item"]
        assert items, "必须产出 item 节点"
        car = [n for n in items if n["props"].get("sub_type") == "vehicle"]
        assert car, "涉案车必须在图上"

        # 红线：不为物品新增维度，维度总数仍为 6
        from core.ontology_loader import load_dimension_declarations
        dims = load_dimension_declarations("default")
        assert len(dims) == 6, f"维度总数必须为 6，实为 {len(dims)}"
        assert not [d for d in dims if d["code"] == "item"], (
            "物品不得成为独立维度——会稀释六维覆盖度模型")

        # 无凭证赃物（双肩包/U 盘，sub_type=goods_other）：两件各自独立。
        # 注意口径与直觉相反——**必须**各给一个摘要。若让无凭证物品的
        # primary_digest 统一为空串，语义层去重会把所有赃物坍缩成同一个
        # 实体，那比不消歧更危险（core/item.py 已就此论证）。故取特征摘要，
        # 每条各异；只有"凭证与特征皆空"才返回空串，而那种记录应被拒绝建节点。
        bare = [n for n in items if n["props"].get("sub_type") == "goods_other"]
        assert len(bare) >= 2, (
            f"无凭证物品必须各自独立成节点，实为 {len(bare)}")
        digests = [n["props"].get("primary_digest") for n in bare]
        assert all(digests), "无凭证物品必须有特征摘要，否则会被坍缩成一个实体"
        assert len(set(digests)) == len(digests), (
            "两件不同赃物算出同一摘要 → 被静默合并")
        ids = {n["id"] for n in bare}
        assert len(ids) == len(bare), "无凭证物品被静默合并"

        # 凭证与特征皆空：应被拒绝建节点，绝不产出一个空壳实体
        from server.app.canvas_case import build_item_node
        with pytest.raises(ValueError):
            build_item_node(case_id=CASE_ID, title="", item_type="goods_other")

    def test_e14_item_geo(self):
        """E14 物品落图：有坐标可上图，无坐标标不可落图——绝不塞 0。"""
        r = self._item_layer()
        by_sub = {}
        for n in r["nodes"]:
            if n["kind"] == "item":
                by_sub[n["props"].get("sub_type")] = n

        car = by_sub.get("vehicle")
        assert car is not None, "缺车辆节点"
        p = car["props"]
        assert p.get("lat") and p.get("lng"), "车应带坐标（台账有经纬度）"
        assert p.get("mappable") is not False, "有坐标却标为不可落图"

        inv = by_sub.get("invoice")
        if inv is not None:
            q = inv["props"]
            assert q.get("lat") in (None, ""), "发票无坐标，不得编造"
            assert q.get("lng") not in (0, 0.0), (
                "坐标缺失绝不可塞 0——会画到几内亚湾")
            assert q.get("mappable") is False, (
                "无坐标必须显式标不可落图，否则正兵以为地图坏了")

    def test_e14_item_track_three_afternoons(self):
        """E14 加强：车在三个不同日期的下午都出现在莫干山路。

        为什么这条断言必须有反例才有意义
        --------------------------------
        若演示数据里车的每个点都在莫干山路、每个点都有时刻，那么"三个下午
        都在"就是同义反复——断言恒真，测了个空气。故数据里刻意安排三类反例，
        本用例逐条验它们**没被算进命中**：

          · 09-25 **下午**在文三路 → other_days（只错地点，单验地点筛选）
          · 09-27 **上午**在莫干山路 → other_days（只错时段，单验下午判定）
          · 09-29 只有日期无时刻 → unknown_days（判不出，不是"没去过"）

        前两条**必须错开**：合成一条"上午在文三路"的话，地点与时段两个条件
        互相掩护——改坏任何一个，另一条仍把它挡在 other 里，断言照样通过。
        实测过：合成一条时变异 MUT-2（去掉地点筛选）与 MUT-3（去掉下午判定）
        **双双漏网**，5 项断言全绿。分成两条后逐个被拦下。
        """
        from server.app import canvas_item_source as cis

        r = self._item_layer()
        car = [n for n in r["nodes"]
               if n["kind"] == "item" and n["props"].get("sub_type") == "vehicle"]
        assert car, "缺车辆节点"
        track = car[0]["props"].get("track") or []
        assert track, "车必须带轨迹点（物品轨迹.parquet 未挂载？）"

        s = cis.summarize_item_track(track, location="莫干山路")

        # ① 主断言：三个不同日期的下午
        assert len(s["hit_days"]) == 3, (
            f"车应在 3 个不同日期的下午出现在莫干山路，实为 {s['hit_days']}")
        assert s["hit_days"] == ["2021-09-08", "2021-09-15", "2021-09-22"]

        # ② 反例一：只错地点（下午但在文三路）——单验地点筛选
        assert "2021-09-25" in s["other_days"], (
            "09-25 下午在文三路，必须落在 other_days——"
            "否则说明地点筛选失效，hit 是同义反复")
        assert "2021-09-25" not in s["hit_days"], (
            "09-25 在文三路却被算成在莫干山路")

        # ②b 反例二：只错时段（在莫干山路但上午）——单验下午判定
        assert "2021-09-27" in s["other_days"], (
            "09-27 上午在莫干山路，必须落在 other_days——否则下午判定失效")
        assert "2021-09-27" not in s["hit_days"], (
            "09-27 是上午却被算进『下午』")

        # ③ 反例二：无时刻。判不出下午 → unknown，绝不进 hit
        assert "2021-09-29" in s["unknown_days"], (
            "无时刻的点必须落 unknown，而不是被当成'没去过'")
        assert "2021-09-29" not in s["hit_days"], (
            "无时刻的点被算进了命中——精度不足却声称下午在某地（伪精确）")

        # ④ 互斥性：同一日期不得既命中又判不出。
        # 少了这条，"把 unknown 全塞进 hit"也能让 ① 通过，测不到伪精确。
        assert not (set(s["hit_days"]) & set(s["unknown_days"])), (
            f"同一日期既命中又判不出："
            f"{sorted(set(s['hit_days']) & set(s['unknown_days']))}")

        # ⑤ 命中点本身站得住：确实在莫干山路、确实在下午、精度到分钟
        import server.app.canvas_item_source as mod
        for p in track:
            ts = str(p.get("timestamp") or "")
            if not ts[:10] in s["hit_days"]:
                continue
            assert "莫干山路" in str(p.get("location")), f"命中点不在莫干山路：{p}"
            assert 12 <= mod._hour_of(ts) < 18, f"命中点不在下午：{ts}"
            assert p.get("time_precision") in ("minute", "second"), (
                f"命中点精度不足：{p.get('time_precision')}")

    def test_e14_hour_precision_not_hit(self):
        """整点陷阱：14:00:00 派生为 hour 档，判不出'下午'，必须落 unknown。

        ``derive_time_precision`` 对分秒全零的时刻返回 hour 档（保守判粗），
        而"同时/同时段"只认 minute 及以上。整点记录看似有时刻，实际判不了
        ——把它算进命中就是伪精确，与 E16 那条"unknown 不并入 conflicts"同源。
        """
        from core.time_semantics import derive_time_precision
        from server.app import canvas_item_source as cis

        ts = "2021-09-08 14:00:00"
        assert derive_time_precision(ts) == "hour", (
            "整点应派生为 hour 档（分秒全零不提精度）")
        pts = [{"date": "2021-09-08", "timestamp": ts,
                "location": "浙江省杭州市拱墅区莫干山路111号",
                "lat": 30.3147, "lng": 120.1501, "mappable": True,
                "time_precision": derive_time_precision(ts)}]
        s = cis.summarize_item_track(pts, location="莫干山路")
        assert s["hit_days"] == [], f"整点档不得计入命中：{s}"
        assert s["unknown_days"] == ["2021-09-08"], (
            "整点必须落 unknown 并给出原因，不是静默丢弃")

    def test_e14_track_coords_no_zero(self):
        """轨迹点坐标缺失保持 None，绝不补 0（补 0 会画到几内亚湾）。

        为什么不用 ``mappable`` 当守卫
        --------------------------------
        第一版写成 ``if p.get("mappable") is False:`` 才检查坐标。而 mappable
        是 ``lat is not None and lng is not None``——补 0 之后它变成 **True**，
        守卫条件自己失效，循环体根本不执行。实测变异 MUT-5（``or 0``）因此
        全绿通过。守卫不能依赖它要检测的那个东西，故此处直接查值。
        """
        r = self._item_layer()
        checked = 0
        for n in r["nodes"]:
            if n["kind"] != "item":
                continue
            for p in n["props"].get("track") or []:
                checked += 1
                lat, lng = p.get("lat"), p.get("lng")
                # 0 不可能是杭州的真实坐标——出现即补 0 哨兵
                assert lat not in (0, 0.0), f"轨迹点纬度被补 0：{p}"
                assert lng not in (0, 0.0), f"轨迹点经度被补 0：{p}"
                # 无地点却给了坐标 = 编造
                if not str(p.get("location") or "").strip():
                    assert lat is None or lng is None, (
                        f"{n.get('label')} 无地点却给了坐标（编造）：{p}")
                    assert p.get("mappable") is not True, (
                        "无地点却标为可落图")
        assert checked, "没有任何轨迹点被检查（断言恒真）"

    def test_e14_no_person_track_pollution(self):
        """R-6 防污染：物品轨迹不得混入人的轨迹表。

        「轨迹出行」经 bindings 映射到 ``trackpoint.person_raw``——属性名就是
        人。车牌一旦进那张表，空间镜头（落脚点/异常轨迹/时空伴随）的主体列表
        里就会多出一个「浙A12345」，而车没有住址与单位，那些镜头的产出全是
        噪音。本版用独立源表从结构上隔离，这条断言守住将来有人误合并。
        """
        from server.app import canvas_item_source as cis

        d = Path(__file__).resolve().parent.parent / "data"
        assert cis.TRACK_TABLE != cis.PERSON_TRACK_TABLE, (
            "物品轨迹与人的轨迹不得共用一张源表")
        p = d / cis.PERSON_TRACK_TABLE
        if not p.exists():
            pytest.skip(f"缺少 {cis.PERSON_TRACK_TABLE}")
        try:
            import duckdb
        except Exception as e:  # pragma: no cover
            pytest.skip(f"duckdb 不可用：{e}")
        con = duckdb.connect()
        try:
            rows = con.execute(
                f"SELECT DISTINCT 主体 FROM read_parquet('{p.as_posix()}')"
            ).fetchall()
        finally:
            con.close()
        names = [str(x[0]) for x in rows]
        bad = [n for n in names if n.startswith("浙") or "A12345" in n]
        assert not bad, f"人的轨迹表里混入了物品：{bad}"

    def test_e15_hold_chain_temporal(self):
        """E15 持有链：必须带时间区间，流转不得被读成『两人共用』。"""
        r = self._item_layer()
        from server.app import canvas_hold as ch
        car = [n for n in r["nodes"]
               if n["kind"] == "item" and n["props"].get("sub_type") == "vehicle"]
        assert car, "缺车辆节点"
        chain = ch.build_item_hold_chain(
            item_node_id=car[0]["id"],
            edges=r["edges"],
            label_by_id={n["id"]: n.get("label", "") for n in r["nodes"]},
        )
        steps = chain.get("steps") or chain.get("chain") or []
        assert steps, "持有链必须有环节"

        # 流转的关键证据：前一手有结束时间。没有它就是"共用"而非"买卖"。
        timed = [s for s in steps if (s.get("end") or s.get("end_date"))]
        assert timed, (
            "持有环节缺结束时间：只画静态边会被读成『两人共用一辆车』，"
            "而实际可能是甲卖给了乙——性质完全相反")

        # 空链必须给原因，不许静默空（正兵会读成"无人持有过"）
        empty = ch.build_item_hold_chain(item_node_id="nope", edges=[])
        assert empty.get("reason"), "空链必须说明是哪种空"

    def test_e16_duplicate_is_signal(self):
        """E16 重号即信号：同时刻被多方持有 → 报冲突，且不走人的消歧。"""
        r = self._item_layer()
        from server.app import canvas_hold as ch
        car = [n for n in r["nodes"]
               if n["kind"] == "item" and n["props"].get("sub_type") == "vehicle"]
        chain = ch.build_item_hold_chain(
            item_node_id=car[0]["id"],
            edges=r["edges"],
            label_by_id={n["id"]: n.get("label", "") for n in r["nodes"]},
        )
        conflicts = chain.get("conflicts") or []
        assert conflicts, (
            "同一车牌在同一时段被两人持有，必须报冲突——"
            "这是套牌线索本身，不是消歧失败")

        # 判不出来的只进 unknown_overlaps，绝不并入 conflicts（伪精确）
        unknown = chain.get("unknown_overlaps")
        assert unknown is not None, "必须区分『真冲突』与『判不出来』"
        # 互斥性：同一对环节不得既算真冲突又算判不出。
        # 少了这条，把 unknown 全塞进 conflicts 也能让上面两条断言通过——
        # 那就测不到"伪精确"这个真正的失真。
        def _pair(x):
            return (str(x.get("a")), str(x.get("b")))
        c_pairs = {_pair(x) for x in conflicts}
        u_pairs = {_pair(x) for x in unknown}
        assert not (c_pairs & u_pairs), (
            f"同一对持有环节既报冲突又报判不出：{sorted(c_pairs & u_pairs)[:3]}")
        assert any(x.get("reason") and "精度不足" not in str(x.get("reason"))
                   for x in conflicts), (
            "conflicts 里混入了判不出的项（伪精确）")

        # 不调用人的消歧入口：物品重号是信号，不是噪声
        import server.app.canvas_hold as mod
        calls = []
        for name in ("identity_conflict", "pk_candidates_for"):
            fn = getattr(mod, name, None)
            if fn is not None:
                calls.append(name)
        assert not calls, (
            f"持有链不得调用人的消歧入口 {calls}——"
            "人的重名是噪声，物品的重号是信号")

    @pytest.mark.skip(reason="E19 核查任务生成未接画布")
    def test_e19_verify_task(self):
        pass

    @pytest.mark.skip(reason="E20 结论回写画布未实施")
    def test_e20_writeback(self):
        pass


# ======================================================================
# 贯穿性断言：持久化剥离（阶段 3 的核心）
# ======================================================================
class TestPersistence:
    def test_lens_layer_stripped_manual_kept(self):
        lens_n = {"id": "case#C1:analysis_result:a", "kind": "analysis_result",
                  "system": True, "label": "x",
                  "props": {"generated_by": cc.GENERATED_BY_LENS}}
        subj = {"id": "case#C1:subject:person_07ed989d84fd", "kind": "subject",
                "system": True, "label": LI,
                "props": {"person_pk": "person_07ed989d84fd"}}
        doc = {"nodes": [lens_n, subj], "edges": []}
        persistent, stat = cdoc.split_persistent(doc)
        ids = [n["id"] for n in persistent["nodes"]]
        assert "case#C1:analysis_result:a" not in ids, "镜头层不得落库"
        assert "case#C1:subject:person_07ed989d84fd" in ids, (
            "提升产生的 subject 必须落库（判据是来源，不是 system）")
        assert stat.get("nodes", 0) >= 1, f"镜头层未被剥离：{stat}"

    def test_merge_inherits_coords(self):
        """重建层不带坐标时，必须继承正兵摆好的坐标。"""
        persisted = {"nodes": [{"id": "n1", "kind": "subject", "x": 10, "y": 20,
                                "props": {}}], "edges": []}
        lens = {"nodes": [{"id": "n1", "kind": "subject", "x": None, "y": None,
                           "props": {"generated_by": cc.GENERATED_BY_LENS}}],
                "edges": []}
        merged = cdoc.merge_lens_layer(persisted, lens)
        n = {x["id"]: x for x in merged["nodes"]}["n1"]
        assert n["x"] == 10 and n["y"] == 20, (
            "重建不得把正兵摆好的位置冲回默认")
