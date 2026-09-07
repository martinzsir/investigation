"""
server/app/cases.py
案件生命周期与 pack 快照锁定（W-005）。

- 建案：校验 pack 存在 → 复制 ontology/<pack> 声明到
  cases/<cid>/ontology/<pack>/（快照，AC4：平台包后续升级不影响在办案件）→
  写 cases 行（待建案）+ case_pack_snapshots 行（声明指纹为包版本凭据）；
- 状态迁移全部经 MetaRepo.transition_case（状态机硬校验，非法迁移硬拒）；
- 快照目录即 Worker BUILD 的 build_ontology(base_dir=...) 根。
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from core.ontology_loader import PACK_ROOT

from server.app.meta.models import (
    CASE_ACTIVE,
    CASE_ARCHIVED,
    CASE_CLOSED,
    CaseRecord,
    PackSnapshot,
)
from server.app.meta.repo import MetaRepo
from server.app.store import StoreFactory

# 参与快照指纹的声明文件（包"版本"凭据：内容变 → 指纹变）
_FINGERPRINT_FILES = (
    "objects.json", "links.json", "bindings.json", "rules.json",
    "actions.json", "functions.json", "policies.json", "views.json",
)


class CaseAlreadyExists(ValueError):
    """案件 ID 已存在。"""


class PackNotFound(FileNotFoundError):
    """建案指定的 pack 不存在。"""


class CaseService:
    def __init__(self, repo: MetaRepo, factory: StoreFactory, *,
                 ontology_root: str | Path | None = None,
                 cases_root: str | Path | None = None):
        self.repo = repo
        self.factory = factory
        self.ontology_root = Path(ontology_root) if ontology_root else PACK_ROOT
        self.cases_root = Path(cases_root) if cases_root else factory.cases_root

    # ---- 路径 ----
    def case_dir(self, case_id: str) -> Path:
        return self.cases_root / case_id

    def snapshot_ontology_root(self, case_id: str) -> Path:
        """案件快照 ontology 根（build_ontology 的 base_dir）。"""
        return self.case_dir(case_id) / "ontology"

    def snapshot_dir(self, case_id: str, pack_id: str) -> Path:
        return self.snapshot_ontology_root(case_id) / pack_id

    @staticmethod
    def fingerprint(snapshot_dir: Path) -> str:
        """声明指纹（sha1 前 12 位）：快照内容的版本凭据。"""
        h = hashlib.sha1()
        for name in _FINGERPRINT_FILES:
            p = snapshot_dir / name
            if p.exists():
                h.update(name.encode())
                h.update(p.read_bytes())
        return h.hexdigest()[:12]

    # ---- 生命周期 ----
    def create_case(self, *, case_id: str, name: str, tenant_id: str = "default",
                    pack_id: str = "default", created_by: str = "") -> CaseRecord:
        if self.repo.get_case(case_id) is not None:
            raise CaseAlreadyExists(f"案件已存在：{case_id}")
        src = self.ontology_root / pack_id
        if not (src / "objects.json").exists():
            raise PackNotFound(f"ontology 案件包不存在：{src}")

        dst = self.snapshot_dir(case_id, pack_id)
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        version = self.fingerprint(dst)

        locked_at = datetime.now().isoformat(timespec="seconds")
        snap = PackSnapshot(case_id=case_id, pack_id=pack_id, version=version,
                            snapshot_path=str(dst), locked_at=locked_at)
        case = CaseRecord(id=case_id, tenant_id=tenant_id, name=name,
                          pack_id=pack_id, pack_snapshot_at=snap.locked_at,
                          created_by=created_by)
        self.repo.create_case(case)
        self.repo.create_pack_snapshot(snap)
        got = self.repo.get_case(case_id)
        assert got is not None
        return got

    def get_case(self, case_id: str) -> CaseRecord | None:
        return self.repo.get_case(case_id)

    def list_cases(self, tenant_id: str | None = None) -> list[CaseRecord]:
        return self.repo.list_cases(tenant_id)

    def activate_case(self, case_id: str, by: str) -> CaseRecord:
        return self.repo.transition_case(case_id, CASE_ACTIVE, by)

    def close_case(self, case_id: str, by: str) -> CaseRecord:
        return self.repo.transition_case(case_id, CASE_CLOSED, by)

    def archive_case(self, case_id: str, by: str) -> CaseRecord:
        return self.repo.transition_case(case_id, CASE_ARCHIVED, by)
