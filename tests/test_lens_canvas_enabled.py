"""镜头在画布内调度（CAN-16）。

不止计数——每条断言都绑一个业务后果：某个镜头开不了，推演里哪一步会断。
"""
from __future__ import annotations

import glob
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALID_DIMS = {"fund", "comm", "behavior", "space", "relation", "time"}


def _all_skills():
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "packs", "*", "pack.json"))):
        pack = json.load(open(path, encoding="utf-8"))
        for s in pack.get("skills", []):
            s = dict(s)
            s["_pack"] = pack.get("pack_id")
            out.append(s)
    return out


def test_packs_loadable():
    skills = _all_skills()
    assert len(skills) >= 22


def test_deterministic_lenses_all_canvas_enabled():
    """CAN-16：确定性镜头必须能在画布内调度。

    反例后果：业务推演 E3「选中张卫国 → 落脚点画像」跑不了，图上长不出
    文三路/莫干山路/江陵路，整个"顺着图往下查"的路径在第一就断了。
    """
    off = [s.get("skill_id") for s in _all_skills()
           if s.get("mode") == "deterministic" and not s.get("canvas_enabled")]
    assert not off, f"确定性镜头未开画布调度：{off}"


def test_geo_site_profile_enabled():
    """E3 直接依赖：落脚点画像镜头。"""
    by_id = {s.get("skill_id"): s for s in _all_skills()}
    assert by_id["geo_site_profile"].get("canvas_enabled") is True


def test_relation_lenses_enabled():
    """E12 直接依赖：组织间接路径（张→某单位←李）。"""
    by_id = {s.get("skill_id"): s for s in _all_skills()}
    for sid in ("relation_org_interest", "relation_paths"):
        assert by_id[sid].get("canvas_enabled") is True, sid


def test_timeline_lenses_enabled():
    """E8 直接依赖：时间窗口里看"通话—见面—通话"节奏。"""
    by_id = {s.get("skill_id"): s for s in _all_skills()}
    for sid in ("timeline_sequence", "timeline_rhythm",
                "timeline_cross_collision"):
        assert by_id[sid].get("canvas_enabled") is True, sid


def test_draft_lens_stays_off_with_reason():
    """vlm 是 draft 且维度未声明，必须显式关闭并写明理由。

    不能"顺手全开"：图像维度不在六维声明内，开了会让图像证据不进覆盖度
    统计却仍占用画布入口，正兵会以为跑过了、有结论。
    """
    vlm = [s for s in _all_skills() if s.get("skill_id") == "vlm_inspect"]
    assert vlm, "未找到 vlm_inspect"
    s = vlm[0]
    assert s.get("canvas_enabled") is False
    assert s.get("mode") == "draft"
    assert not (set(s.get("produces_dims") or []) & VALID_DIMS)
    note = str(s.get("_canvas_enabled_note") or "")
    assert len(note) > 0, "关闭必须有可查理由，否则会被当成遗漏"


def test_enabled_lenses_dims_declared():
    """开着的镜头，其维度必须在六维内——否则产出不进覆盖度统计。"""
    bad = []
    for s in _all_skills():
        if not s.get("canvas_enabled"):
            continue
        dims = set(s.get("produces_dims") or [])
        if dims and not dims <= VALID_DIMS:
            bad.append((s.get("skill_id"), sorted(dims)))
    assert not bad, f"维度未声明却已开画布调度：{bad}"


def test_every_disabled_lens_has_reason():
    """任何未开的镜头都要写明为什么——避免"以为是遗漏"而误开。"""
    no_reason = [s.get("skill_id") for s in _all_skills()
                 if not s.get("canvas_enabled")
                 and not str(s.get("_canvas_enabled_note") or "").strip()]
    assert not no_reason, f"未开但无理由说明：{no_reason}"


def test_canvas_enabled_count():
    """计数快照：22 个镜头、21 开、1 有意不开（vlm）。"""
    skills = _all_skills()
    on = [s for s in skills if s.get("canvas_enabled")]
    assert len(skills) == 22
    assert len(on) == 21
