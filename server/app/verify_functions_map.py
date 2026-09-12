"""
server/app/verify_functions_map.py
REQ-V-016 核查方向 → 只读 Function 映射（P3，REQ-V-017 一键复跑的前置）。

ADR-V-6：playbook 是唯一数据源，不另维护关键词映射表——
  1. 主路径（建议项/ai_draft 项）：channel=function 且 ref_function 非空
     → 直接映射 ref_function（playbook loader 装载期已硬失败校验
     functions.json 声明，core.ontology_loader.load_verify_playbooks AC5）；
     fallback_function 按同名 function 反查 verify_playbooks.json
     （首个声明非空 fallback 的条目；未声明 = 空串）。
  2. 兜底路径（manual/auto 项，ref_function 为空，含结构化构造器
     "库内复跑"）：按维度关键词映射（FALLBACK_RULES 代码内常量；
     文本包含关键词组内全部关键词才算命中，表序即优先级）；
     无命中 → None（REQ-V-017 端点据此回 NO_REPLAY_MAPPING）。

fail-closed 纪律（对齐 ontology loader）：解析结果必须存在于案件包
functions.json——load_pack 是唯一权威面：SQL 声明即合法可执行
（FunctionRuntime.invoke 走 sql 分支），py 实现 impl_ref ∈ FUNCTION_IMPLS
由 loader 装载期校验（core/ontology_loader.py）。不在包内（快照换包/
函数下线/脏 ref_function）→ None，不带病映射；fallback_function 失效
→ 降级空串，不废主映射。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.functions import FUNCTION_IMPLS
from core.ontology_loader import load_pack, load_verify_playbooks


@dataclass(frozen=True)
class ReplayMapping:
    """一条可复跑映射。

    function 为主跑 Function；fallback_function 为主跑失败时的库内备选
    （REQ-V-017「库内先试一把」）；source 留痕映射来源（playbook=手册
    声明直取 / fallback=关键词兜底）。
    """
    function: str
    fallback_function: str = ""
    source: str = ""


# 维度关键词兜底表（代码内常量）：只兜 playbook 覆盖不到的 manual 方向，
# 不与 playbook 重复维护业务映射。每条 = (关键词组, function)；
# 装载期校验由 tests/test_verify_replay.py 锁定：function 必须存在于
# default 包 functions.json（py 实现须在 FUNCTION_IMPLS 注册），
# 缺失 = 测试硬失败（对齐 ontology loader 纪律）。
FALLBACK_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("通讯", "频次"), "call_frequency_spike"),
    (("通话", "频次"), "call_frequency_spike"),
    (("轨迹", "同框"), "co_located_pairs"),
)


def _fallback_by_keywords(text: str) -> str:
    """维度关键词兜底：全部关键词命中才算命中；先声明者优先。"""
    for keywords, fn in FALLBACK_RULES:
        if all(k in text for k in keywords):
            return fn
    return ""


def _playbook_fallback(function: str, *, pack_id: str, base_dir) -> str:
    """反查手册：同名 function 条目里首个声明非空 fallback_function 者。"""
    for pb in load_verify_playbooks(pack_id, base_dir):
        if (pb["channel"] == "function" and pb["function"] == function
                and pb["fallback_function"]):
            return pb["fallback_function"]
    return ""


def _runnable(name: str, specs: dict) -> bool:
    """Function 在案件包内可执行：声明存在，且 py 实现已注册
    （loader 已保证，此处防御注册面被绕过）。"""
    if name not in specs:
        return False
    spec = specs[name]
    return not (spec.impl == "py" and name not in FUNCTION_IMPLS)


def resolve_replay_mapping(item: dict, *, pack_id: str = "default",
                           base_dir=None) -> ReplayMapping | None:
    """核查项 → 可复跑映射；None = 无映射（调用方回 NO_REPLAY_MAPPING）。

    item 取 StateStore.list_verify_items 行形状（至少含
    channel/ref_function/text）；suggested/ai_draft/manual 一律同入口，
    由字段形状路由（playbook 主路径优先）。
    """
    channel = str(item.get("channel") or "")
    ref = str(item.get("ref_function") or "").strip()
    text = str(item.get("text") or "")

    if channel == "function" and ref:
        fn = ref
        fb = _playbook_fallback(ref, pack_id=pack_id, base_dir=base_dir)
        source = "playbook"
    else:
        fn = _fallback_by_keywords(text)
        if not fn:
            return None
        fb, source = "", "fallback"

    specs = load_pack(pack_id, base_dir=base_dir).functions
    if not _runnable(fn, specs):
        return None
    if fb and not _runnable(fb, specs):
        fb = ""  # 备选失效降级空串，主映射不受影响
    return ReplayMapping(function=fn, fallback_function=fb, source=source)
