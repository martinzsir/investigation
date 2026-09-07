"""
tests/test_case_snapshot.py
M1 阶段 C：案件生命周期 + pack 快照锁定（W-005）。

验收点：
  - 建案落 cases 行（待建案）+ 快照目录 + case_pack_snapshots 行（指纹）；
  - 快照可经 load_pack(base_dir=快照根) 装载（loader 校验通过）；
  - AC4 快照隔离：建案后源包被修改/升级，快照内容不变；
  - 重复建案拒绝、不存在 pack 拒绝；
  - 激活迁移合法，越级/回退由状态机硬拒（非法迁移详细用例在 test_meta_store）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology_loader import load_pack

from server.app.cases import CaseAlreadyExists, CaseService, PackNotFound
from server.app.meta.models import (
    CASE_ACTIVE,
    CASE_DRAFT,
    IllegalTransition,
)
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory


class TestCaseLifecycleAndSnapshot(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # 用临时 ontology 根（复制 default），便于验证"源包升级不影响快照"
        self.onto_root = self.tmp / "ontology"
        shutil.copytree(ROOT / "ontology" / "default",
                        self.onto_root / "default")
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases")
        self.svc = CaseService(self.repo, self.factory,
                               ontology_root=self.onto_root,
                               cases_root=self.tmp / "cases")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_case_locks_snapshot(self):
        case = self.svc.create_case(case_id="c2026_001", name="海州专案",
                                    tenant_id="t1", created_by="王检")
        self.assertEqual(case.status, CASE_DRAFT)
        self.assertEqual(case.pack_id, "default")
        self.assertTrue(case.pack_snapshot_at)
        # 快照目录与文件
        snap = self.svc.snapshot_dir("c2026_001", "default")
        self.assertTrue((snap / "objects.json").exists())
        self.assertTrue((snap / "bindings.json").exists())
        # 快照可被 loader 校验装载
        spec = load_pack("default",
                         base_dir=self.svc.snapshot_ontology_root("c2026_001"))
        self.assertEqual(spec.name, "default")
        self.assertGreater(len(spec.objects), 0)
        # 快照登记行（指纹为版本凭据）
        row = self.repo.get_pack_snapshot("c2026_001")
        self.assertEqual(row.pack_id, "default")
        self.assertEqual(len(row.version), 12)
        self.assertTrue(row.locked_at)

    def test_snapshot_isolated_from_source_upgrade(self):
        """AC4：建案后源包改动，快照内容不变。"""
        self.svc.create_case(case_id="c1", name="案", created_by="u")
        snap_dir = self.svc.snapshot_dir("c1", "default")
        fp_before = self.svc.fingerprint(snap_dir)

        # 源包"升级"：改写 bindings.json
        src_bindings = self.onto_root / "default" / "bindings.json"
        data = json.loads(src_bindings.read_text(encoding="utf-8"))
        data["_upgrade_marker"] = "post-case-change"
        src_bindings.write_text(json.dumps(data, ensure_ascii=False),
                               encoding="utf-8")

        # 快照指纹不变，且快照文件不含升级标记
        self.assertEqual(self.svc.fingerprint(snap_dir), fp_before)
        snap_data = json.loads(
            (snap_dir / "bindings.json").read_text(encoding="utf-8"))
        self.assertNotIn("_upgrade_marker", snap_data)
        # 新建案件会锁定新版本
        self.svc.create_case(case_id="c2", name="案二", created_by="u")
        self.assertNotEqual(
            self.svc.fingerprint(self.svc.snapshot_dir("c2", "default")),
            fp_before)

    def test_duplicate_and_missing_pack(self):
        self.svc.create_case(case_id="c1", name="案", created_by="u")
        with self.assertRaises(CaseAlreadyExists):
            self.svc.create_case(case_id="c1", name="重名", created_by="u")
        with self.assertRaises(PackNotFound):
            self.svc.create_case(case_id="c3", name="案", pack_id="ghost_pack",
                                 created_by="u")

    def test_activate_and_state_guard(self):
        self.svc.create_case(case_id="c1", name="案", created_by="u")
        active = self.svc.activate_case("c1", by="王检")
        self.assertEqual(active.status, CASE_ACTIVE)
        # 越级结案（侦查中不可直接封存到已结案？侦查中→已结案合法；
        # 待建案→已结案非法）——新建 c2 验证越级拒绝
        self.svc.create_case(case_id="c2", name="案二", created_by="u")
        with self.assertRaises(IllegalTransition):
            self.svc.close_case("c2", by="u")

    def test_tenant_listing(self):
        self.svc.create_case(case_id="c1", name="甲", tenant_id="t1",
                             created_by="u")
        self.svc.create_case(case_id="c2", name="乙", tenant_id="t2",
                             created_by="u")
        self.assertEqual([c.id for c in self.svc.list_cases("t1")], ["c1"])
        self.assertEqual(
            sorted(c.id for c in self.svc.list_cases()), ["c1", "c2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
