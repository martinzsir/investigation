"""研判主体：名录（CAN-13）+ 重名不自裁红线（CAN-10）。

每条用例独立可测；关键红线配反向验证——变异体必须让断言失败。
真实语义层用例直接连 investigation.duckdb，不造假数据。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.app import canvas_subjects_view as sv  # noqa: E402
from server.app.canvas_case import (  # noqa: E402
    build_subject_node,
    identity_conflict,
    org_pk_candidates,
    pk_candidates_for,
)

DB = os.path.join(ROOT, "investigation.duckdb")


def _real_conn():
    if not os.path.exists(DB):
        pytest.skip("无语义层 duckdb")
    try:
        import duckdb
    except Exception:
        pytest.skip("无 duckdb")
    return duckdb.connect(DB, read_only=True)


# ----------------------------------------------------------------------
# 一、重名不自裁（CAN-10）——真实库上的红线
# ----------------------------------------------------------------------
def test_homonym_pending_not_anchored():
    """张卫国在库里有两个身份证号 → 不得判成"已锚定唯一主键"。

    修复前：pk_candidates 只查 obj_person（分列未落库 → 1 行），
    于是 ambiguous=False / anchored=True，系统静默挑了一个人。
    """
    c = _real_conn()
    p = build_subject_node(case_id="C1", name="张卫国", conn=c)["props"]
    assert p["person_pk_ambiguous"] is True
    assert p["pk_status"] == "homonym_pending"
    assert p["anchored"] is False, "同名异人待分列不得算已锚定"
    assert "分列尚未落库" in p["pk_resolution"]


def test_homonym_blocked_variation():
    """反向验证：若 identity_conflict 恒返回无冲突，上一条必然失效。"""
    c = _real_conn()
    cf = identity_conflict(c, "张卫国")
    assert cf["conflict"] is True
    assert cf["row_count"] == 2
    # 变异：把 conflict 改 False → 下面这条不成立
    assert cf["conflict"] is not False


def test_identity_conflict_no_plaintext():
    """只给性质，不给号码：id_card/phone 属敏感数据元。"""
    c = _real_conn()
    cf = identity_conflict(c, "张卫国")
    text = str(cf)
    for secret in ("330100197501011234", "330100198203045678",
                   "13800000001", "13900000002"):
        assert secret not in text, f"泄露敏感明文：{secret}"
    assert "身份证号互斥" in cf["evidence"]


def test_unique_person_still_anchored():
    """李志强只有一个主键 → 正常锚定，不得因修红线而误伤。"""
    c = _real_conn()
    p = build_subject_node(case_id="C1", name="李志强", conn=c)["props"]
    assert p["pk_status"] == "unique"
    assert p["anchored"] is True
    assert p["person_pk_ambiguous"] is False


def test_unknown_person_not_forged():
    """查无此人 → pk 为 None，绝不按名派生键假装锚定。"""
    c = _real_conn()
    p = build_subject_node(case_id="C1", name="查无此人", conn=c)["props"]
    assert p["person_pk"] is None
    assert p["pk_status"] == "none"
    assert p["anchored"] is False


# ----------------------------------------------------------------------
# 二、组织主体（走 obj_org，不再被判成"未收录"）
# ----------------------------------------------------------------------
def test_org_pk_candidates():
    c = _real_conn()
    pks = org_pk_candidates(c, "宏业建设")
    assert len(pks) == 1 and pks[0].startswith("org_")


def test_org_subject_usable():
    """修复前：组织走 person 表 → 全查不到 → usable=False。"""
    c = _real_conn()
    p = build_subject_node(case_id="C1", name="宏业建设",
                           sub_type="organization", conn=c)["props"]
    assert p["pk_status"] == "unique"
    assert p["anchored"] is True
    assert "未收录" not in p["pk_resolution"]


def test_pk_candidates_for_dispatch():
    c = _real_conn()
    assert pk_candidates_for(c, "宏业建设", "organization")[0].startswith("org_")
    assert pk_candidates_for(c, "李志强", "person")[0].startswith("person_")


# ----------------------------------------------------------------------
# 三、名录（CAN-13）
# ----------------------------------------------------------------------
def test_subjects_list_real():
    c = _real_conn()
    r = sv.list_subjects(conn=c, case_id="C1")
    assert r["semantic_ready"] is True
    names = {s["name"] for s in r["subjects"]}
    assert "张卫国" in names and "李志强" in names
    assert "宏业建设" in names, "组织必须出现在名录里"


def test_subjects_same_口径_as_canvas():
    """名录判定与画布节点同口径：不得名录说可用、建上去变待裁决。"""
    c = _real_conn()
    for s in sv.list_subjects(conn=c, case_id="C1")["subjects"]:
        p = build_subject_node(case_id="C1", name=s["name"],
                               sub_type=s["sub_type"], conn=c)["props"]
        assert s["pk_status"] == p["pk_status"]
        assert s["person_pk_ambiguous"] == p["person_pk_ambiguous"]


def test_subjects_unavailable_says_why():
    """语义层不可达 → 明说，不返回空数组假装"本案没有主体"。"""
    r = sv.list_subjects(conn=None)
    assert r["semantic_ready"] is False
    assert r["subjects"] == []
    assert "无法" in r["reason"] and len(r["reason"]) > 10


def test_subjects_relational_marked():
    """关系型指代（张卫国配偶）标出来，不静默从名录消失。"""
    c = _real_conn()
    d = {s["name"]: s for s in sv.list_subjects(conn=c, case_id="C1")["subjects"]}
    rel = d.get("张卫国配偶")
    assert rel is not None, "关系型指代不得静默过滤"
    assert rel["relational"] is True
    assert rel["usable"] is False


def test_subjects_query_filter():
    c = _real_conn()
    names = [s["name"] for s in sv.list_subjects(conn=c, q="张")["subjects"]]
    assert names and all("张" in n for n in names)


def test_subjects_usable_first():
    """可用主体排前面；张卫国（待裁决）不得排在李志强之前。"""
    c = _real_conn()
    subs = sv.list_subjects(conn=c, case_id="C1")["subjects"]
    flags = [bool(s["usable"]) for s in subs]
    assert flags == sorted(flags, reverse=True)
