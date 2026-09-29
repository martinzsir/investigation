"""节点窗口（WIN）读侧装配。

GET /cases/{cid}/canvas/window?node_id=...

为什么只有"证据窗口"需要新端点
------------------------------
四个窗口里三个能直接用画布内存数据：
  - 关系窗口：画布内已有的边（谁连谁、什么关系）
  - 地图窗口：节点 props 自带的经纬度与精度档
  - 时间窗口：事件节点自带的时刻
**只有 analysis_result（证据窗口）缺数据**：它只知道自己属于哪些维度
（dims），不知道"凭哪几条观察成立"。所以要回查观察档案。

两条红线
--------
1. 每条支撑观察带**自己的**精度档，不继承节点精度。
   继承会让 4 条 minute 档和 12 条 date 档在窗口里长得一样——
   "次数会骗人"从后门钻回来（关系窗口排序踩过一次，见 canvas-window.ts）。

2. 匹配不上时**写明原因**，不返回空列表了事。空列表会被正兵读成
   "确实没有支撑"，而实际可能是"没记 lens_id 所以定位不到"——
   两者处置方式完全不同。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from server.app.clues_artifact import (load_case_observations,
                                       load_directed_observations)

_VALID_DIMS = ("space", "time", "relation")


# ----------------------------------------------------------------------
# 观察装载（与 convergence_view 同口径：批量 + 定向都要）
# ----------------------------------------------------------------------
def _load_obs(case_dir: str | Path, version: int) -> list[dict]:
    """定向深挖的观察也要进来，否则画布上跑出的东西查不到——那才是最该看的。"""
    # 先规范化再去重：批量返回 Observation 对象、定向也可能给 dict，
    # 若在转换前按属性取 id，dict 分支会直接 AttributeError。
    raw = list(load_case_observations(case_dir, version))
    raw += list(load_directed_observations(case_dir))
    out: list[dict] = []
    seen: set[str] = set()
    for o in raw:
        d = o.to_dict() if hasattr(o, "to_dict") else dict(o)
        oid = str(d.get("observation_id") or "")
        if oid and oid in seen:
            continue
        seen.add(oid)
        out.append(d)
    return out


# 维度与精度派生已上提到领域层（server.app.canvas_case），此处导入以保持
# 模块级名字不变（既有调用方与单测无感）。CAN-19 建节点要用同一份口径：
# 若这里留一份副本，就会出现"证据窗口里 minute 档、挂到画布上 date 档"。
from server.app.canvas_case import (better_precision,  # noqa: F401
                                    dim_of, obs_precision)


# ----------------------------------------------------------------------
# 匹配：这条结论凭哪些观察成立
# ----------------------------------------------------------------------
def _match_supports(node: dict, node_id: str, obs: list[dict]) -> tuple[list[dict], dict]:
    """返回 (命中的观察, 匹配说明)。

    优先 **精确** 匹配，逐级放宽，但每一级都记录 mode 与 reason：
      0. observation_ids 直指——growth 重建层节点按 (靶心×镜头) 聚合，
         props 自带该组观察编号列表，是最精确的来源；且观察顺序要按
         列表保留（不能被档案顺序重排）
      1. observation_id 直指——逐条回写节点（canvas_writeback）自带单值
      2. lens_id + 靶心收窄——同镜头但限定 origin.node_id 命中节点自身
         或 props.target_node_id（growth 节点 id 是聚合 id，观察里记的
         是靶心主体 id），避免把批量扫描/别的靶心的同镜头观察混进来
      3. lens_id 全案兜底——节点没记靶心/观察编号时的最后手段，reason
         如实标注"可能含批量扫描观察"
    都不中 → 返回空 + 原因，**不退化成"按维度全捞"**：
    那样会把本案所有 space 观察都算成这条结论的支撑。
    """
    props = node.get("props") or {}
    lens_id = str(props.get("lens_id") or "").strip()
    obs_id = str(props.get("observation_id") or "").strip()
    target_id = str(props.get("target_node_id") or "").strip()

    # 0) growth 聚合节点：props.observation_ids 列表精确命中（保留列表顺序）
    id_list = props.get("observation_ids")
    if isinstance(id_list, list) and id_list:
        want = [str(x or "").strip() for x in id_list if str(x or "").strip()]
        by_oid = {str(o.get("observation_id") or ""): o for o in obs}
        hits = [by_oid[x] for x in want if x in by_oid]
        if hits:
            return hits, {
                "mode": "observation_ids",
                "reason": f"按结论自带的 {len(hits)} 条观察编号精确定位",
            }

    # 1) 单条回写节点：observation_id 直指
    hits = [o for o in obs if obs_id and str(o.get("observation_id") or "") == obs_id]
    if hits:
        return hits, {"mode": "observation_id", "reason": "节点自带观察编号"}

    # 2) 镜头 + 靶心收窄：节点自身 id 或 growth 节点 props.target_node_id
    focal_ids = {x for x in (node_id, target_id) if x}
    if lens_id and focal_ids:
        hits = [
            o for o in obs
            if str(o.get("skill_id") or "") == lens_id
            and str((o.get("origin") or {}).get("node_id") or "") in focal_ids
        ]
        if hits:
            return hits, {
                "mode": "lens_and_target",
                "reason": f"按产出镜头 {lens_id} 与发起靶心定位",
            }

    # 3) 兜底：只按镜头全案匹配（可能混入批量扫描观察，如实标注）
    hits = [o for o in obs if lens_id and str(o.get("skill_id") or "") == lens_id]
    if hits:
        return hits, {
            "mode": "lens_id",
            "reason": (f"按产出镜头 {lens_id} 定位（节点未带观察编号/靶心，"
                       "可能含批量扫描观察）"),
        }

    # 定向 origin.node_id 兜底：没记 lens_id 但能从节点发起记录定位
    hits = [o for o in obs
            if node_id and str((o.get("origin") or {}).get("node_id") or "") == node_id]
    if hits:
        return hits, {"mode": "directed", "reason": "按该节点发起的定向深挖定位"}

    return [], {
        "mode": "none",
        "reason": ("该结论未记录 lens_id / 定向来源 / 观察编号，无法定位支撑观察；"
                   "此处不是'没有支撑'，而是'无从定位'"),
    }


def assemble_window(*, case_dir: str | Path, version: int,
                    node: dict, node_id: str = "",
                    support_limit: int = 20,
                    fact_limit: int = 5) -> dict:
    """按节点 kind 装配窗口数据。

    非 analysis_result 直接返回 available + 提示"用画布内存数据"，
    不假装需要后端——那会让前端无谓等待一个必然为空的请求。
    """
    kind = str(node.get("kind") or "")
    nid = node_id or str(node.get("id") or "")
    if kind != "analysis_result":
        return {
            "available": True,
            "node_id": nid,
            "kind": kind,
            "server_sourced": False,
            "supports": [],
            "by_dim": {},
            "note": "该类型窗口使用画布内存数据（边/坐标/时刻），无需后端查询",
        }

    obs = _load_obs(case_dir, version)
    hits, match = _match_supports(node, nid, obs)

    rows: list[dict] = []
    for o in hits:
        d = dim_of(o.get("skill_id"))
        rows.append({
            "obs_id": str(o.get("observation_id") or ""),
            "label": str(o.get("title") or o.get("lens_name") or o.get("skill_id") or ""),
            "precision": obs_precision(o),
            "dim": d or "unknown",
            "skill_id": str(o.get("skill_id") or ""),
            "lens_name": str(o.get("lens_name") or ""),
            "basis": str(o.get("basis") or ""),
            "falsification": str(o.get("falsification") or ""),
            "degraded": bool(o.get("degraded")),
            "degraded_reason": str(o.get("degraded_reason") or ""),
            "facts": (o.get("facts") or [])[:fact_limit],
            "facts_total": len(o.get("facts") or []),
        })

    # 排序：精度强者在前（同精度按观察编号稳定），
    # **绝不按事实条数排**——条数多只说明采样多，不代表更可信。
    rows.sort(key=lambda r: (_rank(r["precision"]), r["obs_id"]))

    shown = rows[:support_limit]
    by_dim: dict[str, list[dict]] = {d: [] for d in _VALID_DIMS}
    by_dim["unknown"] = []
    for r in shown:
        by_dim.setdefault(r["dim"], []).append(r)

    try:
        from core.convergence import PRECISION_WEIGHT
        weight_model = dict(PRECISION_WEIGHT)
    except Exception:
        weight_model = {}

    return {
        "available": True,
        "node_id": nid,
        "kind": kind,
        "server_sourced": True,
        "supports": shown,
        "by_dim": {k: v for k, v in by_dim.items() if v},
        "match": match,
        "total": len(rows),
        "shown": len(shown),
        "truncated": len(rows) > len(shown),
        "weight_model": weight_model,
    }


def _rank(precision: str) -> int:
    """排序用：越精确越靠前。与 PRECISION_WEIGHT 同向，不另立标准。"""
    order = {"second": 0, "minute": 0, "hour": 1, "date": 2, "unknown": 3}
    return order.get(str(precision or "unknown"), 3)
