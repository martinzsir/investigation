"""CAN-19：研判结果挂到**发起它的靶心节点**下。

业务起点
--------
正兵在画布上选中「张卫国」，跑一次异常轨迹。此前这条结果只落进观察档案，
画布上不出现任何东西——工具能跑、结果有、但**图不长**。图不长，就无所谓
"顺着图往下查"，整个研判层画布的立意落空。

本模块把一次镜头运行的观察，聚成**一个研判结论节点**，挂到发起它的那个
节点下，并按归属自动连到假设。

三条红线
--------
R-1 结论节点按 **(靶心 × 镜头)** 聚合，不按观察逐条铺。
    一个镜头跑出 20 条观察就铺 20 个节点，主画布会被淹没（既有
    canvas_seed.build_origin_lens_layer 就是逐条铺，只因它是线索级、
    每线索深挖次数少才撑得住）。案件级画布上多线索并发，必须聚合。

R-2 精度取**木桶最弱档**，且 unknown 必须先剔除。
    · 取最弱：1 条 minute 档不该把 12 条 date 档抬成实线粗边——那是
      "次数会骗人"换了个方向（用最精确的一条代表整组）。节点标最弱档，
      完整构成由 precision_breakdown 交给证据窗口呈现。
    · 剔除 unknown：它是木桶里最低档，一旦参与比较就永远赢，会把真实
      精度吞掉。这个坑在地图侧踩过一次（木桶算出 unknown 而非 date）。

R-3 挂错比不挂更坏——**记录过别的靶心的观察，绝不混入本组**。
    混入就会出现"在 A 身上跑的结果挂到了 B 名下"，且正兵无从察觉。
    这类观察被排除并如实报出，不静默丢弃。

为什么只读定向观察
------------------
批量扫描的观察没有 origin.node_id，无从知道"谁发起的"。强行按维度归到
某个节点名下，就是把全案观察当成某人专属证据——正是 CAN-19 要治的病。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from server.app.canvas_case import (GENERATED_BY_LENS, LENS_RESULT_REL,
                                    PRECISION_WEIGHT, build_hypothesis_layer,
                                    build_infer_edges, build_place_node,
                                    build_result_node, build_subject_node,
                                    case_edge_id, case_node_id, dim_of,
                                    line_style, obs_precision,
                                    parse_case_node_id, weaker_precision)

# 排除原因（前端可据此提示"该次运行的归属与当前节点不符"）
EXCLUDE_OTHER_TARGET = "other_target"
EXCLUDE_NO_TARGET = "no_target"


# ----------------------------------------------------------------------
# 观察 → 靶心
# ----------------------------------------------------------------------
def origin_node_of(o: dict) -> str:
    """观察记录的发起节点（origin.node_id）；没记返回空串。"""
    origin = o.get("origin") if isinstance(o.get("origin"), dict) else {}
    return str((origin or {}).get("node_id") or "").strip()


def group_by_target(observations: list[dict],
                    target_node_id: str | None = None,
                    ) -> tuple[dict[tuple[str, str], list[dict]], list[dict]]:
    """按 (靶心节点 × 镜头) 分组；返回 (分组, 被排除项)。

    ``target_node_id`` 为空时按观察自带 origin.node_id 归组（重建整层用）。
    指定时：只收"记录了本靶心"与"压根没记靶心"的两类；**记录了别的靶心
    的一律排除并报出**（R-3）。

    "没记靶心"的那类为什么收：早期运行（或路由未透传 node_id）的产物若被
    整批丢掉，正兵会看到"跑过但图上没有"——那比挂在默认位置更费解。它们
    由调用方指定的靶心承接，并在 meta 里如实标注来源。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    excluded: list[dict] = []
    for o in observations or []:
        if not isinstance(o, dict):
            continue
        nid = origin_node_of(o)
        if target_node_id and nid and nid != str(target_node_id):
            excluded.append({
                "observation_id": str(o.get("observation_id") or ""),
                "skill_id": str(o.get("skill_id") or ""),
                "recorded_node_id": nid,
                "reason": EXCLUDE_OTHER_TARGET,
                "note": (f"该观察记录的发起节点是 {nid}，与当前靶心不符，"
                         f"未并入本次结论"),
            })
            continue
        node_id = str(target_node_id or nid or "")
        if not node_id:
            excluded.append({
                "observation_id": str(o.get("observation_id") or ""),
                "skill_id": str(o.get("skill_id") or ""),
                "recorded_node_id": "",
                "reason": EXCLUDE_NO_TARGET,
                "note": "未记录发起节点且调用方未指定靶心，无从挂接",
            })
            continue
        lens_id = str(o.get("skill_id") or "") or "unknown"
        groups.setdefault((node_id, lens_id), []).append(o)
    return groups, excluded


