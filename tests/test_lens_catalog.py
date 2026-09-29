"""镜头元信息反查（core/lens_catalog）——逐条可独立验证。

背景：CAN-19 冒烟时发现结论节点标签是机器编号、假设长不出来，根因是
元信息靠调用方记得传。本文件验证"服务端自己反查"这条路径。
"""
from __future__ import annotations

from core import lens_catalog as C


# ----------------------------------------------------------------------
# 目录装载
# ----------------------------------------------------------------------
def test_catalog_covers_all_registered_lenses():
    """22 个已注册镜头全部在册（含 CAN-17 新建的 fund/comm 两包）。"""
    cat = C.load_catalog()
    assert len(cat) >= 22, f"镜头数偏少：{len(cat)}"
    for sid in ("geo_anomaly", "geo_site_profile", "fund_overpass_two_hop",
                "fund_integer_transfer", "comm_call_frequency",
                "timeline_cross_collision", "relation_org_interest"):
        assert sid in cat, f"{sid} 未注册"


def test_single_bad_pack_does_not_kill_catalog():
    """一个包解析失败只跳过该包，不让整册失效（故障不得放大）。"""
    cat = C.load_catalog(base_dir="/nonexistent/ontology")
    assert isinstance(cat, dict)


# ----------------------------------------------------------------------
# R-1：查不到名字不拿 id 冒充
# ----------------------------------------------------------------------
def test_lens_name_is_chinese_not_id():
    assert C.lens_name("geo_anomaly") == "异常轨迹镜头"
    assert C.lens_name("fund_overpass_two_hop") == "两跳过桥路径镜头"


def test_unregistered_lens_name_is_none():
    """未注册返回 None——让调用方退成通用名，而不是把 id 当名字显示。"""
    assert C.lens_name("some_unregistered_lens") is None
    assert C.lens_name("") is None


def test_vlm_has_no_functions_but_has_name():
    """vlm_inspect 未声明 uses_functions：名字要有，内核如实为空。"""
    assert C.lens_name("vlm_inspect") == "视觉图像分析镜头"
    assert C.lens_functions("vlm_inspect") == []


# ----------------------------------------------------------------------
# 维度：修 dim_of 只认 geo_/timeline_/relation_ 的漏判
# ----------------------------------------------------------------------
def test_fund_and_comm_dims_declared():
    """fund_/comm_ 镜头的维度由 produces_dims 声明，不靠前缀猜。"""
    assert "fund" in C.lens_dims("fund_integer_transfer")
    assert "comm" in C.lens_dims("comm_call_frequency")
    assert "space" in C.lens_dims("geo_anomaly")


def test_dims_empty_for_unregistered():
    assert C.lens_dims("nope") == []


# ----------------------------------------------------------------------
# 归属：镜头 → 假设
# ----------------------------------------------------------------------
def test_overpass_lens_resolves_h4():
    hyps, why = C.resolve_lens_assumptions("fund_overpass_two_hop")
    assert hyps == ["H4"], f"{hyps} / {why}"


def test_call_frequency_lens_resolves_h3():
    """通话高频挂 H3（二人密切私下关系），不是 H2——此前挂错过。"""
    hyps, _ = C.resolve_lens_assumptions("comm_call_frequency")
    assert hyps == ["H3"]


def test_org_interest_lens_resolves_h2():
    hyps, _ = C.resolve_lens_assumptions("relation_org_interest")
    assert hyps == ["H2"]


def test_unresolvable_returns_empty_with_reason():
    """判不出就返回空列表 + 具体原因，绝不硬猜。"""
    for sid in ("geo_anomaly", "timeline_cross_collision", "nope"):
        hyps, why = C.resolve_lens_assumptions(sid)
        assert hyps == [], f"{sid} 不该硬猜出假设"
        assert why and len(why) > 5, f"{sid} 必须给出原因"


def test_multi_function_lens_lists_all_not_one(monkeypatch):
    """R-2：多内核归属不同假设时全部列出，不挑一个。

    真实包里目前都是单内核，故用 monkeypatch 构造——这条守的是"将来某个
    镜头声明两个内核时不得静默取第一个"。
    """
    real = C.load_catalog()

    def fake(base_dir=None):
        cat = dict(real)
        cat["multi_fn_lens"] = {
            "lens_id": "multi_fn_lens", "name": "多内核镜头",
            "pack_id": "test",
            "functions": ["integer_transfer_aggregates", "call_frequency_spike"],
            "dims": ["fund", "comm"], "canvas_enabled": True,
        }
        return cat

    monkeypatch.setattr(C, "load_catalog", fake)
    hyps, why = C.resolve_lens_assumptions("multi_fn_lens")
    assert set(hyps) == {"H4", "H3"}, f"应列出两个归属，实得 {hyps}"
    assert "全部列出" in why
