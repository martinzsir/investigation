"""证据实体 → 研判主体提升（CAN-11 / CAN-12）。

每条用例独立可测；关键红线配反向验证——变异体必须让断言失败。
"""
from __future__ import annotations

import pytest

from server.app import canvas_edit as ce
from server.app import canvas_promote as cp


def _obj(node_id="obj1", otype="person", pk="person_abc",
         label="张卫国", name=None):
    props = {"type": otype, "pk": pk}
    if name:
        props["name"] = name
    return {"id": node_id, "kind": "object", "label": label, "props": props}


class _Cur:
    def __init__(self, rows, cols):
        self._rows = rows
        self.description = [(c, None) for c in cols]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """people: {name: [pk, ...]}"""

    def __init__(self, people=None):
        self.people = people or {}

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        if "obj_person" in s or "obj_person_identity" in s:
            nm = (params or [""])[0]
            return _Cur([(p,) for p in self.people.get(nm, [])], ["person_id"])
        return _Cur([], ["x"])


# ======================================================================
# CAN-11 提升：新建锚定节点 + 系统边，原节点保留
# ======================================================================
def test_promote_creates_anchored_subject():
    conn = FakeConn({"张卫国": ["person_aaa"]})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert res["status"] == "promoted"
    sub = res["subject"]
    assert sub["kind"] == "subject"
    assert sub["props"]["person_pk"] == "person_aaa"
    assert sub["props"]["anchored"] is True
    assert sub["props"]["person_pk_ambiguous"] is False


def test_promote_keeps_source_object():
    """原 object 保留在溯源层——隐藏会断掉"凭什么"这条链。"""
    conn = FakeConn({"张卫国": ["person_aaa"]})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert res["keeps_source"] is True
    assert res["edge"]["source"] == "obj1"
    assert res["edge"]["rel"] == "提升为"
    assert res["edge"]["system"] is True      # 可删不可改


def test_promote_idempotent():
    """同一 object 重复提升不产生第二个主体节点（id 由 ref 决定）。"""
    conn = FakeConn({"张卫国": ["person_aaa"]})
    a = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    b = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert a["subject"]["id"] == b["subject"]["id"]
    assert a["edge"]["id"] == b["edge"]["id"]

    # 批量口径同样幂等
    batch = cp.promote_all(case_id="C1", nodes=[_obj(), _obj()], conn=conn)
    assert len(batch["nodes"]) == 1
    assert batch["reports"][-1]["status"] == "duplicate"


def test_promote_homonym_no_autopick():
    """R-1：两个候选且不指定 → pk 置空 + ambiguous，绝不挑一个。"""
    conn = FakeConn({"张卫国": ["person_01234", "person_45678"]})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert res["status"] == "ambiguous"
    sub = res["subject"]
    assert sub["props"]["person_pk_ambiguous"] is True
    assert not sub["props"]["person_pk"]
    assert sub["props"]["anchored"] is False
    assert len(sub["props"]["pk_candidates"]) == 2
    assert "裁决" in res["reason"]
    # 反向验证：若实现改成"挑第一个"，下面必失败
    assert sub["props"]["person_pk"] != "person_01234"


def test_promote_requested_pk_must_be_in_candidates():
    """R-2：指定键不在候选内 → 拒绝。否则可随手绕过消歧。"""
    conn = FakeConn({"张卫国": ["person_01234", "person_45678"]})
    with pytest.raises(cp.PromoteError) as ei:
        cp.promote_object_to_subject(case_id="C1", object_node=_obj(),
                                     conn=conn, requested_pk="person_fake")
    assert "候选" in str(ei.value)


def test_promote_requested_pk_anchors():
    """正兵在候选里明确指定 → 锚定该键，且不再标歧义。"""
    conn = FakeConn({"张卫国": ["person_01234", "person_45678"]})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(),
                                       conn=conn, requested_pk="person_45678")
    assert res["status"] == "promoted"
    assert res["subject"]["props"]["person_pk"] == "person_45678"
    assert res["subject"]["props"]["person_pk_ambiguous"] is False
    assert "已裁决" in res["subject"]["props"]["pk_resolution"]


def test_promote_unanchored_still_creates_node():
    """R-3：查无此人照样建节点与边，但写明未锚定、无可用研判。"""
    conn = FakeConn({})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert res["status"] == "unanchored"
    assert res["subject"] is not None and res["edge"] is not None
    assert res["subject"]["props"]["anchored"] is False
    assert "无可用研判" in res["reason"]


def test_promote_unverified_when_no_conn():
    """无语义层连接时退回节点自带键：可锚定，但必须标"未经核对"。

    与 unmatched 的区别：前者是"没法核对"，后者是"核对过、确实没有"。
    """
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=None)
    assert res["status"] == "promoted"
    assert res["subject"]["props"]["pk_verified"] is False
    assert res["subject"]["props"]["pk_source"] == "node_pk_unverified"
    assert "未经语义层核对" in res["reason"]


def test_promote_never_anchors_on_unmatched_pk():
    """R-3 反向验证：语义层查无此人时，节点自带键不得当作锚定成功。

    变异体（把 node_pk_unmatched 也纳入自动锚定）会让下面两条同时失败。
    """
    conn = FakeConn({})          # 库里没有张卫国
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert res["status"] == "unanchored"
    assert res["subject"]["props"]["anchored"] is False
    # 自带键仍保留为候选，供人工确认——不丢信息，但不给"已锚定"的错觉
    assert res["candidates"] == ["person_abc"]


