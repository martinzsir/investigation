"""物品持有链装配（应用层）。

为什么要有这一层
----------------
``core/hold_chain.py`` 提供的是**纯算法**：给一组持有记录，产出时序判定
（错序 / 重叠 / 不可判定）与前端可消费的链形态。它不知道"记录从哪来"。

本文件负责第二件事：把**画布上的边**变成那组记录。

两者分开的理由与 ``core/lens_origin.py`` 同源——规则是纯函数，放在应用层
就只能靠 HTTP 端到端测，而端到端依赖整条装配链，**那条最该守住的规则
反而测不到**。这里保持纯函数，可脱离服务单测。

数据源现状（务必知情）
--------------------
``holds`` 关系类型已注册进本体（person→item，带 start_date/end_date），
但**当前案件数据集没有任何物品数据**（轨迹/流水/招投标/工商四表均无车牌、
权证号、发票号等字段）。所以真实画布上暂时产不出持有边，本层会优雅返回
空链并给出原因——**不是"查无此事"，是"尚无该数据源"**，两者不能混淆。
"""

from __future__ import annotations

from typing import Any

from core.hold_chain import build_hold_chain

# 与 canvas_case.HOLD_REL 同值；此处不 import 是为了让本模块能脱离
# server.app 单测（canvas_case 依赖较重）。
HOLD_REL = "持有"


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def holding_records_from_edges(
    *,
    item_node_id: str,
    edges: list[dict] | None,
    label_by_id: dict[str, str] | None = None,
) -> list[dict]:
    """从画布边里抽出指向某物品节点的持有记录。

    方向：人是 source、物品是 target（本体 holds 即 person→item）。
    反向边（物品→人）**不认**，不用"两边都试"的方式容错——方向错了说明
    建模错了，静默纠正会让错误一直藏着。
    """
    nid = _text(item_node_id)
    if not nid:
        return []
    labels = label_by_id or {}

    out: list[dict] = []
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        if _text(e.get("rel")) != HOLD_REL:
            continue
        if _text(e.get("target")) != nid:
            continue

        src = _text(e.get("source"))
        # 边属性键统一：装配层写 props，data 是早期形态（保留兜底，不删——
        # 删了会让既有人工边读不到）。props 优先，避免两处各写一份后分叉。
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        props = e.get("props") if isinstance(e.get("props"), dict) else {}
        pm = {**data, **props}
        holder = _text(pm.get("holder")) or labels.get(src) or src
        out.append({
            "id": _text(e.get("id")) or f"{holder}#{len(out)}",
            "holder": holder,
            "start": _text(pm.get("start") or pm.get("start_date")),
            "end": _text(pm.get("end") or pm.get("end_date")),
            "order": pm.get("order"),
        })
    return out


def build_item_hold_chain(
    *,
    item_ref: str = "",
    item_node_id: str = "",
    edges: list[dict] | None = None,
    label_by_id: dict[str, str] | None = None,
) -> dict:
    """装配某物品节点的持有链（前端 buildHoldingWindow 可直接消费）。"""
    records = holding_records_from_edges(
        item_node_id=item_node_id,
        edges=edges,
        label_by_id=label_by_id,
    )
    chain = build_hold_chain(records, item_ref=item_ref)
    chain["data_source"] = "canvas_edges"
    if not records:
        # 空链必须给原因，且原因要说清**是哪种空**。
        # 静默空列表会被正兵读成"这东西没人持有过"，而实际是"画布上还没有
        # 持有边"——两者后续动作完全不同：前者是结论，后者是要补数据源。
        chain["reason"] = "画布上暂无持有边（holds 数据源可能尚未接入，非『查无持有』）"
    return chain
