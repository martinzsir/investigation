"""
core/pack.py
REQ-044 多案件包与隔离。

设计：
  - PackManager 管理多个案件包的生命周期（注册/列表/切换）；
  - 每个包有独立的 DuckDB 文件（investigation_<pack>.duckdb）；
  - 包间默认隔离：跨包查询被拒；
  - 新包可从模板初始化（从 default 复制 ontology 声明）；
  - 跨包操作需显式授权（authorized_packs）并记录审计。

隔离边界（与三条禁令一致）：
  - 不提供跨包 JOIN；
  - Gateway 绑定单个 pack，只查当前包的 obj_*/lnk_*；
  - 切换案件包不影响其他包的版本与审计。
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path
from typing import Any

from core.access import AccessContext
from core.audit import AuditChain
from core.ontology_loader import PACK_ROOT, load_pack


class PackIsolationError(PermissionError):
    """跨包操作未授权或被拒。"""


class PackNotFoundError(FileNotFoundError):
    """案件包不存在。"""


# init_pack 写入新案件包 bindings.json 的占位 SQL——loader 视为合法
# source_sql 声明，build_ontology 时若用户未替换则按 optional/硬失败规则处理
_INIT_PACK_PLACEHOLDER_SQL = (
    "-- REQ-044 init_pack 占位：请改写为指向新案件数据源的 SELECT 语句"
)


class PackManager:
    """多案件包管理器：注册/列表/隔离检查/跨包授权。

    用法：
        pm = PackManager(base_dir=ontology_root)
        pm.list_packs()  # → ["default", "case_2024_001", ...]
        pm.init_pack("case_2024_001", from_pack="default")
        pm.assert_authorized(ctx, "case_2024_001")
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or PACK_ROOT

    def list_packs(self) -> list[str]:
        """列出所有已注册案件包（目录名）。"""
        if not self.base_dir.exists():
            return []
        return sorted(
            d.name for d in self.base_dir.iterdir()
            if d.is_dir() and (d / "objects.json").exists()
        )

    def pack_exists(self, pack: str) -> bool:
        """检查案件包是否存在。"""
        return (self.base_dir / pack / "objects.json").exists()

    def init_pack(self, pack_name: str, *, from_pack: str = "default") -> str:
        """AC2: 从模板初始化新案件包。

        复制 ontology/<from_pack>/ 为 ontology/<pack_name>/，然后把每个
        object_binding 的结构化 `source`（{table, columns}）替换为占位
        `source_sql`——保留声明结构、移除指向旧案件数据源的列映射，
        等待新案件填入。loader 视占位 source_sql 为合法声明（占位 SQL
        在 build_ontology 阶段会因表不存在而被 optional 跳过或硬失败，
        调用方需在 build 前改写 bindings.json 为新案件实际数据源）。
        """
        src = self.base_dir / from_pack
        if not src.exists():
            raise PackNotFoundError(f"模板包不存在：{src}")
        dst = self.base_dir / pack_name
        if dst.exists():
            raise ValueError(f"案件包已存在：{pack_name}")
        shutil.copytree(src, dst)
        # 结构化 source 替换为占位 source_sql（待新案件填入数据源）
        bindings_path = dst / "bindings.json"
        if bindings_path.exists():
            data = json.loads(bindings_path.read_text(encoding="utf-8"))
            for b in data.get("object_bindings", []):
                if "source" in b:
                    b.pop("source")                    # 移除旧结构化源
                    b.pop("source_table", None)         # 清旧 table 溯源
                    # 占位 SQL：loader 视为已声明 source_sql，build_ontology
                    # 时会被 optional 跳过或硬失败提示，提示用户填入真实数据源
                    b.setdefault("source_sql", _INIT_PACK_PLACEHOLDER_SQL)
            bindings_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        return pack_name

    def assert_authorized(self, ctx: AccessContext, pack: str) -> None:
        """AC1/AC5: 跨包操作授权检查。

        - ctx.case_id == pack：同包操作，放行；
        - ctx.is_system：system 角色旁路；
        - 其他：需 ctx.case_id 显式授权跨包 → 记录审计。
        """
        if ctx.case_id == pack:
            return
        if ctx.is_system:
            return
        # 跨包：case_id 必须以 "+<pack>" 形式声明跨包授权
        authorized = ctx.case_id
        # case_id 可以是 "default+case_001" 形式
        if f"+{pack}" in authorized:
            return
        raise PackIsolationError(
            f"跨包操作未授权：ctx.case_id={ctx.case_id!r} 试图访问 pack={pack!r}"
            f"（REQ-044 AC1/AC5：包间默认隔离，跨包需显式授权）")

    def cross_pack_audit(self, conn, ctx: AccessContext,
                         target_pack: str, action: str) -> str:
        """AC5: 跨包操作记录审计。"""
        chain = AuditChain(conn)
        event_id = chain.append(
            operator=ctx.operator,
            before={"pack": ctx.case_id},
            after={"pack": target_pack, "action": action},
            source_row_ids=[target_pack],
            ontology_version=chain.current_ontology_version(),
        )
        return event_id

    def load(self, pack: str):
        """加载指定案件包的 ontology 声明。"""
        if not self.pack_exists(pack):
            raise PackNotFoundError(f"案件包不存在：{pack}")
        return load_pack(pack, base_dir=self.base_dir)


def db_path_for_pack(pack: str, root: str | Path = ".") -> str:
    """获取案件包对应的 DuckDB 文件路径。"""
    return str(Path(root) / f"investigation_{pack}.duckdb")


def list_all_packs(base_dir: Path | None = None) -> list[str]:
    """便捷函数：列出所有案件包。"""
    return PackManager(base_dir).list_packs()
