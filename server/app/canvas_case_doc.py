"""案件级画布：什么该落库、什么该每次重建（纯函数，可单测）。

业务起点
--------
案件级画布此前**没有服务端持久化**——人工加的主体、提升产生的节点、摆好
的坐标，全在前端画布文档里，**刷新就没了**。而刷新丢失对研判画布是致命的：
正兵花一小时把人、地、事、假设摆成一张图，关掉页面就归零，下次重摆。

但要落库就得先回答：**整个文档都存吗？**

分层结论
--------
不该存：**镜头重建层**。它由定向观察档案（artifacts/directed_observations）
每次 GET 重建。若连它一起存，重扫后库里那份就是过期的——档案变了、库里
没变，而且再也不会被覆盖，正兵看到的是一张**僵死的图**。这比刷新丢失更
隐蔽：图还在、数据旧、无人提示。

必须存：档案里没有、丢了回不来的东西——
  · 人工 hypothesis / note 节点
  · 提升产生的 subject 节点（人工发起，非镜头产出）
  · 人工边
  · **所有节点的坐标与钉住**（表达层数据，档案里根本没有）

判据不是 system 真假
--------------------
最自然的写法是"system=true 的节点剥离"。**这条在这里是错的**：CAN-08 提升
产生的 subject 节点也是 system=True（可删不可改），但它是人工动作的结果，
档案里没有，剥离后刷新就丢——正是本模块要治的病。

所以判据是**来源**：镜头重建层的产物统一打 ``generated_by = lens_layer``
（见 canvas_case.GENERATED_BY_LENS），落库前按它剥离。

红线
----
R-1 剥离只看 generated_by 标记，**不看 system 字段**。
R-2 剥离后必须剔除**悬空边**——引用了已剥离节点的边若不清理，G6 渲染会
    直接报"边引用了不存在的节点"，整张图白屏。这类边如实计入 dropped，
    不静默丢。
R-3 合并时**保留库里已有的坐标与钉住**：重扫不该把正兵摆好的图冲回默认
    位置。新出现的节点没有历史坐标，由调用方给默认落位。
R-4 合并以重建层为准覆盖同 id 节点（系统层永远最新），人工节点原样保留。
"""
from __future__ import annotations

from typing import Any

from server.app.canvas_case import GENERATED_BY_ITEM, GENERATED_BY_LENS

# 表达层字段：合并时从库里继承，不被重建层冲掉
LAYOUT_FIELDS = ("x", "y", "pinned", "collapsed")

# 重建层节点布局在 meta 中的键。重建节点本身不落库（split 会剥），但正兵摆
# 好的坐标/钉住是表达层数据、丢了回不来——剥节点前收割到这里，重建后应用
# 回同 id 节点。镜头层/物品层同机制，故只设一个键。
META_LENS_LAYOUT = "lens_layout"

# 渐进式揭示集在 meta 中的键。值为 [{"target": node_id, "lens": skill_id}]，
# 表示正兵已让哪些「靶心×镜头」结果组长到画布上。缺省=空集（打开只见人工
# 层），由前端「全部显示/逐组开关」维护；与坐标同属表达层，随画布持久。
META_REVEALED = "revealed_groups"


def parse_revealed_groups(meta: Any) -> set[tuple[str, str]]:
    """读取揭示集。缺省/脏值一律降级为空集（fail-closed：不多长）。"""
    if not isinstance(meta, dict):
        return set()
    raw = meta.get(META_REVEALED)
    if not isinstance(raw, list):
        return set()
    out: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        tgt = str(item.get("target") or "").strip()
        lens = str(item.get("lens") or "").strip()
        if tgt and lens:
            out.add((tgt, lens))
    return out


def serialize_revealed_groups(groups: set[tuple[str, str]]
                              ) -> list[dict[str, str]]:
    """揭示集落 meta；排序保证文档稳定（diff/审计可读）。"""
    return [{"target": t, "lens": l} for t, l in sorted(groups)]


