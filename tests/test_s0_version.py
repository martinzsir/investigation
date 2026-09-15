"""
tests/test_s0_version.py
S0-3/F3 版本机制修复：指纹扩容 + 编辑后重算 + 版本历史只追加。

验收点：
  - 指纹清单扩容（F3.3）：data_elements.json + _shared/_industry 目录内容
    纳入指纹（改了数据元版本号必须动）；
  - 建案即入历史（首条 prev_version 空，reason=建案快照，PRD §14 待确认#4）；
  - commit_ontology_version（F3.1/F3.2）：重算指纹 → 更新快照版本 →
    追加历史（只追加不覆盖，R2）+ 完整快照归档（snapshot_ref 可取回）；
  - 指纹未变不产生历史行；
  - 连做三次编辑 → 建案 + 三条编辑记录，prev 链完整（S0-3 验收）；
  - E3-1：归档失败保留上一版本 + ops 显式留痕，不静默。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.cases import CaseService
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.snapshot_config import commit_ontology_version
from server.app.store import StoreFactory


class S0VersionTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.onto_root = self.tmp / "ontology"
        shutil.copytree(ROOT / "ontology" / "default",
                        self.onto_root / "default")
        shared_src = ROOT / "ontology" / "_shared"
        if shared_src.is_dir():
            shutil.copytree(shared_src, self.onto_root / "_shared")
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases")
        self.svc = CaseService(self.repo, self.factory,
                               ontology_root=self.onto_root,
                               cases_root=self.tmp / "cases")
        self.svc.create_case(case_id="c1", name="案", created_by="建案人")
        self.snap_dir = self.svc.snapshot_dir("c1", "default")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rewrite(self, filename: str, marker: str) -> None:
        """改快照声明文件（指纹只看内容，不跑 loader，无需保语义）。"""
        p = self.snap_dir / filename
        data = json.loads(p.read_text(encoding="utf-8"))
        data["_s0_marker"] = marker
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")


class TestFingerprintExpansion(S0VersionTestBase):

    def test_baseline_12hex(self):
        fp = self.svc.fingerprint(self.snap_dir)
        self.assertEqual(len(fp), 12)

    def test_data_elements_json_included(self):
        """F3.3：data_elements.json Web 可写，必须参与版本凭据。"""
        fp_before = self.svc.fingerprint(self.snap_dir)
        self._rewrite("data_elements.json", "de-edit")
        self.assertNotEqual(self.svc.fingerprint(self.snap_dir), fp_before)

    def test_shared_layer_included(self):
        """F3.3：全域层数据元改了，所有案件版本都应变化。"""
        fp_before = self.svc.fingerprint(self.snap_dir)
        shared = self.onto_root / "_shared" / "data_elements.json"
        data = json.loads(shared.read_text(encoding="utf-8"))
        data["_s0_marker"] = "shared-edit"
        shared.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        self.assertNotEqual(self.svc.fingerprint(self.snap_dir), fp_before)

    def test_industry_layer_included(self):
        """F3.3：行业层内容参与指纹（F1 建层后）。"""
        ind = self.onto_root / "_industry" / "金融"
        ind.mkdir(parents=True)
        (ind / "data_elements.json").write_text(
            json.dumps({"schema_version": 2,
                        "elements": {"DE_FIN_ACCT_NO": {
                            "name": "银行账号", "type": "string"}}},
                       ensure_ascii=False), encoding="utf-8")
        fp_with = self.svc.fingerprint(self.snap_dir)
        # 行业层内容变化 → 指纹变化
        (ind / "data_elements.json").write_text(
            json.dumps({"schema_version": 2,
                        "elements": {"DE_FIN_ACCT_NO": {
                            "name": "银行账号(修订)", "type": "string"}}},
                       ensure_ascii=False), encoding="utf-8")
        self.assertNotEqual(self.svc.fingerprint(self.snap_dir), fp_with)


class TestVersionHistory(S0VersionTestBase):

    def test_initial_history_row_on_create(self):
        """建案快照入历史（首条）：prev 空 + reason=建案快照 + 归档可取回。"""
        rows = self.repo.list_pack_snapshot_history("c1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.version,
                         self.repo.get_pack_snapshot("c1").version)
        self.assertEqual(row.prev_version, "")
        self.assertEqual(row.reason, "建案快照")
        self.assertEqual(row.operator, "建案人")
        # R2：snapshot_ref 指向完整快照归档（含 objects.json 等整包）
        ref = Path(row.snapshot_ref)
        self.assertTrue((ref / "default" / "objects.json").exists())
        self.assertTrue((ref / "_shared" / "data_elements.json").exists())

    def test_commit_appends_history_and_updates_version(self):
        fp_before = self.repo.get_pack_snapshot("c1").version
        self._rewrite("objects.json", "edit-1")
        new_fp = commit_ontology_version(
            repo=self.repo, cases=self.svc, case_id="c1",
            snap_dir=self.snap_dir, op="model_objects_save",
            operator="张偏将", reason="新增对象类型",
            changed_files=["objects.json"])
        self.assertNotEqual(new_fp, fp_before)
        self.assertEqual(len(new_fp), 12)
        # 当前版本指针更新（F3.1）
        self.assertEqual(self.repo.get_pack_snapshot("c1").version, new_fp)
        # 历史只追加（F3.2）：建案 + 编辑，prev 链完整
        rows = self.repo.list_pack_snapshot_history("c1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].version, new_fp)
        self.assertEqual(rows[1].prev_version, fp_before)
        self.assertEqual(rows[1].operator, "张偏将")
        self.assertEqual(rows[1].reason, "新增对象类型")
        self.assertEqual(rows[1].changed_files, ["objects.json"])
        self.assertTrue(Path(rows[1].snapshot_ref,
                             "default", "objects.json").exists())

    def test_commit_noop_when_fingerprint_unchanged(self):
        fp = commit_ontology_version(
            repo=self.repo, cases=self.svc, case_id="c1",
            snap_dir=self.snap_dir, op="knowledge_save",
            operator="u", reason="无凭据文件变更")
        self.assertEqual(fp, self.repo.get_pack_snapshot("c1").version)
        rows = self.repo.list_pack_snapshot_history("c1")
        self.assertEqual(len(rows), 1)  # 只有建案首条
        # 再提交一次同样无变化 → 仍不追加
        commit_ontology_version(
            repo=self.repo, cases=self.svc, case_id="c1",
            snap_dir=self.snap_dir, op="knowledge_save", operator="u")
        self.assertEqual(len(self.repo.list_pack_snapshot_history("c1")), 1)

    def test_three_edits_yield_full_chain(self):
        """S0-3 验收：连做三次编辑 → 每次版本推进 + 记录完整可追溯。"""
        versions = [self.repo.get_pack_snapshot("c1").version]
        for i in (1, 2, 3):
            self._rewrite("rules.json", f"edit-{i}")
            fp = commit_ontology_version(
                repo=self.repo, cases=self.svc, case_id="c1",
                snap_dir=self.snap_dir, op="rule_edit",
                operator=f"编辑人{i}", reason=f"第{i}次调整阈值",
                changed_files=["rules.json"])
            versions.append(fp)
            self.assertEqual(self.repo.get_pack_snapshot("c1").version, fp)
        rows = self.repo.list_pack_snapshot_history("c1")
        self.assertEqual(len(rows), 4)  # 建案 + 3 次编辑
        self.assertEqual([r.version for r in rows], versions)
        for prev_row, cur in zip(rows, rows[1:]):
            self.assertEqual(cur.prev_version, prev_row.version)
        # 只追加（R2）：同版本行不被改写（reason/operator 各自保留）
        self.assertEqual(rows[1].operator, "编辑人1")
        self.assertEqual(rows[3].operator, "编辑人3")

    def test_archive_failure_keeps_prev_version(self):
        """E3-1：归档失败 → 保留上一版本 + ops 显式留痕，不静默。"""
        fp_before = self.repo.get_pack_snapshot("c1").version
        self._rewrite("objects.json", "edit-x")
        with patch.object(CaseService, "archive_snapshot",
                          side_effect=OSError("disk full")):
            fp = commit_ontology_version(
                repo=self.repo, cases=self.svc, case_id="c1",
                snap_dir=self.snap_dir, op="model_objects_save",
                operator="u", reason="x")
        self.assertEqual(fp, "")  # 收口失败返回空串
        self.assertEqual(self.repo.get_pack_snapshot("c1").version,
                         fp_before)  # 版本保留
        self.assertEqual(len(self.repo.list_pack_snapshot_history("c1")), 1)
        ops = self.repo.list_ops(kind="ontology_commit_failed")
        self.assertTrue(any(o.get("case_id") == "c1" for o in ops))


if __name__ == "__main__":
    unittest.main()