def test_promote_reject_non_object():
    with pytest.raises(cp.PromoteError):
        cp.promote_object_to_subject(case_id="C1",
                                     object_node={"id": "f1", "kind": "fact"})
    with pytest.raises(cp.PromoteError):
        cp.promote_object_to_subject(case_id="C1", object_node=None)


def test_promote_sub_type_from_object_type():
    conn = FakeConn({"某建筑公司": ["org_001"]})
    n = _obj(node_id="o2", otype="org", pk="org_001", label="某建筑公司",
             name="某建筑公司")
    res = cp.promote_object_to_subject(case_id="C1", object_node=n, conn=conn)
    assert res["subject"]["props"]["sub_type"] == "organization"


def test_promote_all_skips_non_subject_objects():
    """交易、通话等非主体对象不参与提升。"""
    conn = FakeConn({"张卫国": ["person_aaa"]})
    nodes = [_obj(), _obj(node_id="t1", otype="transaction", pk="tx_1",
                          label="一笔转账")]
    batch = cp.promote_all(case_id="C1", nodes=nodes, conn=conn)
    assert len(batch["nodes"]) == 1
    assert all(r["object_node_id"] != "t1" for r in batch["reports"])


# ======================================================================
# CAN-12 系统边与人工四类互斥
# ======================================================================
def test_system_rels_disjoint_from_manual():
    assert ce.system_only_rels().isdisjoint(ce.MANUAL_RELS)


def test_manual_cannot_create_promote_edge():
    """人工连线用「提升为」必须被拒，且原因指明"只能由系统建立"。"""
    ok, why = ce.can_connect("object", "subject", "提升为")
    assert ok is False
    assert "只能由系统建立" in why


def test_manual_rels_still_work():
    assert ce.can_connect("object", "hypothesis", "推断为")[0] is True
    assert ce.can_connect("evidence", "hypothesis", "证实")[0] is True


def test_system_edge_semantics():
    """提升边必须是系统边（可删不可改），与人工边视觉可辨。"""
    conn = FakeConn({"张卫国": ["person_aaa"]})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    e = res["edge"]
    assert e["system"] is True
    assert e["rel"] in ce.system_only_rels()
    # 反向验证：若提升边被建成人工边，互斥断言会失守
    assert e["rel"] not in ce.MANUAL_RELS


# ======================================================================
# 一张卡 + 溯源标记：多来源合并（不出现第二张同名卡）
# ======================================================================
def test_single_promote_origin_count_one():
    conn = FakeConn({"张卫国": ["person_aaa"]})
    res = cp.promote_object_to_subject(case_id="C1", object_node=_obj(), conn=conn)
    assert res["subject"]["props"]["promoted_from_refs"] == ["obj1"]
    assert res["subject"]["props"]["origin_count"] == 1


def test_promote_all_merges_multi_source():
    """同一主体出现在两条线索：只建一张卡，来源追加为 2 条。

    为什么不能判重复就跳过：refs 若只剩第一条线索，正兵点开溯源标记只会
    看到一个来源——"这个人被几条线索提到"正是要不要继续查的依据。
    """
    conn = FakeConn({"张卫国": ["person_aaa"]})
    batch = cp.promote_all(case_id="C1",
                           nodes=[_obj(node_id="obj1"), _obj(node_id="obj2")],
                           conn=conn)
    assert len(batch["nodes"]) == 1              # 图上只一张卡
    sub = batch["nodes"][0]
    assert sub["props"]["promoted_from_refs"] == ["obj1", "obj2"]
    assert sub["props"]["origin_count"] == 2
    dup = [r for r in batch["reports"] if r["status"] == "duplicate"]
    assert len(dup) == 1 and "追加" in dup[0]["reason"]


def test_promote_all_same_card_across_anchor_states():
    """第一次未裁决、第二次裁决：必须仍是同一张卡。

    各按自己算出的 id 建节点会分裂成两张同名卡（名字哈希 vs 主键），
    那正是要消掉的重复——所以按名字预扫裁决结果，统一主键建卡。
    """
    conn = FakeConn({"张卫国": ["person_aaa", "person_bbb"]})
    batch = cp.promote_all(case_id="C1",
                           nodes=[_obj(node_id="obj1"), _obj(node_id="obj2")],
                           conn=conn, requested={"obj2": "person_bbb"})
    assert len(batch["nodes"]) == 1
    sub = batch["nodes"][0]
    assert sub["props"]["person_pk"] == "person_bbb"
    assert sub["props"]["anchored"] is True
    assert sub["props"]["origin_count"] == 2      # 两条来源都记在这张卡上


def test_merge_does_not_downgrade_existing_anchor():
    """既有已锚定，新来源未锚定：不得把已锚定状态冲掉，但来源照常追加。"""
    existing = {"props": {"person_pk": "person_aaa", "anchored": True,
                          "promoted_from_refs": ["obj1"], "origin_count": 1}}
    cp._merge_promotion_source(existing,
                               {"props": {"person_pk": None, "anchored": False}},
                               "obj2")
    assert existing["props"]["person_pk"] == "person_aaa"
    assert existing["props"]["anchored"] is True
    assert existing["props"]["origin_count"] == 2


def test_multi_source_depends_on_merge(monkeypatch):
    """反向验证：合并失效时来源数退回 1——证明 2 不是碰巧得来的。"""
    monkeypatch.setattr(cp, "_merge_promotion_source",
                        lambda existing, incoming, nid: None)
    conn = FakeConn({"张卫国": ["person_aaa"]})
    batch = cp.promote_all(case_id="C1",
                           nodes=[_obj(node_id="obj1"), _obj(node_id="obj2")],
                           conn=conn)
    assert batch["nodes"][0]["props"]["origin_count"] == 1