# ----------------------------------------------------------------------
# 精度聚合（R-2）
# ----------------------------------------------------------------------
def aggregate_precision(observations: list[dict]) -> tuple[str, dict[str, int]]:
    """结论节点精度 = 木桶最弱档；返回 (档位, 各档条数)。"""
    counts: dict[str, int] = {}
    known: list[str] = []
    for o in observations or []:
        p = obs_precision(o)
        counts[p] = counts.get(p, 0) + 1
        if p != "unknown":
            known.append(p)
    if not known:
        return "unknown", counts
    worst = known[0]
    for p in known[1:]:
        worst = weaker_precision(worst, p)
    return worst, counts


def aggregate_dim_cells(observations: list[dict]) -> dict[str, dict[str, Any]]:
    """按三维聚合证据矩阵：{dim: {hit,count,precision,weight}}。

    供证据窗口直接渲染（前端 dimCellsOf 消费对象形状；发字符串数组会让
    三维全 miss、加权分恒 0）。每维精度同样取木桶最弱档、unknown 不参与
    比较（与 aggregate_precision 同口径）；全 unknown 时档位 unknown、
    权重 0，但 hit 仍为真——"查了但精度不足"与"没查"不能画成一样。
    非标准三维前缀（如 fund_/comm_）不进矩阵，由 dims 列表保留声明。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for o in observations or []:
        d = dim_of(o.get("skill_id"))
        if not d:
            continue
        cell = buckets.setdefault(d, {"count": 0, "_known": []})
        cell["count"] += 1
        p = obs_precision(o)
        if p != "unknown":
            cell["_known"].append(p)
    out: dict[str, dict[str, Any]] = {}
    for d, cell in buckets.items():
        known = cell["_known"]
        prec = known[0] if known else "unknown"
        for p in known[1:]:
            prec = weaker_precision(prec, p)
        out[d] = {
            "hit": cell["count"] > 0,
            "count": cell["count"],
            "precision": prec,
            "weight": round(float(PRECISION_WEIGHT.get(prec, 0.0)), 2),
        }
    return out


def _result_ref(target_node_id: str, lens_id: str) -> str:
    """结论节点 ref：靶心 + 镜头，稳定幂等（同一靶心重跑同一镜头不增节点）。"""
    parsed = parse_case_node_id(target_node_id) or {}
    base = str(parsed.get("ref") or "").strip() or str(target_node_id or "unknown")
    return f"{lens_id}@{base}"


def _catalog():
    """镜头注册表（延迟导入：避免装载期就把 packs 全部读一遍）。"""
    try:
        from core.lens_catalog import (lens_dims, lens_functions, lens_name)
        return lens_name, lens_functions, lens_dims
    except Exception:
        return None, None, None


# ----------------------------------------------------------------------
# 单组 → 结论节点 + 挂接边
# ----------------------------------------------------------------------
def build_result_layer(*, case_id: str, target_node_id: str, lens_id: str,
                       observations: list[dict], lens_name: str | None = None,
                       function_id: str | None = None,
                       assumption: str | None = None,
                       pack: str = "default",
                       base_dir=None,
                       target_label: str | None = None,
                       conn=None) -> dict[str, Any]:
    """(靶心 × 镜头) 一组的观察 → 结论节点 + 挂到靶心下的边。

    地点/共现人物也从观察 detail 提取并上画布（§2/§4）：
    - detail.companion.locations → place 节点 + 靶心→place 的「位于」边
    - detail.companion.person_a/person_b → subject 节点 + 靶心→subject 的「同现」边
      （靶心本人按 target_label 跳过，避免重复节点）
    """
    obs = [o for o in (observations or []) if isinstance(o, dict)]
    if not obs:
        return {"node": None, "edge": None, "reason": "该组没有观察"}

    precision, breakdown = aggregate_precision(obs)
    dim_cells = aggregate_dim_cells(obs)
    name_fn, fns_fn, dims_fn = _catalog()

    dims: list[str] = []
    for o in obs:
        d = dim_of(o.get("skill_id"))
        if d and d not in dims:
            dims.append(d)
    # 维度兜底：dim_of 只认 geo_/timeline_/relation_ 前缀，fund_/comm_ 镜头
    # 全部落空（dim_count 恒 0）。维度应由注册表的 produces_dims 声明。
    if not dims and dims_fn:
        for d in dims_fn(lens_id, base_dir):
            if d not in dims:
                dims.append(d)

    # 元信息：调用方显式传入优先，缺则从 packs/*/pack.json 反查。
    # 真实路径（镜头路由 → 观察落库 → 画布还原）隔了好几层，靠调用方
    # 记得传必然丢——丢了的后果是标签变机器编号、假设长不出来。
    meta_source = "caller"
    if not lens_name and name_fn:
        lens_name = name_fn(lens_id, base_dir) or None
        if lens_name:
            meta_source = "catalog"

    if not function_id and fns_fn:
        declared = fns_fn(lens_id, base_dir)
        if len(declared) == 1:
            function_id = declared[0]       # 多内核不取单值，见下
        elif len(declared) > 1:
            function_id = None
            meta_source = "catalog"

    # 假设归属：优先调用方，否则按 CAN-18 从 Function 反查；仍判不出则
    # 走镜头 → uses_functions → 规则 的整链。都不出就留 None + 原因，不硬猜。
    assumption_note = ""
    assumptions: list[str] = []
    if assumption:
        assumptions = [str(assumption)]
    else:
        # 调用方给了内核就先按内核反查（显式优先），否则走镜头整链。
        if function_id:
            try:
                from core.lens_assumption import resolve_function_assumption
                h, n = resolve_function_assumption(function_id, pack, base_dir)
                if h:
                    assumptions = [h]
                assumption_note = n
            except Exception:
                assumption_note = "假设归属解析失败"
        if not assumptions:
            try:
                from core.lens_catalog import resolve_lens_assumptions
                assumptions, assumption_note = resolve_lens_assumptions(
                    lens_id, pack, base_dir)
            except Exception:
                assumptions, assumption_note = [], "假设归属解析失败"
        # 多归属：如实全部列出，不挑一个（与"重名不自裁"同源）。
        # 单值置 None——挑一个就是假答案，方向判断留给正兵。
        if len(assumptions) > 1:
            assumption = None
        elif len(assumptions) == 1:
            assumption = assumptions[0]

    name = str(lens_name or lens_id or "研判结论")
    # R-1：查不到名字就不拿 lens_id 冒充——正兵读不懂机器编号，
    # 看着有标题其实等于没有。此时标签退成通用名，id 留在 props 里。
    label = f"{name}（{len(obs)} 条）" if lens_name else f"研判结论（{len(obs)} 条）"
    node = build_result_node(
        case_id=case_id,
        result_ref=_result_ref(target_node_id, lens_id),
        label=label,
        lens_id=lens_id, function_id=function_id,
        assumption=assumption, precision=precision, dims=dims,
        dim_cells=dim_cells,
        extra={
            "lens_name": (name if lens_name else None),
            "target_node_id": target_node_id,
            "observation_ids": [str(o.get("observation_id") or "") for o in obs],
            "observation_count": len(obs),
            "run_ids": sorted({str(o.get("run_id") or "")
                               for o in obs if o.get("run_id")}),
            "precision_breakdown": breakdown,
            "assumption_note": assumption_note or None,
            # 多归属如实列出（单值已置 None），供 HYP 系列建多条推断边
            "assumptions": assumptions,
            "meta_source": meta_source,
            # 名字来自注册表而非调用方透传时记一笔：标签出现机器编号就能
            # 立刻定位是"包里没写 name"还是"调用链没传"，不用逐层猜
            "unresolved_name": not bool(lens_name),
        },
    )
    st = line_style(precision)
    edge = {
        "id": case_edge_id(target_node_id, LENS_RESULT_REL, node["id"]),
        "source": target_node_id,
        "target": node["id"],
        "rel": LENS_RESULT_REL,
        "system": True,               # 可删不可改
        "precision": st["precision"],
        "stroke_dasharray": st["stroke_dasharray"],
        "line_width": st["line_width"],
        # 镜头重建层标记：落库前剥离（同 build_result_node）
        "generated_by": GENERATED_BY_LENS,
        "note": f"由该节点发起的「{name if lens_name else '研判'}」"
                f"研判得出（精度档 {st['precision']}）",
    }
    # --- §2/§4：从观察 detail 提取地点和共现人物，创建独立画布节点 ---
    extra_nodes: list[dict[str, Any]] = []
    extra_edges: list[dict[str, Any]] = []
    locations_seen: set[str] = set()
    companions_seen: set[str] = set()
    for o in obs:
        d = o.get("detail") if isinstance(o.get("detail"), dict) else {}
        comp = d.get("companion") if isinstance(d.get("companion"), dict) else {}
        if not comp:
            continue
        for loc in (comp.get("locations") or []):
            if isinstance(loc, str) and loc.strip():
                locations_seen.add(loc.strip())
        for key in ("person_a", "person_b"):
            p = comp.get(key)
            if isinstance(p, str) and p.strip():
                companions_seen.add(p.strip())

    # 地点 → place 节点 + 靶心→place 的「位于」边
    for loc in sorted(locations_seen):
        pnode = build_place_node(case_id=case_id, location_id=loc,
                                 label=loc)
        # 重建层标记必须打在节点上（边已有）：split_persistent 按
        # props.generated_by 剥离，漏标会让地点在首次 PATCH 时被当人工层留存
        pnode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
        extra_nodes.append(pnode)
        extra_edges.append({
            "id": case_edge_id(target_node_id, "位于", pnode["id"]),
            "source": target_node_id,
            "target": pnode["id"],
            "rel": "位于",
            "system": True,
            "generated_by": GENERATED_BY_LENS,
            "note": f"「{name if lens_name else '研判'}」观察地点",
        })

    # 共现人物 → subject 节点 + 靶心→subject 的「同现」边
    # 跳过靶心本人（target_label 匹配），避免同一个人产出两个节点
    for person in sorted(companions_seen):
        if target_label and person == target_label:
            continue
        # 传语义层 conn：按名字解析出 person_/org_ 主键，id 才能与人工提升的
        # 靶心节点同键（merge 时按 R-4 去重）。不传 conn 会退化成名字哈希 id
        # （case#...:subject:<sha1[:12]>）——演示数据里哈希恰好撞主键长相，
        # 排查时极易误判。
        snode = build_subject_node(case_id=case_id, name=person, conn=conn)
        snode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
        extra_nodes.append(snode)
        extra_edges.append({
            "id": case_edge_id(target_node_id, "同现", snode["id"]),
            "source": target_node_id,
            "target": snode["id"],
            "rel": "同现",
            "system": True,
            "generated_by": GENERATED_BY_LENS,
            "note": f"「{name if lens_name else '研判'}」时空同框",
        })

    return {"node": node, "edge": edge, "reason": "",
            "extra_nodes": extra_nodes, "extra_edges": extra_edges}


# ----------------------------------------------------------------------
# 整层：多组 + 假设自动入图
# ----------------------------------------------------------------------
def _resolve_lens_names(lens_ids: set[str],
                        given: dict[str, str] | None,
                        base_dir=None) -> dict[str, str]:
    """补齐镜头中文名：调用方透传优先，缺则查注册表（查不到不编造）。"""
    names = dict(given or {})
    missing = [lid for lid in lens_ids if not names.get(lid)]
    if not missing:
        return names
    name_fn, _, _ = _catalog()
    if not name_fn:
        return names
    for lid in missing:
        try:
            n = name_fn(lid, base_dir)
        except Exception:
            n = None
        if n:
            names[lid] = n
    return names


def filter_by_clue(observations: list[dict],
                   clue_id: str | None) -> list[dict]:
    """线索域过滤：只保留 origin.clue_id 命中的观察。

    clue_id 为 None（案件级画布）= 不过滤，全部可见；
    非空（线索画布）= 仅本线索发起的观察，案件级发起（无 clue_id）的不混入
    ——跨域并线正是 R-3「挂错比不挂更坏」的同类问题。
    """
    if clue_id is None:
        return list(observations or [])
    want = str(clue_id)
    out: list[dict] = []
    for o in observations or []:
        if not isinstance(o, dict):
            continue
        origin = o.get("origin") if isinstance(o.get("origin"), dict) else {}
        if str(origin.get("clue_id") or "") == want:
            out.append(o)
    return out


def build_lens_result_layers(*, case_id: str, observations: list[dict],
                             target_node_id: str | None = None,
                             lens_names: dict[str, str] | None = None,
                             function_ids: dict[str, str] | None = None,
                             pack: str = "default",
                             base_dir=None,
                             target_labels: dict[str, str] | None = None,
                             revealed: set[tuple[str, str]] | None = None,
                             on_canvas_ids: set[str] | None = None,
                             clue_id: str | None = None,
                             conn=None
                             ) -> dict[str, Any]:
    """把定向观察整层还原成画布元素。

    返回 {nodes, edges, hypothesis_nodes, infer_edges, meta}。假设节点与
    推断边复用 HYP 系列：**结论一挂上画布，假设就自己长出来、证据自己连上
    去**——不让正兵再手工连一遍。

    target_labels: {node_id: label}，用于跳过靶心本人的 subject 节点（§2/§4）。

    渐进式生成（revealed）
    ---------------------
    - None = 不过滤（兼容既有调用/测试：scope 查询、线索画布增量并入）；
    - set[(target_node_id, lens_id)] = 仅为已揭示组产出节点/边/假设。
    - meta.groups **始终枚举全部组**并带 revealed 标志，供前端清单展示。
    - 假设层只从已构建（已揭示）的结论节点反查——首个支撑结论被揭示时
      假设才出现，天然满足 v3 §6。

    on_canvas_ids: 当前持久画布上的节点 id 集合，供清单标注靶心是否在图上
    （靶心缺失时挂边会悬空，前端据此提示先提升主体）。

    clue_id: 非空时只保留 origin.clue_id 命中的观察（线索域，见 filter_by_clue）。
    """
    observations = filter_by_clue(observations, clue_id)
    groups, excluded = group_by_target(observations, target_node_id)
    fids = function_ids or {}
    tlabels = target_labels or {}
    names = _resolve_lens_names({lid for _, lid in groups}, lens_names,
                                base_dir)
    canvas_ids = on_canvas_ids if on_canvas_ids is not None else set()

    # 清单始终枚举全部组：未揭示的组也要让正兵知道"还有什么可长"。
    all_keys = sorted(groups.keys())
    revealed_keys = (
        all_keys if revealed is None
        else [k for k in all_keys if k in revealed]
    )
    revealed_set = set(revealed_keys)

    meta_rows: list[dict[str, Any]] = []
    for node_id, lens_id in all_keys:
        obs = groups[(node_id, lens_id)]
        # 未揭示组不调 build_result_layer（不产节点），精度/假设用轻量聚合
        # 填清单——清单要能显示"这组是什么精度、指向哪个假设"供正兵决定揭示。
        precision, _ = aggregate_precision(obs)
        meta_rows.append({
            "target_node_id": node_id,
            "lens_id": lens_id,
            "lens_name": names.get(lens_id) or lens_id,
            "target_label": tlabels.get(node_id) or "",
            "on_canvas": node_id in canvas_ids,
            "revealed": (node_id, lens_id) in revealed_set,
            "observation_count": len(obs),
            "precision": precision,
            # 已揭示组在下方构建时回填真实归属；未揭示组先置 None
            "assumption": None,
        })

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    # extra_nodes/edges 按 ID 去重（同一地点/人物可能跨多组观察出现）
    extra_seen: set[str] = set()
    for node_id, lens_id in revealed_keys:
        obs = groups[(node_id, lens_id)]
        r = build_result_layer(case_id=case_id, target_node_id=node_id,
                               lens_id=lens_id, observations=obs,
                               lens_name=names.get(lens_id),
                               function_id=fids.get(lens_id),
                               target_label=tlabels.get(node_id),
                               pack=pack, base_dir=base_dir, conn=conn)
        if not r.get("node"):
            continue
        # 回填清单行的实际假设归属（构建时才做完整反查，比轻量聚合准）
        for row in meta_rows:
            if row["target_node_id"] == node_id \
                    and row["lens_id"] == lens_id:
                row["precision"] = r["node"]["props"].get("precision")
                row["assumption"] = r["node"]["props"].get("assumption")
                break
        nodes.append(r["node"])
        if r.get("edge"):
            edges.append(r["edge"])
        for en in (r.get("extra_nodes") or []):
            eid = str(en.get("id") or "")
            if eid and eid not in extra_seen:
                extra_seen.add(eid)
                nodes.append(en)
        for ee in (r.get("extra_edges") or []):
            eid = str(ee.get("id") or "")
            if eid and eid not in extra_seen:
                extra_seen.add(eid)
                edges.append(ee)

    hyp = build_hypothesis_layer(case_id=case_id, nodes=nodes, pack=pack,
                                 base_dir=base_dir)
    infer = build_infer_edges(case_id=case_id, result_nodes=nodes)

    reason = ""
    if not nodes:
        reason = ("没有可挂接的定向观察：发起时未带 node_id，"
                  "或观察记录的靶心与指定节点不符")
        if revealed is not None and not revealed_keys and groups:
            reason = "已有研判结果但尚未揭示：在研判结果清单中选择显示"

    return {
        "nodes": nodes,
        "edges": edges,
        "hypothesis_nodes": hyp.get("nodes") or [],
        "infer_edges": infer,
        "missing_hypotheses": hyp.get("missing") or [],
        "excluded": excluded,
        "meta": {"groups": meta_rows, "reason": reason,
                 "node_id_scope": target_node_id or "*"},
    }


# ----------------------------------------------------------------------
# 案件读侧：定向观察 → 整层
# ----------------------------------------------------------------------
def load_growth_layer(*, case_dir: str | Path, case_id: str,
                      target_node_id: str | None = None,
                      pack: str = "default",
                      base_dir=None,
                      target_labels: dict[str, str] | None = None,
                      revealed: set[tuple[str, str]] | None = None,
                      on_canvas_ids: set[str] | None = None,
                      clue_id: str | None = None,
                      conn=None
                      ) -> dict[str, Any]:
    """从案件级定向观察档案还原研判结果层（不挂版本，重扫不蒸发）。

    target_labels: {node_id: label}，用于跳过靶心本人的 subject 节点（§2/§4）。
    revealed: 渐进式揭示集，None=全部构建；set=仅构建已揭示组（清单仍全量）。
    on_canvas_ids: 持久画布节点 id，供清单标注靶心在图状态。
    clue_id: 线索域过滤（线索画布只看本线索发起的观察）。
    """
    try:
        from server.app.clues_artifact import load_directed_observations
        raw = list(load_directed_observations(case_dir))
    except Exception as e:
        return {"nodes": [], "edges": [], "hypothesis_nodes": [],
                "infer_edges": [], "excluded": [],
                "meta": {"groups": [], "reason": f"定向观察档案读取失败：{e}",
                         "node_id_scope": target_node_id or "*"}}
    obs: list[dict] = []
    seen: set[str] = set()
    for o in raw:
        d = o.to_dict() if hasattr(o, "to_dict") else dict(o)
        oid = str(d.get("observation_id") or "")
        if oid and oid in seen:
            continue
        seen.add(oid)
        obs.append(d)
    return build_lens_result_layers(case_id=case_id, observations=obs,
                                    target_node_id=target_node_id,
                                    pack=pack, base_dir=base_dir,
                                    target_labels=target_labels,
                                    revealed=revealed,
                                    on_canvas_ids=on_canvas_ids,
                                    clue_id=clue_id, conn=conn)
