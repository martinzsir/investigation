"""镜头元信息反查：lens_id → 中文名 / 计算内核 / 可否在画布调度。

为什么需要这个
--------------
CAN-19 冒烟时发现两个症状，同出一个根因：

1. 结论节点标签显示 ``geo_anomaly`` 这样的**机器编号**，而不是
   「异常轨迹镜头」——正兵看不懂，卡片上等于没标题。
2. 假设归属**判不出来**。假设是按 CAN-18 从 Function 反查的
   （Function → rule_id → hypothesis），而 ``function_id`` 没传进来，
   于是假设节点不会长出来、证据不会自动连上去——
   "结论挂上画布，假设自己长出来"这条链在真实路径上是断的。

根因是**元信息靠调用方记得传**。真实路径是
``镜头路由 → 观察落库 → 画布还原``，中间隔了好几层，任何一层没透传，
名字和编号就丢了。

单测抓不到——因为单测是把 name 和 function_id 直接喂进去的，走的
**不是真实入口**。这类问题只有跑真实档案才暴露。

设计主张：**元信息由服务端从镜头注册表反查，不靠调用方记得传。**
packs/*/pack.json 里每个 skill 自带 name / uses_functions，那就是唯一真相源；
调用方显式传入的值仍然优先（便于覆盖与测试注入）。

红线
----
R-1 查不到就如实报 Unknown，**不把 lens_id 当名字用**。
    ``geo_anomaly`` 当标题显示，看着有内容其实正兵读不懂，
    比空着更糟——空着至少知道"缺名字"。

R-2 多内核不挑一个。一个镜头声明了多个 uses_functions 时，逐个反查假设；
    得到多个不同假设就**全部列出**，与"重名不自裁""多归属出多条边"同源。
    单值 ``assumption`` 在多归属时置 None——挑一个就是假答案。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _packs_root(base_dir: Path | None = None) -> Path:
    """镜头包根目录（packs/），**恒定平台根**。

    packs/* 是平台级镜头插件，不进案件 ontology 快照（与镜头启停文件
    lenses.json 放案件根而非快照目录同口径）。历史上按 ontology base_dir
    推算其兄弟目录：平台根下 ontology/ 与 packs/ 恰好是兄弟，看似可行；
    但案件画布读面传入的是案件快照根（cases/<cid>/ontology），会推出
    cases/<cid>/packs——快照中根本没有 packs，整册变空，镜头中文名与
    假设归属全部反查失败（研判画布 §6「假设自己长出来」断链的根因）。

    base_dir 参数保留仅为签名兼容（假设模式库 hypothesis_patterns.json
    仍随案件快照走，但那不在本模块读取），不再参与 packs 定位。
    """
    return Path(__file__).resolve().parents[1] / "packs"


def load_catalog(base_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """全部镜头 → {lens_id: {name, pack_id, functions, canvas_enabled}}。

    读 packs/*/pack.json。单个包解析失败只跳过该包，不让整册失效——
    一个坏包就让所有镜头变成 Unknown，是故障放大。
    """
    import json

    root = _packs_root(base_dir)
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for pj in sorted(root.glob("*/pack.json")):
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pack_id = str(data.get("pack_id") or pj.parent.name)
        for sk in data.get("skills") or []:
            sid = str(sk.get("skill_id") or "").strip()
            if not sid:
                continue
            fns = [str(f) for f in (sk.get("uses_functions") or []) if str(f)]
            dims = [str(d) for d in (sk.get("produces_dims") or []) if str(d)]
            out[sid] = {
                "lens_id": sid,
                "name": str(sk.get("name") or "").strip(),
                "pack_id": pack_id,
                "functions": fns,
                "dims": dims,
                "canvas_enabled": bool(sk.get("canvas_enabled")),
                # 庙算假设挂钩（pack.json 显式声明，如 "H6"）。与 worker
                # 端 pack_loader → SkillSpec.assumption 同一真相源；空串
                # 表示业务未确认归属，由 uses_functions 规则链兜底反查。
                "assumption": str(sk.get("assumption") or "").strip(),
            }
    return out


def lens_meta(lens_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    """单个镜头元信息；未注册返回 None（调用方据此标注 Unknown）。"""
    sid = str(lens_id or "").strip()
    if not sid:
        return None
    return load_catalog(base_dir).get(sid)


def lens_name(lens_id: str, base_dir: Path | None = None) -> str | None:
    """镜头中文名；未注册或无 name 返回 None（R-1：不拿 id 冒充名字）。"""
    m = lens_meta(lens_id, base_dir)
    return (m or {}).get("name") or None


def lens_functions(lens_id: str, base_dir: Path | None = None) -> list[str]:
    """镜头声明的计算内核；未注册返回空列表。"""
    m = lens_meta(lens_id, base_dir)
    return list((m or {}).get("functions") or [])


def lens_dims(lens_id: str, base_dir: Path | None = None) -> list[str]:
    """镜头声明的产出维度（produces_dims）；未注册返回空列表。

    为什么需要：``canvas_case.dim_of`` 只认 geo_/timeline_/relation_ 三种前缀，
    fund_ 与 comm_ 镜头（CAN-17 新建的两个包）全部落空——那些结论节点的
    dim_count 恒为 0，维度归属在图上丢失。维度应由注册表声明，不靠前缀猜。
    """
    m = lens_meta(lens_id, base_dir)
    return list((m or {}).get("dims") or [])


# ----------------------------------------------------------------------
# 归属：镜头 → 假设（R-2 多内核不挑一个）
# ----------------------------------------------------------------------
def resolve_lens_assumptions(lens_id: str, pack: str = "default",
                             base_dir: Path | None = None
                             ) -> tuple[list[str], str]:
    """镜头 → (假设 id 列表, 说明)。

    两段归属，显式声明优先：
      0. pack.json 显式 ``assumption``（如 geo_accompany → H6）——与
         worker 端 SkillSpec.assumption 同一真相源，单值直接采用。
      1. 经 uses_functions 逐个走 CAN-18 反查：
         单一归属 → ([H4], "经内核 X → 规则 R2 反查")；
         多归属   → ([H4, H1], "多内核归属不同，全部列出")；
         判不出   → ([], 具体原因)   ← 不硬猜。

    显式优先的理由：pack.json 是镜头声明的登记处，规则链反查是包未
    声明时的推断兜底；两者漂移时以登记为准（geo_spatiotemporal_accompany
    就曾因只在反查表里缺失，导致 geo_accompany 的 H6 挂不上画布）。
    """
    sid = str(lens_id or "").strip()
    if not sid:
        return [], "未提供镜头 id"

    meta = lens_meta(sid, base_dir)
    declared = str((meta or {}).get("assumption") or "").strip()
    if declared:
        return [declared], (f"镜头 {sid} 在 {meta.get('pack_id')}/pack.json "
                            f"显式声明归属 {declared}")

    fns = list((meta or {}).get("functions") or [])
    if not fns:
        if meta is None:
            return [], f"镜头 {sid} 未在 packs/*/pack.json 注册"
        return [], f"镜头 {sid} 未声明 uses_functions，且未显式声明 assumption"

    try:
        from core.lens_assumption import resolve_function_assumption
    except Exception:
        return [], "假设归属解析模块不可用"

    found: list[str] = []
    notes: list[str] = []
    for fn in fns:
        hyp, note = resolve_function_assumption(fn, pack, base_dir)
        notes.append(f"{fn}: {note}")
        if hyp and hyp not in found:
            found.append(hyp)

    if not found:
        return [], "；".join(notes)
    if len(found) > 1:
        return found, "多内核归属不同假设，全部列出，未取单一归属"
    return found, f"经内核 {fns[0]} 反查（{notes[0]}）"