# 重建层来源标记全集：镜头层 + 物品源层。
# 两者都"每次重建、不落库"——存进库就会僵死（源变了、库里不变，且再也不
# 会被覆盖）。分开两个值而非合并成一个，是为了让正兵看得出这批节点从哪来。
GENERATED_BY_VALUES = frozenset({GENERATED_BY_LENS, GENERATED_BY_ITEM})


def _is_lens_generated(item: Any) -> bool:
    """重建层产物判定（R-1：只看标记，不看 system）。"""
    if not isinstance(item, dict):
        return False
    props = item.get("props")
    if isinstance(props, dict) \
            and str(props.get("generated_by") or "") in GENERATED_BY_VALUES:
        return True
    return str(item.get("generated_by") or "") in GENERATED_BY_VALUES


def _has_own_mark(item: Any) -> bool:
    """节点自身（含 props）是否带重建层标记。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("generated_by") or ""):
        return True
    props = item.get("props")
    return isinstance(props, dict) and bool(str(props.get("generated_by") or ""))


def split_persistent(doc: dict | None) -> tuple[dict, dict[str, int]]:
    """整文档 → (可落库文档, 剥离统计)。

    返回统计而不是静默丢弃：调用方据此如实报"本次剥离了 N 个重建层节点"，
    剥离数为 0 却仍有镜头跑过，就说明标记没打上——那是缺陷，不该无声。
    """
    d = doc if isinstance(doc, dict) else {}
    nodes = [n for n in (d.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (d.get("edges") or []) if isinstance(e, dict)]

    keep_nodes = [n for n in nodes if not _is_lens_generated(n)]
    keep_ids = {str(n.get("id") or "") for n in keep_nodes}
    stripped_ids = {str(n.get("id") or "") for n in nodes} - keep_ids

    keep_edges: list[dict] = []
    dangling = 0
    for e in edges:
        if _is_lens_generated(e):
            continue
        s = str(e.get("source") or "")
        t = str(e.get("target") or "")
        if s in stripped_ids or t in stripped_ids:
            dangling += 1          # R-2：悬空边剔除并计数，不静默丢
            continue
        keep_edges.append(e)

    # 收割重建层节点布局：节点要剥，坐标/钉住不能跟着丢。与既有 meta 合并
    # （未揭示/暂不在文档里的节点布局也要留），当前文档中的值为准。
    meta = d.get("meta")
    meta = dict(meta) if isinstance(meta, dict) else {}
    saved_layout = meta.get(META_LENS_LAYOUT)
    layout_map: dict[str, dict] = (
        dict(saved_layout) if isinstance(saved_layout, dict) else {})
    harvested = 0
    for n in nodes:
        nid = str(n.get("id") or "")
        if nid not in stripped_ids:
            continue
        vals = {k: n.get(k) for k in LAYOUT_FIELDS if n.get(k) is not None}
        if vals:
            layout_map[nid] = vals
            harvested += 1
    meta[META_LENS_LAYOUT] = layout_map

    out = dict(d)
    out["meta"] = meta
    out["nodes"] = keep_nodes
    out["edges"] = keep_edges
    return out, {"nodes": len(stripped_ids), "edges": dangling,
                 "layout_harvested": harvested}


def merge_lens_layer(persistent: dict | None,
                     growth: dict | None,
                     stats_sink: dict[str, int] | None = None) -> dict:
    """(可落库文档) + (镜头重建层) → 供渲染的完整文档（R-3/R-4）。

    坐标继承：库里存过的坐标优先。重扫后新出现的节点没有历史坐标，由
    调用方落默认位——本函数不动它们（不知道该怎么摆）。重建层节点的坐标
    来自 split 时收割、存在 meta.lens_layout 里的布局（节点本身不落库）。

    stats_sink: 可选统计出口，写入 {"edges_dropped": N}——合并后端点仍缺失
    的重建边（如靶心主体未提升）在此剔除，不静默丢，也不让 G6 白屏。
    """
    base = persistent if isinstance(persistent, dict) else {}
    g = growth if isinstance(growth, dict) else {}

    layout: dict[str, dict[str, Any]] = {}
    # 1) 人工层节点自带的坐标
    for n in (base.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if not nid:
            continue
        saved = {k: n.get(k) for k in LAYOUT_FIELDS if n.get(k) is not None}
        if saved:
            layout[nid] = saved
    # 2) 重建层节点坐标：split 时收割进 meta，隐藏再显示/保存重进都不丢
    base_meta = base.get("meta")
    if isinstance(base_meta, dict):
        lens_layout = base_meta.get(META_LENS_LAYOUT)
        if isinstance(lens_layout, dict):
            for nid, vals in lens_layout.items():
                key = str(nid)
                if key in layout or not isinstance(vals, dict):
                    continue   # 人工层节点自带坐标优先，不被重建布局覆盖
                layout[key] = {k: v for k, v in vals.items()
                               if k in LAYOUT_FIELDS}

    # 人工层里已存在、且**没有**重建层标记的节点 id。
    # 为什么必须先记下来：重建层会为持有人产出 subject 节点（如"张卫国"），
    # 与正兵手加的主体同 id。若照 R-4 无脑覆盖，重建层的 generated_by 会
    # 盖到人工节点上，保存时该节点被当作重建层剥离——**正兵手加的主体刷新
    # 就没了**。故标记以人工层为准：人工层没有标记，就不从重建层继承。
    manual_ids = {
        str(n.get("id") or "")
        for n in (base.get("nodes") or [])
        if isinstance(n, dict)
        and not _is_lens_generated(n)
        and str(n.get("id") or "")
    }

    merged: dict[str, dict] = {}
    order: list[str] = []
    for n in (base.get("nodes") or []) + (g.get("nodes") or []) \
            + (g.get("hypothesis_nodes") or []):
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if not nid:
            continue
        if nid in merged:
            incoming = dict(n)
            # manual_ids 的语义就是"人工层存在且无标记"，故此处不再判
            # incoming 自带标记与否——判了就永远不触发（实测踩过）。
            if nid in manual_ids:
                # 人工层无标记：不继承重建层的标记（保住人工节点不被剥离）
                incoming.pop("generated_by", None)
                props = incoming.get("props")
                if isinstance(props, dict):
                    props = dict(props)
                    props.pop("generated_by", None)
                    incoming["props"] = props
            merged[nid].update(incoming)   # R-4：重建层覆盖同 id 系统节点
        else:
            merged[nid] = dict(n)
            order.append(nid)
        if nid in layout:
            merged[nid].update(layout[nid])   # R-3：坐标不被重扫冲掉

    edges: dict[str, dict] = {}
    for e in (base.get("edges") or []) + (g.get("edges") or []) \
            + (g.get("infer_edges") or []):
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        if not eid:
            continue
        edges[eid] = dict(edges[eid], **e) if eid in edges else dict(e)

    # 悬空边防御：端点不在合并后节点集合里的边必须剔除（G6 遇之整图白屏）。
    # 典型成因：观察靶心是某个主体，但该主体从未提升到案件画布（demoX 即如此），
    # 重建层的「研判得出」边就指向了一个图上不存在的节点。如实计数不静默丢。
    node_ids = set(merged.keys())
    kept_edges: list[dict] = []
    dropped = 0
    for e in edges.values():
        s = str(e.get("source") or "")
        t = str(e.get("target") or "")
        if s in node_ids and t in node_ids:
            kept_edges.append(e)
        else:
            dropped += 1
    if stats_sink is not None:
        stats_sink["edges_dropped"] = \
            stats_sink.get("edges_dropped", 0) + dropped

    out = dict(base)
    out["nodes"] = [merged[i] for i in order]
    out["edges"] = kept_edges
    return out
