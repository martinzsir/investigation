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
import json
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
    PackSnapshotHistory,
)
from server.app.meta.repo import MetaRepo
from server.app.store import StoreFactory

# 参与快照指纹的声明文件（包"版本"凭据：内容变 → 指纹变）
_FINGERPRINT_FILES = (
    "objects.json", "links.json", "bindings.json", "rules.json",
    "actions.json", "functions.json", "policies.json", "views.json",
    "data_elements.json",  # S0-3 F3.3：Web 可写，不纳入则改了版本纹丝不动
)
# S0-3 F3.3：上游层数据元目录（全域/行业层变了，所有案件版本都应变化）
_FINGERPRINT_LAYER_DIRS = ("_shared", "_industry")


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

    def fingerprint(self, snapshot_dir: Path) -> str:
        """声明指纹（sha1 前 12 位）：快照内容的版本凭据。

        S0-3 F3.3 扩容：data_elements.json + _shared/_industry 上游层目录
        （相对路径 + 内容，排序保证确定性）一并纳入：快照内复制件 +
        建案源 ontology 根双份计入（全域/行业层是跨案件共享口径，源头
        变了所有案件的版本凭据都应变化；快照复制件 Web 可写，改了同样
        必须动版本）。
        """
        h = hashlib.sha1()
        for name in _FINGERPRINT_FILES:
            p = snapshot_dir / name
            if p.exists():
                h.update(name.encode())
                h.update(p.read_bytes())
        # 上游层双份计入（去重同一路径）：快照内复制件（Web 可写，改了
        # 必须动版本）+ 建案源 ontology 根的上游层（全域/行业层是跨案件
        # 共享口径，源头变了所有案件的版本凭据都应变化）。
        for base in dict.fromkeys((snapshot_dir.parent, self.ontology_root)):
            for layer in _FINGERPRINT_LAYER_DIRS:
                layer_dir = base / layer
                if not layer_dir.is_dir():
                    continue
                for f in sorted(layer_dir.rglob("*")):
                    if f.is_file():
                        h.update(f.relative_to(base).as_posix().encode())
                        h.update(f.read_bytes())
        return h.hexdigest()[:12]

    def archive_snapshot(self, case_id: str, version: str) -> Path:
        """完整快照归档（S0-3 R2）：ontology 根整目录拷到
        cases/<cid>/ontology_history/<version>/，同版本幂等跳过。
        版本沿革举证时「改前是什么」= 上一版本的归档。"""
        dest = self.case_dir(case_id) / "ontology_history" / version
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.snapshot_ontology_root(case_id), dest)
        return dest

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
        # 复制 _shared 全域基础层（数据元三层合并需要）
        shared_src = self.ontology_root / "_shared"
        if shared_src.exists():
            shared_dst = self.snapshot_ontology_root(case_id) / "_shared"
            if shared_dst.exists():
                shutil.rmtree(shared_dst)
            shutil.copytree(shared_src, shared_dst)
        # 复制行业叠加层（S0-1）：由模板包 pack_meta.json industry 决定
        meta_path = src / "pack_meta.json"
        if meta_path.exists():
            industry = (json.loads(
                meta_path.read_text(encoding="utf-8")).get("industry"))
            if industry:
                ind_src = self.ontology_root / "_industry" / industry
                if ind_src.exists():
                    ind_dst = (self.snapshot_ontology_root(case_id)
                               / "_industry" / industry)
                    if ind_dst.exists():
                        shutil.rmtree(ind_dst.parent)
                    ind_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(ind_src, ind_dst)
        version = self.fingerprint(dst)

        locked_at = datetime.now().isoformat(timespec="seconds")
        snap = PackSnapshot(case_id=case_id, pack_id=pack_id, version=version,
                            snapshot_path=str(dst), locked_at=locked_at)
        case = CaseRecord(id=case_id, tenant_id=tenant_id, name=name,
                          pack_id=pack_id, pack_snapshot_at=snap.locked_at,
                          created_by=created_by)
        self.repo.create_case(case)
        self.repo.create_pack_snapshot(snap)
        # S0-3 F3.2：建案快照入版本历史（首条，prev_version 空，
        # reason 固定「建案快照」与人工变更理由区分）+ 完整快照归档（R2）
        ref = self.archive_snapshot(case_id, version)
        self.repo.append_pack_snapshot_history(PackSnapshotHistory(
            case_id=case_id, version=version, prev_version="",
            operator=created_by, reason="建案快照",
            changed_files=[], snapshot_ref=str(ref), created_at=locked_at))
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
