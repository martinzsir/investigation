"""靶心自动带入（CAN-15）与未锚定阻断（CAN-20）。

覆盖：选中即带入、重名不代选、未锚定明示原因、地点靶心、工具箱灰显。
"""
from __future__ import annotations

from server.app import canvas_target as ct


def _subj(pk=None, ambiguous=False, cands=None, name="张卫国", resolution=""):
    return {
        "id": "case#C1:subject:pk",
        "kind": "subject",
        "label": name,
        "props": {
            "name": name,
            "person_pk": pk,
            "person_pk_ambiguous": ambiguous,
            "pk_candidates": cands or ([pk] if pk else []),
            "anchored": pk is not None and not ambiguous,
            "pk_resolution": resolution,
        },
    }


def _skill(sid="geo_site_profile", params=None, name="落脚点画像"):
    return {
        "skill_id": sid,
        "name": name,
        "params_schema": params if params is not None else {
            "target_subject": {"type": "string", "required": True,
                               "description": "目标主体"},
        },
    }


# ======================================================================
# CAN-15 靶心自动带入
# ======================================================================
def test_autofill_fills_pk():
    """选中一次，镜头参数自动带上主键——不用再选一遍人。"""
    r = ct.autofill_target(node=_subj(pk="person_aaa"), skill=_skill())
    assert r["status"] == "filled"
    assert r["params"]["target_subject"] == "person_aaa"
    assert r["target_pk"] == "person_aaa"


def test_autofill_accepts_alias_param_names():
    """不同镜头参数名（subject / person / subject_a）都要认。"""
    for pname in ("subject", "person", "subject_a", "target"):
        sk = _skill(params={pname: {"type": "string"}})
        r = ct.autofill_target(node=_subj(pk="person_aaa"), skill=sk)
        assert r["status"] == "filled", pname
        assert r["params"][pname] == "person_aaa"


def test_autofill_only_fills_primary_subject():
    """双主体镜头只填主靶心——都填会变成"自己与自己同框"。"""
    sk = _skill(params={"subject_a": {"type": "string"},
                        "subject_b": {"type": "string"}})
    r = ct.autofill_target(node=_subj(pk="person_aaa"), skill=sk)
    assert r["params"] == {"subject_a": "person_aaa"}
    assert "subject_b" not in r["params"]


def test_autofill_place_target():
    node = {"id": "p1", "kind": "place", "label": "莫干山路",
            "props": {"name": "莫干山路", "location_id": "loc_9"}}
    sk = _skill(params={"location_id": {"type": "string"}})
    r = ct.autofill_target(node=node, skill=sk)
    assert r["status"] == "filled"
    assert r["params"]["location_id"] == "loc_9"


def test_no_target_param_skill_runnable():
    """全量扫描类镜头不需要靶心，应可直接跑（不是阻断）。"""
    sk = _skill(params={"radius_m": {"type": "integer"}})
    r = ct.autofill_target(node=_subj(pk="person_aaa"), skill=sk)
    assert r["status"] == "no_target_param"


def test_no_selection():
    r = ct.autofill_target(node=None, skill=_skill())
    assert r["status"] == "no_selection"
    assert r["params"] == {}


def test_wrong_kind():
    node = {"id": "e1", "kind": "event", "label": "中标", "props": {}}
    r = ct.autofill_target(node=node, skill=_skill())
    assert r["status"] == "wrong_kind"
    assert "研判主体" in r["reason"]


# ======================================================================
# R-1 重名不代选
# ======================================================================
def test_ambiguous_blocked_not_autopicked():
    """两个候选 → 不填任何 pk，交候选给正兵裁决。"""
    r = ct.autofill_target(
        node=_subj(pk=None, ambiguous=True,
                   cands=["person_01234", "person_45678"]),
        skill=_skill())
    assert r["status"] == ct.BLOCKED_AMBIGUOUS
    assert r["params"] == {}                      # 关键：不得代填
    assert r["target_pk"] is None
    assert len(r["candidates"]) == 2
    assert "不代为选择" in r["reason"]
    # 反向验证：若实现改成挑第一个，下面必失败
    assert "person_01234" not in r["params"].values()


# ======================================================================
# CAN-20 未锚定禁止跑研判并明示原因
# ======================================================================
def test_unanchored_blocked():
    r = ct.autofill_target(
        node=_subj(pk=None, resolution="语义层未收录「张卫国」，该节点未锚定，无可用研判"),
        skill=_skill())
    assert r["status"] == ct.BLOCKED_UNANCHORED
    assert r["params"] == {}
    assert "无可用研判" in r["reason"]      # 不是静默空结果


def test_unanchored_reason_default_when_missing():
    """即便节点没写 resolution，也要给出可读原因——不能返回空串。"""
    r = ct.autofill_target(node=_subj(pk=None), skill=_skill())
    assert r["status"] == ct.BLOCKED_UNANCHORED
    assert len(r["reason"]) > 0


def test_place_unanchored_blocked():
    node = {"id": "p1", "kind": "place", "label": "某地", "props": {}}
    sk = _skill(params={"location_id": {"type": "string"}})
    r = ct.autofill_target(node=node, skill=sk)
    assert r["status"] == ct.BLOCKED_UNANCHORED


# ======================================================================
# 工具箱灰显（fillable_skills）
# ======================================================================
def test_fillable_skills_marks_runnable():
    skills = [_skill(sid="a"), _skill(sid="b", params={"radius_m": {"type": "integer"}})]
    rows = ct.fillable_skills(_subj(pk="person_aaa"), skills)
    assert [r["runnable"] for r in rows] == [True, True]
    assert rows[0]["params"]["target_subject"] == "person_aaa"


def test_fillable_skills_blocks_ambiguous_everywhere():
    skills = [_skill(sid="a"), _skill(sid="c", params={"person": {"type": "string"}})]
    rows = ct.fillable_skills(
        _subj(pk=None, ambiguous=True, cands=["p1", "p2"]), skills)
    assert all(r["runnable"] is False for r in rows)
    assert all(r["status"] == ct.BLOCKED_AMBIGUOUS for r in rows)


def test_fillable_skills_no_selection():
    rows = ct.fillable_skills(None, [_skill()])
    assert rows[0]["runnable"] is False
    assert rows[0]["status"] == "no_selection"


# ======================================================================
# 参数识别口径
# ======================================================================
def test_target_params_ignores_non_string():
    sk = _skill(params={"target_subject": {"type": "string"},
                        "radius_m": {"type": "integer"},
                        "subject": {"type": "number"}})
    subj, place = ct.target_params(sk)
    assert subj == ["target_subject"]      # number 类型不当主体靶心
    assert place == []


def test_target_params_stable_order():
    sk = _skill(params={"subject_b": {"type": "string"},
                        "subject_a": {"type": "string"}})
    subj, _ = ct.target_params(sk)
    assert subj == ["subject_b", "subject_a"]    # 跟随声明顺序，多次调用一致
    assert ct.target_params(sk)[0] == subj
