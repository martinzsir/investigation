"""
tests/test_store_backend.py
M1 阶段 A：StoreBackend 抽象 + CaseStore + StoreFactory + grep 门禁（W-001/002/003）。

验收点（M1_plan 阶段 A）：
  - ABC 不可实例化；read 模式写连接拒绝、写尝试由 DuckDB 只读拒绝；
  - CrossCaseStore 骨架 read/write/query 一律 UnsupportedOperation（M5）；
  - A/B 案件互不可见；query 返回字典列表；引用计数随 open/close 增减；
  - 版本指针：未注入 meta 时 read 必须显式 version；注入后按指针解析；
  - grep 门禁两条硬断言（服务端开库收口，破线即红）。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from abc import ABC
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.store import (
    CaseStore,
    CrossCaseStore,
    StoreBackend,
    StoreFactory,
    UnsupportedOperation,
)


class _FakeMeta:
    """版本指针桩（阶段 D 前用于工厂指针解析）。"""

    def __init__(self, versions: dict[str, int]):
        self._versions = dict(versions)

    def current_version(self, case_id: str) -> int:
        return self._versions[case_id]


class TestStoreBackendABC(unittest.TestCase):

    def test_abc_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            StoreBackend()  # type: ignore[abstract]

    def test_abc_is_abstract_base(self):
        self.assertTrue(issubclass(CaseStore, StoreBackend))
        self.assertTrue(issubclass(CrossCaseStore, StoreBackend))
        self.assertIsInstance(CaseStore(Path("x"), case_id="c", version=1,
                                        mode="write"), StoreBackend)


class TestCaseStoreModes(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = StoreFactory(cases_root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_mode_creates_file_and_rw(self):
        s = self.f.for_case("caseA", mode="write", version=1)
        s.write_conn.execute("CREATE TABLE t (x INTEGER)")
        s.write_conn.execute("INSERT INTO t VALUES (1), (2)")
        rows = s.query("SELECT * FROM t ORDER BY x")
        self.assertEqual(rows, [{"x": 1}, {"x": 2}])
        self.assertEqual(s.version, 1)
        self.assertEqual(s.case_id, "caseA")
        self.assertTrue(self.f.version_path("caseA", 1).exists())
        s.close()

    def test_read_mode_write_conn_rejected(self):
        s = self.f.for_case("caseA", mode="write", version=1)
        s.write_conn.execute("CREATE TABLE t (x INTEGER)")
        s.write_conn.execute("INSERT INTO t VALUES (42)")
        s.close()
        r = self.f.for_case("caseA", mode="read", version=1)
        with self.assertRaises(UnsupportedOperation):
            r.write_conn
        # DuckDB 只读连接：写尝试由引擎自身拒绝
        with self.assertRaises(duckdb.Error):
            r.read_conn.execute("INSERT INTO t VALUES (3)")
        with self.assertRaises(duckdb.Error):
            r.read_conn.execute("CREATE TABLE t2 (y INTEGER)")
        # 读不受影响
        self.assertEqual(r.query("SELECT COUNT(*) AS n FROM t"), [{"n": 1}])
        r.close()

    def test_read_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.f.for_case("ghost", mode="read", version=9).read_conn

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            self.f.for_case("c", mode="append", version=1)
        with self.assertRaises(ValueError):
            CaseStore(Path("x"), case_id="c", version=1, mode="x")  # type: ignore[arg-type]

    def test_close_idempotent_and_ctx(self):
        s = self.f.for_case("caseA", mode="write", version=1)
        s.write_conn.execute("CREATE TABLE t (x INTEGER)")
        s.close()
        s.close()  # 幂等
        with self.f.for_case("caseA", mode="read", version=1) as r:
            self.assertEqual(r.query("SELECT 1 AS v"), [{"v": 1}])


class TestCaseIsolation(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = StoreFactory(cases_root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cases_mutually_invisible(self):
        a = self.f.for_case("caseA", mode="write", version=1)
        a.write_conn.execute("CREATE TABLE secret_a (v VARCHAR)")
        a.write_conn.execute("INSERT INTO secret_a VALUES ('A-only')")
        a.close()
        b = self.f.for_case("caseB", mode="write", version=1)
        b.write_conn.execute("CREATE TABLE secret_b (v VARCHAR)")
        b.write_conn.execute("INSERT INTO secret_b VALUES ('B-only')")
        b.close()

        ra = self.f.for_case("caseA", mode="read", version=1)
        self.assertEqual(ra.query("SELECT v FROM secret_a"), [{"v": "A-only"}])
        with self.assertRaises(duckdb.Error):
            ra.query("SELECT * FROM secret_b")
        rb = self.f.for_case("caseB", mode="read", version=1)
        with self.assertRaises(duckdb.Error):
            rb.query("SELECT * FROM secret_a")
        ra.close()
        rb.close()

    def test_versions_coexist(self):
        """v1 读者与 v2 写者指向不同文件，互不干扰（版本化文件基础）。"""
        v1 = self.f.for_case("caseA", mode="write", version=1)
        v1.write_conn.execute("CREATE TABLE t (v INTEGER)")
        v1.write_conn.execute("INSERT INTO t VALUES (1)")
        v1.close()
        r1 = self.f.for_case("caseA", mode="read", version=1)
        v2 = self.f.for_case("caseA", mode="write", version=2)
        v2.write_conn.execute("CREATE TABLE t (v INTEGER)")
        v2.write_conn.execute("INSERT INTO t VALUES (2)")
        v2.close()
        # v1 读者仍读到旧内容
        self.assertEqual(r1.query("SELECT v FROM t"), [{"v": 1}])
        r2 = self.f.for_case("caseA", mode="read", version=2)
        self.assertEqual(r2.query("SELECT v FROM t"), [{"v": 2}])
        r1.close()
        r2.close()


class TestCrossCaseSkeleton(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = StoreFactory(cases_root=self.tmp)

    def tearDown(self):
        from server.app.store.backend_cross import evict_cache
        evict_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cross_case_attach_readonly(self):
        """M5：CrossCaseStore ATTACH READ_ONLY + 只读查询。"""
        # 造两个案件版本文件
        for cid in ("caseA", "caseB"):
            db = self.f.version_path(cid, 1)
            db.parent.mkdir(parents=True, exist_ok=True)
            conn = duckdb.connect(str(db))
            conn.execute(f"CREATE TABLE t (v VARCHAR)")
            conn.execute(f"INSERT INTO t VALUES ('{cid}')")
            conn.close()
        # 用 version_paths 直接构造（绕过 meta 指针）
        from server.app.store.backend_cross import CrossCaseStore
        paths = {cid: self.f.version_path(cid, 1)
                 for cid in ("caseA", "caseB")}
        x = CrossCaseStore(["caseA", "caseB"], version_paths=paths)
        self.assertEqual(sorted(x.authorized_cases), ["caseA", "caseB"])
        self.assertEqual(x.case_id, "*cross-case*")
        # ATTACH 后可跨库查询
        rows = x.query("SELECT v FROM case_caseA.t UNION ALL "
                       "SELECT v FROM case_caseB.t")
        self.assertEqual(sorted(r["v"] for r in rows), ["caseA", "caseB"])
        # write_conn 永远拒绝
        with self.assertRaises(UnsupportedOperation):
            x.write_conn
        # DDL/DML 拒绝
        with self.assertRaises(UnsupportedOperation):
            x.query("CREATE TABLE x (i INT)")
        x.close()


class TestFactoryPointerAndRefs(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_without_meta_or_version_rejected(self):
        f = StoreFactory(cases_root=self.tmp)
        with self.assertRaises(UnsupportedOperation):
            f.for_case("caseA", mode="read")
        with self.assertRaises(ValueError):
            f.for_case("caseA", mode="write")  # 写必须显式目标版本

    def test_meta_pointer_resolution(self):
        # 先造 v3 文件
        f = StoreFactory(cases_root=self.tmp, meta=_FakeMeta({"caseA": 3}))
        w = f.for_case("caseA", mode="write", version=3)
        w.write_conn.execute("CREATE TABLE t (v INTEGER)")
        w.write_conn.execute("INSERT INTO t VALUES (30)")
        w.close()
        r = f.for_case("caseA", mode="read")  # 经指针解析到 v3
        self.assertEqual(r.version, 3)
        self.assertEqual(r.query("SELECT v FROM t"), [{"v": 30}])
        r.close()

    def test_reader_reference_count(self):
        f = StoreFactory(cases_root=self.tmp)
        w = f.for_case("caseA", mode="write", version=1)
        w.write_conn.execute("CREATE TABLE t (v INTEGER)")
        w.close()
        self.assertEqual(f.reader_count("caseA", 1), 0)
        r1 = f.for_case("caseA", mode="read", version=1)
        r2 = f.for_case("caseA", mode="read", version=1)
        self.assertEqual(f.reader_count("caseA", 1), 2)
        # 写者不计数
        w2 = f.for_case("caseA", mode="write", version=2)
        self.assertEqual(f.reader_count("caseA", 2), 0)
        w2.close()
        r1.close()
        self.assertEqual(f.reader_count("caseA", 1), 1)
        r2.close()
        self.assertEqual(f.reader_count("caseA", 1), 0)

    def test_for_local_layout(self):
        """for_local 兼容既有 data/investigation.duckdb 单案件布局。"""
        local_root = self.tmp / "data"
        f = StoreFactory(cases_root=self.tmp)
        s = f.for_local(mode="write", root=local_root,
                        db_path="investigation.duckdb")
        self.assertEqual(s.case_id, "local")
        self.assertEqual(s.version, 0)
        s.write_conn.execute("CREATE TABLE t (v INTEGER)")
        s.close()
        self.assertTrue((local_root / "investigation.duckdb").exists())


class TestServerOpenStoreGate(unittest.TestCase):
    """grep 门禁：服务端开库收口（W-001/002），破线即红。"""

    SERVER = ROOT / "server"

    def _py_files(self, base: Path):
        return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]

    def test_no_bare_store_instantiation_in_server(self):
        """server/ 树内不得出现 core Store 的无参实例化。"""
        bad = []
        pat = re.compile(r"(?<![\w.])Store\s*\(\s*\)")
        for p in self._py_files(self.SERVER):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
        self.assertEqual(bad, [], f"server/ 内发现无参 Store 实例化：\n" +
                                  "\n".join(bad))

    def test_no_direct_duckdb_connect_outside_store_pkg(self):
        """server/app/ 除 store/ 包外，不得直接 duckdb 连接。"""
        bad = []
        pat = re.compile(r"duckdb\.connect\s*\(")
        for p in self._py_files(self.SERVER / "app"):
            if "store" in p.relative_to(self.SERVER).parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
        self.assertEqual(bad, [], f"server/app/ 非 store 模块发现直连：\n" +
                                  "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
