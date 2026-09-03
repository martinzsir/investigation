"""
tests/test_features.py
REQ-015 L1 特征落盘与版本化 测试。

覆盖 AC1-AC5：
  AC1: feat_l1 持久化，新连接（重启）按 feature_set_hash 读回
  AC2: 同输入同 hash（确定性，键序无关），不同输入不同 hash
  AC3: 记录 data_version；源版本推进 mark_stale 置旧特征 stale=true
  AC4: feature_ref() 携带 feature_set_hash 签名
  AC5: get_or_compute 缓存 miss 回源重算落盘，命中不重算
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.features import (                                    # noqa: E402
    FeatureStore, feature_set_hash, feature_ref, FeatureRecord,
)


class TestFeatureStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "feat.duckdb")
        self.conn = duckdb.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _put(self, fs, *, entity="person_1", window="2024Q1",
             name="cash_total", payload=None, inputs=None, version="build-1"):
        return fs.put(
            entity_id=entity, time_window=window, feature_name=name,
            payload=payload if payload is not None else {"total": 120000},
            inputs=inputs if inputs is not None else {"rows": [1, 2, 3]},
            data_version=version)

    def test_ac1_persist_across_restart(self):
        """AC1: 写入后新连接（模拟重启）仍可读回。"""
        fs = FeatureStore(self.conn)
        rec = self._put(fs)
        # 新连接 + 新 FeatureStore（热缓存为空，必须走表）
        conn2 = duckdb.connect(self.db)
        try:
            fs2 = FeatureStore(conn2)
            got = fs2.get(rec.feature_set_hash)
            self.assertIsNotNone(got)
            self.assertEqual(got.payload, {"total": 120000})
            self.assertEqual(got.data_version, "build-1")
            self.assertEqual(got.entity_id, "person_1")
            self.assertFalse(got.stale)
        finally:
            conn2.close()

    def test_ac2_deterministic_hash(self):
        """AC2: 同输入同 hash（dict 键序无关）；输入变 hash 变。"""
        h1 = feature_set_hash("p1", "2024Q1", "f", {"a": 1, "b": 2})
        h2 = feature_set_hash("p1", "2024Q1", "f", {"b": 2, "a": 1})
        self.assertEqual(h1, h2)
        self.assertNotEqual(
            h1, feature_set_hash("p1", "2024Q1", "f", {"a": 1, "b": 3}))
        self.assertNotEqual(
            h1, feature_set_hash("p2", "2024Q1", "f", {"a": 1, "b": 2}))
        self.assertNotEqual(
            h1, feature_set_hash("p1", "2024Q2", "f", {"a": 1, "b": 2}))
        # 不可变：同 hash 重复 put 不覆盖
        fs = FeatureStore(self.conn)
        r1 = self._put(fs, version="build-1")
        r2 = self._put(fs, version="build-2")
        self.assertEqual(r1.feature_set_hash, r2.feature_set_hash)
        got = fs.get(r1.feature_set_hash)
        self.assertEqual(got.data_version, "build-1")

    def test_ac3_stale_on_data_version_change(self):
        """AC3: 源版本推进 → 旧版本特征置 stale，返回受影响行数。"""
        fs = FeatureStore(self.conn)
        old = self._put(fs, version="build-1")
        new = self._put(
            fs, entity="person_2", payload={"total": 1},
            inputs={"rows": [9]}, version="build-2")

        n = fs.mark_stale("build-2")
        self.assertEqual(n, 1)  # 仅 build-1 的一条变 stale
        # 新实例读回（绕过热缓存）
        fs2 = FeatureStore(self.conn)
        self.assertTrue(fs2.get(old.feature_set_hash).stale)
        self.assertFalse(fs2.get(new.feature_set_hash).stale)
        self.assertEqual(fs2.count_stale(), 1)
        self.assertEqual(fs2.stale_hashes(), [old.feature_set_hash])

    def test_ac4_feature_ref_signature(self):
        """AC4: 评分/规则入参携带 feature_set_hash 签名。"""
        fs = FeatureStore(self.conn)
        rec = self._put(fs)
        self.assertEqual(feature_ref(rec),
                         {"feature_set_hash": rec.feature_set_hash})
        self.assertEqual(feature_ref(rec.feature_set_hash),
                         {"feature_set_hash": rec.feature_set_hash})
        self.assertIsInstance(rec, FeatureRecord)

    def test_ac5_get_or_compute_miss_recomputes(self):
        """AC5: miss 回源重算落盘；命中（热缓存/落盘）不重算。"""
        calls = []

        def compute():
            calls.append(1)
            return {"score": 0.8}

        kw = dict(entity_id="p1", time_window="2024Q1", feature_name="score",
                  inputs={"x": 1}, data_version="build-1")
        fs = FeatureStore(self.conn)
        r1 = fs.get_or_compute(compute_fn=compute, **kw)
        r2 = fs.get_or_compute(compute_fn=compute, **kw)
        self.assertEqual(len(calls), 1)            # 第二次热缓存命中
        self.assertEqual(r1.feature_set_hash, r2.feature_set_hash)
        self.assertEqual(r2.payload, {"score": 0.8})

        # 新实例（热缓存空）→ 落盘命中，仍不重算
        fs2 = FeatureStore(self.conn)
        r3 = fs2.get_or_compute(compute_fn=compute, **kw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r3.feature_set_hash, r1.feature_set_hash)

        # 输入变化 → miss → 重算落盘
        r4 = fs2.get_or_compute(
            compute_fn=compute, **{**kw, "inputs": {"x": 2}})
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(r4.feature_set_hash, r1.feature_set_hash)


if __name__ == "__main__":
    unittest.main()
