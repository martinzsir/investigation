"""案件级「知己」配置（庙算沙盘强制输入：证据缺口 + 授权边界）。

为什么单独一个文件
------------------
MiaoSuan.set_ji() 强制非空——知己栏是庙算的前提：授权边界决定哪些假设
能查（受限标记），证据缺口决定哪些假设只能降级。Web 侧此前**完全没有**
知己输入，于是庙算实例根本建不起来（`build()` 会抛"请先调用 set_ji()"），
Web detect 只能传 miao=None，派生视图也只能从线索产物反推。

配置落案件快照根（cases/<cid>/miao_ji.json，与 hypotheses.json 同级），
不进本体指纹——改知己 ≠ 改本体版本。

未配置时的派生口径
------------------
正兵未填写时**不阻塞 BUILD**：按案件实际数据派生，并标注 `derived=True`
让 UI 明确"这是系统猜的，不是人填的"：
  - 证据缺口 ← 语义表零行对应的数据源（真实可算）
  - 授权边界 ← 无输入可推，写"未声明"而不是编一条——编造授权边界会让
    假设被错误标记受限/放行，比空着更危险。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

JI_FILENAME = "miao_ji.json"
SCHEMA_VERSION = 1
MAX_ITEMS = 50
ITEM_MAX = 200

_UNDECLARED = "未声明（正兵未填写授权边界，假设不做受限标记）"


def ji_path(case_dir: str | Path) -> Path:
    return Path(case_dir) / JI_FILENAME


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _clean(items: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for x in items[:MAX_ITEMS]:
        s = str(x).strip()
        if s:
            out.append(s[:ITEM_MAX])
    return out


def load_ji(case_dir: str | Path) -> dict[str, Any]:
    """读知己配置；文件缺失/损坏 → {"configured": False, ...}（不抛）。"""
    p = ji_path(case_dir)
    if not p.exists():
        return {"schema_version": SCHEMA_VERSION, "configured": False,
                "gaps": [], "auth_boundary": [], "audit": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "configured": False,
                "gaps": [], "auth_boundary": [], "audit": []}
    if not isinstance(d, dict):
        return {"schema_version": SCHEMA_VERSION, "configured": False,
                "gaps": [], "auth_boundary": [], "audit": []}
    gaps = _clean(d.get("gaps"))
    auth = _clean(d.get("auth_boundary"))
    return {
        "schema_version": SCHEMA_VERSION,
        "configured": bool(gaps) and bool(auth),
        "gaps": gaps,
        "auth_boundary": auth,
        "audit": d.get("audit") if isinstance(d.get("audit"), list) else [],
    }


def save_ji(case_dir: str | Path, *, gaps: list[str],
            auth_boundary: list[str], operator: str = "",
            prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """写知己配置（全量覆盖 + 审计条目）。"""
    prev = prev if isinstance(prev, dict) else load_ji(case_dir)
    audit = list(prev.get("audit") or [])
    audit.append({"action": "save", "operator": operator, "ts": _now()})
    data = {
        "schema_version": SCHEMA_VERSION,
        "gaps": _clean(gaps),
        "auth_boundary": _clean(auth_boundary),
        "audit": audit[-200:],
    }
    _atomic_write(ji_path(case_dir), data)
    return {**data, "configured": bool(data["gaps"]) and bool(data["auth_boundary"])}


# ----------------------------------------------------------------------
# 派生口径（未配置时）：证据缺口从真实数据算，授权边界不编
# ----------------------------------------------------------------------

def _source_names(pack: str) -> dict[str, str]:
    """对象/链接类型 → 数据源展示名（五间包声明）。

    load_wujian 读的是**已挂载**注册表，未 discover() 时返回 None →
    调用方回落类型名（英文），而假设的 data_sources 是中文展示名，
    两者匹配不上会把全部假设误标降级。故此处先确保挂载。
    """
    try:
        from core.wujian import load_wujian, offered_packs
        if pack not in offered_packs():
            from core.wujian import build_wujian, register_wujian
            from pathlib import Path
            wp = Path(__file__).resolve().parents[2] / "packs" / "wujian"
            if wp.is_dir():
                register_wujian(build_wujian(wp, pack))
        wj = load_wujian(pack)
        return (wj.source_names if wj is not None else {}) or {}
    except Exception:
        return {}


def derive_gaps(conn, pack: str = "default", base_dir=None) -> list[str]:
    """语义表零行 → 数据源展示名（真实可算的证据缺口）。

    判据与庙算实证缺口定位同源：表不存在或 COUNT=0 即未接入。
    """
    try:
        from core.ontology_loader import load_pack
        names = _source_names(pack)
        spec = load_pack(pack, base_dir=base_dir)
        spec = load_pack(pack, base_dir=base_dir)
        objs = spec.objects or []
    except Exception:
        return []
    gaps: list[str] = []
    for o in objs:
        t = getattr(o, "name", "") if not isinstance(o, str) else o
        if not t:
            continue
        tbl = f"obj_{t}"
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            n = 0
        if not n:
            gaps.append(names.get(t, t))
    return _clean(gaps)


def derive_available(conn, pack: str = "default",
                     base_dir=None) -> tuple[list[str], list[str]]:
    """已接入/未接入数据源名（供 MiaoSuan.build 打降级标记）。

    与 derive_gaps 同源同判据：语义表有行=已接入，零行/不存在=未接入。
    返回 (available, unavailable)——两者并用才能给假设打"降级"。
    """
    try:
        from core.ontology_loader import load_pack
        names = _source_names(pack)
        spec = load_pack(pack, base_dir=base_dir)
        objs = spec.objects or []
    except Exception:
        return [], []
    avail: list[str] = []
    unavail: list[str] = []
    for o in objs:
        t = getattr(o, "name", "") if not isinstance(o, str) else o
        if not t:
            continue
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM obj_{t}").fetchone()[0]
        except Exception:
            n = 0
        label = names.get(t, t)
        (avail if n else unavail).append(label)
    return avail, unavail


def build_miaosuan(*, case_dir: Path, conn, pack: str = "default",
                   base_dir=None, findings: list[dict] | None = None,
                   manual: dict | None = None) -> tuple[Any, dict]:
    """Web 侧庙算实例（此前 miao=None，庙算在 Web 侧形同不存在）。

    三步：知己 → 数据驱动自动假设 → 人工假设合并 → 规则约束打标。

    返回 (MiaoSuan 实例, 元信息)。元信息含 derived_ji 标记——UI 需明示
    "知己是系统派生的，不是正兵填的"，否则正兵会以为授权边界已声明。
    """
    from core.hypotheses import MiaoSuan, Hypothesis

    gaps, auth, configured = resolve_ji(case_dir, conn, pack, base_dir)
    avail, unavail = derive_available(conn, pack, base_dir)
    # 受限判定看**授权边界**，降级判定看**数据未接入**——两者都要传：
    # build() 用 unavailable 对 evidence_needed 做子串匹配打「受限」标记，
    # 只传 unavail 会让超授权假设漏标（实测 HM1 需房产、边界含"不可查房产
    # 车辆"，却仍显示待推演）。未声明时边界为占位串，不匹配任何证据。
    unavailable = list(unavail) + list(auth)

    miao = MiaoSuan(pack, base_dir)
    miao.set_ji(gaps=gaps, auth_boundary=auth)
    auto = miao.auto_from_findings(list(findings or []))

    # 人工假设（正兵判断，跨版本持久，不随重扫蒸发）
    manual_items = list((manual or {}).get("items") or [])
    added_manual: list[str] = []
    for it in manual_items:
        if not isinstance(it, dict):
            continue
        try:
            miao.add(Hypothesis(**{
                k: v for k, v in it.items()
                if k in Hypothesis.__dataclass_fields__ and k != "source_rows"}))
            added_manual.append(str(it.get("id") or ""))
        except Exception:
            continue  # 单条非法只跳过该条，不让配置问题拖垮 BUILD

    miao.build(avail, unavailable)
    try:
        miao.enumerate_space()
    except Exception:
        pass

    return miao, {
        "ji_configured": configured,
        "ji_gaps": gaps,
        "ji_auth_boundary": auth,
        "available": avail,
        "unavailable": unavailable,
        "auto_ids": [h.id for h in auto],
        "manual_ids": [x for x in added_manual if x],
    }


def resolve_ji(case_dir: str | Path, conn=None, pack: str = "default",
               base_dir=None) -> tuple[list[str], list[str], bool]:
    """返回 (证据缺口, 授权边界, 是否人工配置)。

    人工配置优先；未配置时按数据派生，且**授权边界不编造**（写"未声明"，
    这样 _matches 不会误命中任何 evidence_needed，假设不会被错误标受限）。
    两者都必须非空——set_ji 的强制要求。
    """
    cfg = load_ji(case_dir)
    if cfg.get("configured"):
        return cfg["gaps"], cfg["auth_boundary"], True

    gaps = derive_gaps(conn, pack, base_dir) if conn is not None else []
    if not gaps:
        gaps = ["无显著缺口（按已接入数据源自动派生）"]
    return gaps, [_UNDECLARED], False
