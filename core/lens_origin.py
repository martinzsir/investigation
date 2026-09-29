"""定向镜头发起来源归一化（画布回挂的入口契约）。

为什么单独成模块
----------------
`server/app/routers/lenses.py` 依赖 fastapi，而这条归一化是**纯函数**、
必须在没有 web 依赖的环境里可测（沙盒常装不全 fastapi）。放在路由里就
只能靠 HTTP 端到端测，而端到端又依赖整条装配链——于是这条最该被守住
的规则反而测不到。

业务契约
--------
镜头跑完要把结果**挂回发起它的画布节点**下（CAN-19）。发起来源有两种：

  · 线索级画布：origin.clue_id 必填（结果回挂到该线索的画布）
  · 案件级画布：没有发起线索，靶心就是画布节点 id（origin.node_id）

两条红线
--------
R-1 案件级缺 clue_id **不许把 origin 整个丢弃**。
    丢弃后观察不记 node_id，重建层按 no_target 排除，后果是：镜头跑成功
    了、档案里也有产物，但**图上什么都不出现**。正兵只会当成"没查到"，
    而不会知道是发起来源被丢了——比报错更坏，报错至少还有痕迹。

R-2 观察按 observation_id upsert，而 id 指纹里的发起上下文此前**只取
    clue_id**。案件级画布下，对张卫国跑异常轨迹与对李志强跑异常轨迹会
    算出同一个 id，后一次直接覆盖前一次——换个人再跑一次，上个人在图
    上的结论就消失了。所以 node_id 必须同样参与指纹（见
    core/observation.py:observation_from_clue）。

R-3 不落任意结构：只保留已知键，且一律转字符串。origin 来自请求体，
    放任透传等于给存储层开一个任意结构的口子。
"""
from __future__ import annotations

from typing import Any

#: 允许落盘的键（其余一律丢弃，不静默透传）
KNOWN_KEYS = ("clue_id", "node_id", "subject", "surface", "canvas")

#: 案件级画布发起标记（与线索级区分，供读面判断回挂目标）
CASE_CANVAS = "case"


def normalize_origin(raw: Any) -> dict[str, Any] | None:
    """归一化发起来源；无有效上下文返回 None（表示"无发起画布"）。

    返回 None 的**唯一**合法情形是：调用方压根没传、或传了但没有可识别
    的靶心（既无 clue_id 也无 node_id）——那时确实无从回挂，行为同旧版。
    """
    if not isinstance(raw, dict):
        return None

    cid = str(raw.get("clue_id") or "").strip()
    nid = str(raw.get("node_id") or "").strip()

    if cid:
        origin: dict[str, Any] = {"clue_id": cid}
        for k in ("node_id", "subject", "surface"):
            v = raw.get(k)
            if v not in (None, ""):
                origin[k] = str(v)
        return origin

    if nid:
        # 案件级研判画布：无发起线索，靶心即画布节点 id。
        # 记一笔 canvas=case，读面据此知道这不是"漏了 clue_id"。
        origin = {"canvas": CASE_CANVAS, "node_id": nid}
        for k in ("subject", "surface"):
            v = raw.get(k)
            if v not in (None, ""):
                origin[k] = str(v)
        return origin

    return None


def origin_context_key(origin: Any) -> str:
    """发起上下文指纹键：clue_id 优先，回落到 node_id。

    供 core/observation 生成稳定 observation_id 用。两者都取不到返回空串
    （调用方据此不加指纹，与批量扫描同 id——那时确实没有研判上下文）。
    """
    if not isinstance(origin, dict):
        return ""
    cid = str(origin.get("clue_id") or "").strip()
    if cid:
        return cid
    return str(origin.get("node_id") or "").strip()
