"""
core/policy.py
对象级 / 属性级策略执行（REQ-010）。

策略声明在 ontology/<pack>/policies.json（与语义层声明同包同版本管理）：
  - 对象级：roles 名单 + min_clearance 双条件（角色在名单内 且
    clearance >= min_clearance 才放行）；**未声明的对象一律默认拒绝**
    （fail-closed，AC4）——system 角色是唯一旁路；
  - 属性级：default=deny 的敏感列按 allow_roles 判定，无权时按 mask
    策略遮蔽（partial=保前 3 后 4，如 310****1234），否则原文（AC2/AC3）；
  - coverage_missing()：objects.json 每个对象都必须有显式声明（AC5）。

本引擎不做完整 CBAC 分类学（方案劝退清单）；行列两级即最小充分面。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.access import AccessContext, ROLE_RANK


class PolicyDeniedError(PermissionError):
    """对象级策略拒绝（fail-closed 或显式声明拒绝）。"""


class PolicyFileMissing(RuntimeError):
    """policies.json 缺失：按 fail-closed 原则视为全拒，不允许静默放行。"""


def _default_policy_path(pack: str) -> Path:
    return Path(__file__).resolve().parent.parent / "ontology" / pack / "policies.json"


def mask_partial(value) -> str:
    """partial 遮蔽：保前 3 后 4，中段全 *；长度不足 8 全遮。"""
    s = "" if value is None else str(value)
    if len(s) < 8:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


_MASKS = {"partial": mask_partial, "full": lambda v: "***"}


class PolicyEngine:
    """策略引擎。用法：
        pe = PolicyEngine("default")
        pe.check_object(ctx, "tipoff")          # 无权 raise PolicyDeniedError
        rows = pe.apply_row_masks(ctx, "person", rows)
    """

    def __init__(self, pack: str = "default", path: str | Path | None = None):
        self.pack = pack
        p = Path(path) if path else _default_policy_path(pack)
        if not p.exists():
            # fail-closed：声明文件缺失不允许静默放行
            self.object_policies = {}
            self.property_policies = {}
            self._missing_file = True
            return
        self._missing_file = False
        raw = json.loads(p.read_text(encoding="utf-8"))
        # 声明角色集必须与 core.access.ROLE_RANK 同源，防止两处口径漂移
        declared = set(raw.get("roles", ROLE_RANK))
        unknown = declared - set(ROLE_RANK)
        if unknown:
            raise ValueError(f"policies.json 声明了未分级角色 {sorted(unknown)}，"
                             f"与 core.access.ROLE_RANK 不同源")
        self.object_policies = {x["object"]: x for x in raw.get("object_policies", [])}
        self.link_policies = {x["link"]: x for x in raw.get("link_policies", [])}
        self.property_policies = {
            (x["object"], x["property"]): x for x in raw.get("property_policies", [])
        }

    # ---- 对象级 ----
    def check_object(self, ctx: AccessContext, name: str) -> None:
        """对象级判定：不通过 raise PolicyDeniedError（AC1/AC4）。"""
        if ctx.is_system:
            return
        pol = self.object_policies.get(name)
        if pol is None:
            raise PolicyDeniedError(
                f"对象 {name!r} 未声明策略：fail-closed 默认拒绝（REQ-010 AC4）；"
                f"请在 ontology/{self.pack}/policies.json 显式声明")
        if ctx.role in pol.get("roles", []) and ctx.clearance >= pol.get("min_clearance", 0):
            return
        raise PolicyDeniedError(
            f"角色 {ctx.role!r}(clearance={ctx.clearance}) 无权读取对象 {name!r}"
            f"（需 roles={pol.get('roles')} 且 clearance>={pol.get('min_clearance')}）"
            f"——operator={ctx.operator}")

    def check_link(self, ctx: AccessContext, name: str) -> None:
        """链接级判定：同对象级（fail-closed）。"""
        if ctx.is_system:
            return
        pol = self.link_policies.get(name)
        if pol is None:
            raise PolicyDeniedError(
                f"链接 {name!r} 未声明策略：fail-closed 默认拒绝（REQ-010 AC4）；"
                f"请在 ontology/{self.pack}/policies.json link_policies 显式声明")
        if ctx.role in pol.get("roles", []) and ctx.clearance >= pol.get("min_clearance", 0):
            return
        raise PolicyDeniedError(
            f"角色 {ctx.role!r}(clearance={ctx.clearance}) 无权读取链接 {name!r}"
            f"（需 roles={pol.get('roles')} 且 clearance>={pol.get('min_clearance')}）"
            f"——operator={ctx.operator}")

    # ---- 属性级 ----
    def property_rule(self, obj: str, prop: str) -> dict | None:
        return self.property_policies.get((obj, prop))

    def can_read_property(self, ctx: AccessContext, obj: str, prop: str) -> bool:
        """无声明或有权 → True（原文）；有声明无权 → False（将 mask）。"""
        rule = self.property_rule(obj, prop)
        if rule is None or ctx.is_system:
            return True
        return ctx.role in rule.get("allow_roles", [])

    def mask_value(self, ctx: AccessContext, obj: str, prop: str, value):
        if self.can_read_property(ctx, obj, prop):
            return value
        rule = self.property_rule(obj, prop)
        fn = _MASKS.get(rule.get("mask", "full"))
        return fn(value) if fn else "***"

    def apply_row_masks(self, ctx: AccessContext, obj: str, rows: list[dict]) -> list[dict]:
        """按属性策略就地遮蔽行集（AC2）。system 角色原样返回。"""
        if ctx.is_system or not rows:
            return rows
        sensitive = [p for (o, p) in self.property_policies if o == obj]
        if not sensitive:
            return rows
        out = []
        for r in rows:
            r = dict(r)
            for prop in sensitive:
                if prop in r:
                    r[prop] = self.mask_value(ctx, obj, prop, r[prop])
            out.append(r)
        return out

    # ---- 覆盖率（AC5）----
    def coverage_missing(self, object_names: set[str]) -> list[str]:
        """objects.json 里每个对象都必须有显式对象级策略声明。"""
        return sorted(object_names - set(self.object_policies))
