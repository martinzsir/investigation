"""
CAN-18：Function → 假设归属（core/lens_assumption.py）。

背景（核查实证，非推测）：此前 8 个裸 Function 包装 Lens 时假设是**手写**的，
8 个里挂错 4 个。本文件锁死"假设必须来自本体反查"这条纪律。

守三条红线：
  1. 假设不手写 —— 从 hypothesis_patterns.json 反查，抄错不可能发生
  2. 判不出来的不硬猜 —— 返回 None + 原因
  3. 反向验证 —— 变异本体映射后断言必须失败（证明不是恒真摆设）
"""
import json
from pathlib import Path

import pytest

from core.lens_assumption import (
    FUNCTION_RULE,
    FUNCTION_ASSUMPTION_PENDING,
    resolve_function_assumption,
    rule_to_hypothesis,
    validate_all,
    known_functions,
)

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ROOT / "ontology" / "default" / "hypothesis_patterns.json"


def _load():
    return json.loads(PATTERNS.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 红线 1：假设来自本体反查，不是手写
# ----------------------------------------------------------------------
def test_no_hardcoded_hypothesis_ids():
    """源码里不得出现手写的 H1..H6 假设 id 赋值。

    这是 CAN-18 的根治点：一旦允许手写，就一定会再次抄错。
    """
    src = (ROOT / "core" / "lens_assumption.py").read_text(encoding="utf-8")
    # 允许出现在注释与文档字符串里（说明用），但不得作为映射值
    import re
    for m in re.finditer(r'^\s*"(\w+)":\s*"(H\d)"', src, re.M):
        pytest.fail(f"发现手写假设映射 {m.group(1)} -> {m.group(2)}；假设须从本体反查")


def test_rule_to_hypothesis_reads_ontology():
    """反查结果必须等于本体 hypothesis_patterns.json 的声明。"""
    pats = _load()
    expect = {}
    for p in pats["patterns"]:
        for rid in p.get("rule_ids", []):
            expect[rid] = p["hypothesis"]["id"]
    for rid, hid in expect.items():
        got = rule_to_hypothesis(rid)
        assert got is not None and got["id"] == hid, f"{rid} 反查错误"


# ----------------------------------------------------------------------
# 具体归属：这是此前挂错的那 4 个，逐条锁死
# ----------------------------------------------------------------------
@pytest.mark.parametrize("fn,expect", [
    # description 明写"规则 R1 挂钩" → R1 → H1
    ("quarter_end_integer_deposits", "H1"),
    # description 明写"规则 R2 挂钩"+"过桥结构" → R2 → H4（此前误挂 H1）
    ("integer_transfer_aggregates", "H4"),
    # 两跳过桥 → R2 → H4（此前误挂 H1）
    ("overpass_two_hop", "H4"),
    # 通话高频 → R3 → H3（此前误挂 H2）
    ("call_frequency_spike", "H3"),
    # 异主体时空同框 → R-GEO-3 → H6
    ("co_located_pairs", "H6"),
    # 工商利益关联 → R5 → H2
    ("org_interest_links", "H2"),
])
def test_function_assumption_matches_ontology(fn, expect):
    hid, why = resolve_function_assumption(fn)
    assert hid == expect, f"{fn} 应为 {expect}，实际 {hid}（{why}）"
    assert "反查" in why, "必须来自本体反查，不得手写"


def test_previously_wrong_mappings_are_fixed():
    """反向验证：把此前挂错的 4 个逐个断言，防止回退。"""
    wrong_before = {
        "integer_transfer_aggregates": "H1",
        "overpass_two_hop": "H1",
        "call_frequency_spike": "H2",
    }
    for fn, bad in wrong_before.items():
        hid, _ = resolve_function_assumption(fn)
        assert hid != bad, f"{fn} 又挂回了错误的 {bad}"


# ----------------------------------------------------------------------
# 红线 2：判不出来的不硬猜
# ----------------------------------------------------------------------
@pytest.mark.parametrize("fn", ["jian_cross_level"])
def test_pending_returns_none_with_reason(fn):
    """业务歧义项返回 None + 原因，绝不挑一个。"""
    hid, why = resolve_function_assumption(fn)
    assert hid is None, f"{fn} 归属有歧义，不得硬猜"
    assert why and len(why) > 10, "必须给出具体歧义原因，不能只说'待定'"


def test_time_window_collision_follows_ontology_not_intuition():
    """R6 → H4：按本体反查，不按"整数资金偏 H1"的业务直觉。

    这条曾经被列为"待确认"，理由是凭直觉觉得有歧义。核查 hypothesis_patterns
    后确认本体已显式声明 R6 → H4，凭直觉覆盖本体正是本模块要根治的错误，
    故改回反查，并在此固化防回退。
    """
    from core.lens_assumption import FUNCTION_RULE
    assert FUNCTION_RULE.get("time_window_collision") == "R6"
    hid, why = resolve_function_assumption("time_window_collision")
    assert hid == "H4", f"应经 R6 反查到 H4，实得 {hid}（{why}）"
    assert "R6" in why


def test_unknown_function_returns_none():
    """未声明的 Function 返回 None，不静默给默认值。"""
    hid, why = resolve_function_assumption("some_unknown_fn")
    assert hid is None
    assert "未声明" in why


# ----------------------------------------------------------------------
# 红线 3：反向验证——变异本体后断言必须失败
# ----------------------------------------------------------------------
def test_mutation_caught(monkeypatch):
    """把 R2 的假设改成 H1，反查结果必须跟着变。

    证明 test_function_assumption_matches_ontology 不是恒真的摆设：
    若断言恒真，说明它根本没在校验本体。

    注意实现细节（此前的写法测了个寂寞）：``_load_patterns`` 每次都会重新
    读本体，所以在测试里改内存里的 pats 字典对反查**毫无影响**——那次失败
    恰恰反证了"假设始终来自本体"，但也说明必须走 monkeypatch 注入才叫变异。
    """
    import core.lens_assumption as LA

    pats = _load()
    orig = None
    for p in pats["patterns"]:
        if "R2" in p.get("rule_ids", []):
            orig = p["hypothesis"]["id"]
            p["hypothesis"]["id"] = "H1"   # 变异：把 R2 挂成 H1
            break
    assert orig == "H4", "前提：R2 原本映射 H4"

    monkeypatch.setattr(LA, "_load_patterns", lambda *a, **k: pats["patterns"])

    mutated = LA.rule_to_hypothesis("R2")
    assert mutated is not None and mutated["id"] == "H1", \
        "变异后反查应返回 H1（证明反查真的在读声明）"
    # 变异生效时，正确的归属断言必须失败——这才说明断言有约束力
    hid, _ = LA.resolve_function_assumption("integer_transfer_aggregates")
    assert hid == "H1", "变异生效：此时应反查出错误的 H1"
    assert hid != "H4", "变异后不应仍为 H4，否则说明压根没读声明"


# ----------------------------------------------------------------------
# 全量校验
# ----------------------------------------------------------------------
def test_validate_all_no_problems():
    """所有已声明规则都能在本体查到假设。"""
    assert validate_all() == []


def test_known_functions_covers_bare_functions():
    """8 个裸 Function 必须全部被本表覆盖（修一类漏一类的守护）。

    上次按白名单数只有 6 个，漏了 org_interest_links 与 jian_cross_level
    两个完全不可达的；此测试防再次漏。
    """
    fns = json.loads(
        (ROOT / "ontology" / "default" / "functions.json").read_text(encoding="utf-8"))
    bare = [
        "quarter_end_integer_deposits", "integer_transfer_aggregates",
        "time_window_collision", "overpass_two_hop", "call_frequency_spike",
        "co_located_pairs", "org_interest_links", "jian_cross_level",
    ]
    covered = known_functions()
    missing = [b for b in bare if b not in covered]
    assert not missing, f"裸 Function 未被归属表覆盖：{missing}"
    # 且这些 Function 确实存在
    names = {f.get("name") for f in fns.get("functions", [])}
    for b in bare:
        assert b in names, f"{b} 不在 functions.json"
