"""Function 归属视图：注册期就能看出每个 Function 被谁用了。

为什么需要这个
--------------
规则与 Function 是一体两面：规则是人读的判定层（rule_text / basis_text /
assumption / hit_when / params），Function 是机器跑的执行层（inputs / impl /
parameters）。**规则依赖 Function 是硬性的**——run_rules 只有一条执行通道
`fx.invoke(r.function, params)`。

反过来**不成立**：Function 不必然有规则。实测 17 个声明 Function，7 个被
规则引用、6 个被镜头 impl 调用、4 个谁都没用。

直接调用 Function 在技术逻辑上没问题（只读、确定性、可复现），但**绕过
规则层就丢失了判定语义**：没有 assumption、没有 basis_text、没有 hit_when。
同一份 `time_window_collision`，经 R6 跑出来是命题（带 H4、带判据、可证伪），
被奇正直调跑出来只是一堆行——这正是此前"奇正分工方案假设链为空"的根因。

所以需要的不是禁止直调，而是**让归属可见**：注册/装载期就能看出每个
Function 是"经规则判定"、"经镜头研判"还是"尚未接线"。

归属来源（按优先级）
--------------------
1. rules.json 的 `function` 字段 —— 声明式，带 rule_id / assumption / stage
2. packs/*/pack.json 的 `uses_functions` —— 镜头声明式（可选，推荐）
3. packs/*/impl.py 静态扫描 `_invoke(store, "<name>"` —— 兜底，免改动
4. 都没有 —— orphan（未接线）

只有 4 是问题：声明了 Function 却无人使用，会让人以为它能跑。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

STATUS_RULE = "rule"      # 经规则：产出命题（带假设/判据/命中条件）
STATUS_LENS = "lens"      # 经镜头：产出观察（研判手段，无判定层）
STATUS_SKILL = "skill"    # 被内置技能直调：有业务目标，但无判定层
STATUS_ORPHAN = "orphan"  # 未接线：声明了但无人引用

_STATUS_LABELS = {
    STATUS_RULE: "规则",
    STATUS_LENS: "镜头",
    STATUS_SKILL: "技能",
    STATUS_ORPHAN: "未接线",
}

_STATUS_ORDER = (STATUS_RULE, STATUS_LENS, STATUS_SKILL, STATUS_ORPHAN)

# impl.py 里 `FunctionExecutor(...).invoke("xxx", ...)` / `_invoke(store, "xxx", ...)`
_INVOKE_RE = re.compile(r"""_?invoke\(\s*(?:store\s*,\s*)?["']([a-z_][a-z0-9_]*)["']""")

# skills/*.py 里 `invoke_function(store, "xxx")`
_SKILL_INVOKE_RE = re.compile(
    r"""invoke_function\(\s*(?:store\s*,\s*)?["']([a-z_][a-z0-9_]*)["']""")


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _packs_root(base_dir: Path | None) -> Path:
    """镜头包根目录（packs/）；base_dir 是 ontology 根，packs 是其兄弟。"""
    if base_dir is not None:
        p = Path(base_dir)
        return p.parent / "packs" if p.name == "ontology" else p / "packs"
    return Path(__file__).resolve().parents[1] / "packs"


def _scan_impl_functions(packs_root: Path) -> dict[str, list[str]]:
    """静态扫描 packs/*/impl.py → {function_name: [pack_id, ...]}。

    兜底路径，免改动即可用：镜头包即使没在 pack.json 声明 uses_functions，
    只要 impl.py 里调了 `invoke("xxx")` 就能识别出来。
    """
    out: dict[str, list[str]] = {}
    if not packs_root.is_dir():
        return out
    for impl in sorted(packs_root.glob("*/impl.py")):
        try:
            text = impl.read_text(encoding="utf-8")
        except OSError:
            continue
        pack_id = impl.parent.name
        for fn in sorted(set(_INVOKE_RE.findall(text))):
            out.setdefault(fn, [])
            if pack_id not in out[fn]:
                out[fn].append(pack_id)
    return out


def _scan_skill_functions(root: Path) -> dict[str, list[str]]:
    """扫描 skills/*.py → {function_name: [skill_id, ...]}。

    内置技能（奇正/用间等）直接调 Function，不经过 rules.json 也不属于
    镜头包。它们**有业务目标**（五间交叉判定、Q1 碰撞），与"未接线"不同，
    但同样没有判定层——故此单列一类，不与规则混同。

    例：yong_jian 调 jian_cross_level、qi_zheng 调 time_window_collision。
    """
    out: dict[str, list[str]] = {}
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return out
    for f in sorted(skills_dir.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        skill_id = f.stem
        for fn in sorted(set(_SKILL_INVOKE_RE.findall(text))):
            out.setdefault(fn, [])
            if skill_id not in out[fn]:
                out[fn].append(skill_id)
    return out


def _declared_lens_functions(packs_root: Path) -> dict[str, list[tuple[str, str]]]:
    """packs/*/pack.json 的 uses_functions → {function: [(pack_id, skill_id)]}。

    声明式优先：镜头显式说出自己用哪些 Function，可装载期校验（引用的
    Function 必须已声明），比扫描 impl.py 更可靠。
    """
    import json

    out: dict[str, list[tuple[str, str]]] = {}
    if not packs_root.is_dir():
        return out
    for pj in sorted(packs_root.glob("*/pack.json")):
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pack_id = data.get("pack_id") or pj.parent.name
        for sk in data.get("skills") or []:
            sid = str(sk.get("skill_id") or "")
            for fn in sk.get("uses_functions") or []:
                out.setdefault(str(fn), [])
                if (pack_id, sid) not in out[fn]:
                    out[fn].append((pack_id, sid))
    return out


def scan_function_bindings(pack: str = "default",
                           base_dir: Path | None = None) -> dict[str, dict]:
    """扫描全部已声明 Function 的归属 → {function_name: 归属信息}。

    返回每项：
      status      rule / lens / orphan
      status_label 中文标签
      bound_by   [{"kind": "rule"/"lens", ...}] 引用者列表
      note       说明（orphan 时说明"未接线"）
    """
    from core.ontology_loader import load_pack

    try:
        spec = load_pack(pack, base_dir=base_dir)
    except Exception:
        return {}
    funcs = dict(spec.functions or {})

    # 1) 规则引用（声明式，带判定语义）
    rules_by_fn: dict[str, list[dict]] = {}
    for r in (spec.rules or {}).values():
        fn = str(getattr(r, "function", "") or "").strip()
        if not fn:
            continue
        rules_by_fn.setdefault(fn, []).append({
            "kind": "rule",
            "rule_id": str(getattr(r, "id", "")),
            "title": str(getattr(r, "title", "") or ""),
            "stage": str(getattr(r, "stage", "") or ""),
            # 有 assumption 才算"能产出命题"；空则规则没说要验证什么
            "assumption": str(getattr(r, "assumption", "") or ""),
            "has_verdict": bool(getattr(r, "assumption", "")),
        })

    # 2) 镜头声明式 + 3) impl.py 兜底
    packs_root = _packs_root(base_dir)
    lens_declared = _declared_lens_functions(packs_root)
    impl_scanned = _scan_impl_functions(packs_root)
    project_root = Path(__file__).resolve().parents[1]
    skill_scanned = _scan_skill_functions(project_root)

    out: dict[str, dict] = {}
    for name, fspec in funcs.items():
        bound: list[dict] = []
        for r in rules_by_fn.get(name, []):
            bound.append(r)
        for pack_id, sid in lens_declared.get(name, []):
            bound.append({"kind": "lens", "pack_id": pack_id,
                          "skill_id": sid, "declared": True})
        for pack_id in impl_scanned.get(name, []):
            # 已由 pack.json 声明过的不再重复记（声明式优先）
            if any(b.get("kind") == "lens" and b.get("pack_id") == pack_id
                   for b in bound):
                continue
            bound.append({"kind": "lens", "pack_id": pack_id,
                          "skill_id": "", "declared": False})
        for sid in skill_scanned.get(name, []):
            bound.append({"kind": "skill", "skill_id": sid})

        has_rule = any(b["kind"] == "rule" for b in bound)
        has_lens = any(b["kind"] == "lens" for b in bound)
        has_skill = any(b["kind"] == "skill" for b in bound)
        if has_rule:
            status = STATUS_RULE
        elif has_lens:
            status = STATUS_LENS
        elif has_skill:
            status = STATUS_SKILL
        else:
            status = STATUS_ORPHAN

        note = ""
        if status == STATUS_ORPHAN:
            note = "已声明但无人引用：既不挂规则也不挂镜头，直接调用无判定语义"
        elif status == STATUS_RULE:
            if any(b.get("kind") == "rule" and not b.get("has_verdict")
                   for b in bound):
                note = "规则未声明 assumption：产出无假设可证伪"
        elif status == STATUS_LENS:
            note = "镜头调用：产出为观察（无判定层），需人工提升才成为命题"
        elif status == STATUS_SKILL:
            note = ("内置技能直调：有业务目标但无规则判定层——产出需自建"
                    "判定口径（如五间按交叉等级分流）")

        out[name] = {
            "name": name,
            "title": str(getattr(fspec, "title", "") or ""),
            "impl": str(getattr(fspec, "impl", "") or ""),
            "output_type": str(getattr(fspec, "output_type", "") or ""),
            "status": status,
            "status_label": status_label(status),
            "bound_by": bound,
            "note": note,
        }
    return out


def summarize(bindings: dict[str, dict]) -> dict[str, Any]:
    """汇总：各状态计数 + 问题清单（供面板/诊断直接消费）。"""
    by_status: dict[str, list[str]] = {}
    for name, b in bindings.items():
        by_status.setdefault(b["status"], []).append(name)
    return {
        "total": len(bindings),
        "counts": {k: len(v) for k, v in sorted(by_status.items())},
        "orphans": sorted(by_status.get(STATUS_ORPHAN, [])),
        "rules_without_verdict": sorted(
            n for n, b in bindings.items()
            if b["status"] == STATUS_RULE
            and any(x.get("kind") == "rule" and not x.get("has_verdict")
                    for x in b["bound_by"])),
    }
