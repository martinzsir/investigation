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
def _node_name_from_pk(nodes: list, pk: str, conn=None) -> str | None:
    """从 detail.nodes 或语义层反查代理键对应的人名。

    nodes 列表是镜头 detail 自带的（relation_neighborhood 的 nodes[]），
    优先从这里查；查不到再下语义层 obj_person 按 person_id 反查 raw_name。
    """
    for n in (nodes or []):
        if not isinstance(n, dict):
            continue
        if str(n.get("pk") or "") == pk:
            return str(n.get("name") or "").strip() or None
    if conn is not None and pk:
        try:
            rows = conn.execute(
                "SELECT raw_name FROM obj_person WHERE person_id = ?",
                [pk]).fetchall()
            if rows:
                return str(rows[0][0]).strip() or None
        except Exception:
            pass
    return None


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
    # --- §2/§4：从观察 detail 提取地点、共现人物、组织主体，上画布 ---
    # detail 在不同镜头下分支不同，五种取数路径并存：
    # · geo_accompany.detail.companion.person_a/person_b/locations
    # · geo_site_profile.detail.sites[].location_id/std_address
    # · geo_anomaly.detail.anomalies[].location_id/std_address + co_present[]
    # · relation_neighborhood.detail.nodes[].type/name + edges[]
    # · relation_org_interest.detail.row.raw_name(组织) + legal_rep/matched_person(人)
    #
    # 原版只取 companion 分支，导致 sites/anomalies 跑出 10 个地点、3 个偏离
    # 地点全不上图，邻域节点也整体丢失。本节按分支逐项归并到三桶：
    # locations（location_id → std_address，键去重）、companions（人物名）、
    # orgs（组织名）。
    #
    # locations_seen 用 dict 不用 set：value 存 detail 自带的 std_address
    # （中文地名），交给 build_place_node 作 label 优先值。若只存 location_id
    # 集合，build_place_node 会被传 label=loc_xxx 哈希，跳过 row.std_address
    # 中文 fallback（label 优先级 label or row.std_address or lid），画布上
    # 地点节点就显示成 loc_25e5a2764348 这种机器编号。
    extra_nodes: list[dict[str, Any]] = []
    extra_edges: list[dict[str, Any]] = []
    locations_seen: dict[str, str] = {}
    companions_seen: set[str] = set()
    orgs_seen: set[str] = set()
    primary_subjects: dict[str, dict] = {}
    # 组织↔人物定向关联（relation_org_interest）：只建有断言/命中记录的边，
    # 避免所有 companion 与所有 org 的完全图。
    org_person_links: dict[str, set[str]] = {}
    # 通话关系边（relation_neighborhood / relation_paths 的 detail.edges 提取）
    call_links: list[dict] = []
    for o in obs:
        d = o.get("detail") if isinstance(o.get("detail"), dict) else {}

        # 提取主主体（detail.subject）：place 靶心发起的 geo_* 镜头，
        # 观察 detail 里 subject 才是"谁在行动"。共现人物和地点的边
        # 应该挂到主主体上，而不是挂在 place 节点自身（否则产生自环）。
        subj = d.get("subject")
        if isinstance(subj, dict):
            pk = str(subj.get("pk") or subj.get("person_pk") or "").strip()
            nm = str(subj.get("name") or "").strip()
            if pk and nm:
                primary_subjects[nm] = {"pk": pk, "name": nm}
            elif nm:
                primary_subjects.setdefault(nm, {"pk": "", "name": nm})

        # 分支 1：geo_accompany → companion.person_a/person_b + companion.locations
        comp = d.get("companion") if isinstance(d.get("companion"), dict) else {}
        if comp:
            for loc in (comp.get("locations") or []):
                if isinstance(loc, str) and loc.strip():
                    # str 形态：地名文本既是 id 也是 label，自洽
                    locations_seen.setdefault(loc.strip(), loc.strip())
                elif isinstance(loc, dict):  # 兼容 locations 内嵌对象的形态
                    lid = str(loc.get("location_id")
                              or loc.get("std_address") or "").strip()
                    addr = str(loc.get("std_address") or "").strip()
                    if lid:
                        # 已存且新 addr 更全则覆盖（detail 多次出现时取最全）
                        if lid not in locations_seen or (
                                addr and not locations_seen[lid]):
                            locations_seen[lid] = addr
            for key in ("person_a", "person_b"):
                p = comp.get(key)
                if isinstance(p, str) and p.strip():
                    companions_seen.add(p.strip())

        # 分支 2：geo_site_profile → sites[].location_id/std_address
        for site in (d.get("sites") or []):
            if not isinstance(site, dict):
                continue
            lid = str(site.get("location_id") or "").strip()
            addr = str(site.get("std_address") or "").strip()
            if lid:
                if lid not in locations_seen or (
                        addr and not locations_seen[lid]):
                    locations_seen[lid] = addr
            elif addr:
                # 无 location_id 时用 addr 作 key（build_place_node 查不到
                # row，但 label 会 fallback 到 addr 本身，仍是中文）
                locations_seen.setdefault(addr, addr)

        # 分支 3：geo_anomaly → anomalies[].location_id/std_address + co_present[]
        for anom in (d.get("anomalies") or []):
            if not isinstance(anom, dict):
                continue
            lid = str(anom.get("location_id") or "").strip()
            addr = str(anom.get("std_address") or "").strip()
            if lid:
                if lid not in locations_seen or (
                        addr and not locations_seen[lid]):
                    locations_seen[lid] = addr
            elif addr:
                locations_seen.setdefault(addr, addr)
            for p in (anom.get("co_present") or []):
                if isinstance(p, str) and p.strip():
                    companions_seen.add(p.strip())
                elif isinstance(p, dict):  # co_present_refs 嵌套对象
                    nm = str(p.get("name") or "").strip()
                    if nm:
                        companions_seen.add(nm)

        # 分支 4：relation_neighborhood / relation_paths → nodes[].type/name + edges[]
        # type=person 走人物桶；type=org/company 走组织桶；account 是「物」不是
        # 主体——跳过（账户上画布归物品层 canvas_item_source 管），否则账户名
        # （=户主姓名）会被当组织画出与人物同名的幽灵节点。
        for n in (d.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            ntype = str(n.get("type") or "").strip().lower()
            nname = str(n.get("name") or "").strip()
            if not nname:
                continue
            if ntype == "account":
                continue
            if ntype in ("org", "organization", "company"):
                orgs_seen.add(nname)
            else:
                companions_seen.add(nname)

        # 提取 calls_to 边 → subject↔subject「联系」边（P1 新增）
        # detail.edges 是图算法返回的边列表，含 src/dst/edge/edge_pk 等。
        # 只取 edge=="calls_to" 且两端都是 person 的边，避免把 owns 等
        # 组织边也当通话关系。src/dst 是代理键（person_xxx），需反查名字。
        for e in (d.get("edges") or []):
            if not isinstance(e, dict):
                continue
            if str(e.get("edge") or "") != "calls_to":
                continue
            src_pk = str(e.get("src") or "").strip()
            dst_pk = str(e.get("dst") or "").strip()
            if not src_pk or not dst_pk:
                continue
            # 反查人名：先查 nodes 列表（同 detail 里已有），再查语义层
            src_name = _node_name_from_pk(d.get("nodes"), src_pk, conn)
            dst_name = _node_name_from_pk(d.get("nodes"), dst_pk, conn)
            if not src_name or not dst_name:
                continue
            # 跳过靶心自己（自己打给自己不算关系）
            if target_label and src_name == target_label and dst_name == target_label:
                continue
            call_links.append({
                "src": src_name, "dst": dst_name,
                "src_pk": src_pk, "dst_pk": dst_pk,
                "edge_pk": str(e.get("edge_pk") or ""),
            })

        # 分支 5：relation_org_interest → row.raw_name(组织) + 法人/关联人
        # matched_person 可能是 list（一人挂多个候选主体）或 str，
        # 直接 str(list) 会把 ['张卫国'] 当字符串字面量上画布，必须分类型。
        row = d.get("row") if isinstance(d.get("row"), dict) else {}
        if row:
            org_name = str(row.get("raw_name") or "").strip()
            if org_name:
                orgs_seen.add(org_name)
            for nm_key in ("legal_rep", "matched_person"):
                v = row.get(nm_key)
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            companions_seen.add(item.strip())
                            # 记录组织↔人物的定向关联（用于创建双向边）
                            org_person_links.setdefault(org_name, set()).add(
                                item.strip())
                        elif isinstance(item, dict):
                            nm_s = str(item.get("name") or "").strip()
                            if nm_s:
                                companions_seen.add(nm_s)
                                org_person_links.setdefault(org_name, set()).add(nm_s)
                elif isinstance(v, str) and v.strip():
                    companions_seen.add(v.strip())
                    org_person_links.setdefault(org_name, set()).add(v.strip())

    # --- §6：确定 位于/同现 边的源端 ---
    # 靶心是 person → 源端 = 靶心（兼容既有行为）
    # 靶心是 place/event → 源端 = 观察的主主体（detail.subject）
    target_kind = (parse_case_node_id(target_node_id) or {}).get("kind", "")
    target_is_person = target_kind in ("subject", "person")
    edge_sources: list[tuple[str, str]] = []
    if target_is_person:
        edge_sources.append((target_node_id, target_label or ""))

    # --- 先创建所有节点（避免边引用不到节点 ID） ---
    place_nodes: dict[str, dict] = {}
    for lid, addr in sorted(locations_seen.items()):
        pnode = build_place_node(case_id=case_id, location_id=lid,
                                 label=addr or "", conn=conn)
        pnode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
        extra_nodes.append(pnode)
        place_nodes[lid] = pnode

    primary_nodes: dict[str, dict] = {}
    for name, info in sorted(primary_subjects.items()):
        if target_label and name == target_label:
            continue
        snode = build_subject_node(case_id=case_id, name=name,
                                   sub_type="person", conn=conn)
        if info.get("pk"):
            snode.setdefault("props", {})["person_pk"] = info["pk"]
        snode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
        extra_nodes.append(snode)
        primary_nodes[name] = snode
        if not target_is_person:
            edge_sources.append((snode["id"], name))

    companion_nodes: dict[str, dict] = {}
    for person in sorted(companions_seen):
        if target_label and person == target_label:
            continue
        snode = build_subject_node(case_id=case_id, name=person, conn=conn)
        snode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
        extra_nodes.append(snode)
        companion_nodes[person] = snode

    org_nodes: dict[str, dict] = {}
    for org_name in sorted(orgs_seen):
        snode = build_subject_node(case_id=case_id, name=org_name,
                                   sub_type="organization", conn=conn)
        snode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
        extra_nodes.append(snode)
        org_nodes[org_name] = snode

    # --- 再创建所有边（源端按 target kind 路由） ---
    for lid, pnode in place_nodes.items():
        for src_id, src_label in edge_sources:
            if src_id == pnode["id"]:
                continue  # 自环跳过
            extra_edges.append({
                "id": case_edge_id(src_id, "位于", pnode["id"]),
                "source": src_id,
                "target": pnode["id"],
                "rel": "位于",
                "system": True,
                "generated_by": GENERATED_BY_LENS,
                "note": f"「{name if lens_name else '研判'}」观察地点",
            })

    for person, snode in companion_nodes.items():
        for src_id, src_label in edge_sources:
            if person == src_label:
                continue  # 自己不同现自己
            extra_edges.append({
                "id": case_edge_id(src_id, "同现", snode["id"]),
                "source": src_id,
                "target": snode["id"],
                "rel": "同现",
                "system": True,
                "generated_by": GENERATED_BY_LENS,
                "note": f"「{name if lens_name else '研判'}」时空同框",
            })

    for org_name, snode in org_nodes.items():
        # 单位利益关联的边源端：按 org_person_links 连每个命中人（靶心命中
        # 用靶心节点，其他命中人走 companion_nodes），不再只连靶心——
        # 「张卫国→某单位←李志强」这条间接路径需要两条边都画出来。
        # 无命中人信息时保底维持靶心→组织边（旧行为）。
        linked_persons = org_person_links.get(org_name, set())
        linked_ids: list[str] = []
        for pname in sorted(linked_persons):
            if target_label and pname == target_label:
                linked_ids.append(target_node_id)
            elif pname in companion_nodes:
                linked_ids.append(companion_nodes[pname]["id"])
            # 命中人既非靶心也无 companion 节点（如 detail.nodes 未提取）：
            # 跳过，不为其新造节点——生长层只把"已知的人"连到组织上。
        if not linked_ids:
            linked_ids = [src_id for src_id, _ in edge_sources]
        for src_id in linked_ids:
            if src_id == snode["id"]:
                continue
            extra_edges.append({
                "id": case_edge_id(src_id, "同现", snode["id"]),
                "source": src_id,
                "target": snode["id"],
                "rel": "同现",
                "system": True,
                "generated_by": GENERATED_BY_LENS,
                "note": f"「{name if lens_name else '研判'}」单位利益关联",
            })

    # --- 通话关系边（联系）：从 relation_neighborhood/relation_paths 的
    # detail.edges 提取 calls_to，创建 subject↔subject 的「联系」边。
    # 与「同现」（时空同框）区分：远程通话不属于同框，但同样是主体间关系。
    # 按无向人对合并（A→B 与 B→A 是同一条联系），通话次数取语义层
    # lnk_calls_to 的真实边数——detail.edges 是图算法去重后的类型级边，
    # 条数恒为 1，拿它当次数会把「通话 20 次」画成「通话 1 次」。
    link_agg: dict[tuple[str, str], dict] = {}
    for cl in call_links:
        key = tuple(sorted((cl["src_pk"], cl["dst_pk"])))
        agg = link_agg.setdefault(key, {"src": cl["src"], "dst": cl["dst"],
                                        "src_pk": cl["src_pk"],
                                        "dst_pk": cl["dst_pk"],
                                        "edge_pks": []})
        if cl.get("edge_pk"):
            agg["edge_pks"].append(cl["edge_pk"])

    # 语义层真实通话计数（双向合计）；无连接时退化为 detail 边条数
    pair_counts: dict[tuple[str, str], int] = {}
    if conn is not None and link_agg:
        try:
            for fp, tp, cnt in conn.execute(
                    "SELECT from_person, to_person, COUNT(*) "
                    "FROM lnk_calls_to GROUP BY 1, 2").fetchall():
                k = tuple(sorted((str(fp), str(tp))))
                pair_counts[k] = pair_counts.get(k, 0) + int(cnt)
        except Exception:
            pair_counts = {}

    # 为通话关系创建节点（若尚未因 nodes 提取而创建）
    call_persons: dict[str, dict] = {}
    for key, agg in sorted(link_agg.items()):
        for pname in (agg["src"], agg["dst"]):
            if pname in call_persons:
                continue
            if target_label and pname == target_label:
                # 靶心本人是通话一端：直接用靶心节点，不另建——否则
                # 「李志强↔张卫国」这种靶心参与的通话永远缺端点、边被丢弃
                call_persons[pname] = {"id": target_node_id}
            elif pname in companion_nodes:
                call_persons[pname] = companion_nodes[pname]
            else:
                snode = build_subject_node(case_id=case_id, name=pname, conn=conn)
                snode.setdefault("props", {})["generated_by"] = GENERATED_BY_LENS
                extra_nodes.append(snode)
                call_persons[pname] = snode

    # 创建「联系」边
    for key, agg in sorted(link_agg.items()):
        src_node = call_persons.get(agg["src"]) or companion_nodes.get(agg["src"])
        dst_node = call_persons.get(agg["dst"]) or companion_nodes.get(agg["dst"])
        if not src_node or not dst_node:
            continue
        if src_node["id"] == dst_node["id"]:
            continue  # 自环跳过
        cnt = pair_counts.get(key, 0) or max(len(agg["edge_pks"]), 1)
        note = f"「{name if lens_name else '研判'}」通话 {cnt} 次"
        extra_edges.append({
            "id": case_edge_id(src_node["id"], "联系", dst_node["id"]),
            "source": src_node["id"],
            "target": dst_node["id"],
            "rel": "联系",
            "system": True,
            "generated_by": GENERATED_BY_LENS,
            "note": note,
            "call_count": cnt,
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
