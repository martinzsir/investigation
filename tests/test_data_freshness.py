"""REQ-D-019 数据时间新鲜度测试。

按对象 date 时间属性取 MAX(数据时间) 与当前日期比对，超期告警。与本体版本
新鲜度（FRESH/STALE/UNBUILT，回答"声明是否过期"）分开显示、互不混淆。

  - AC-1 显示每个含时间属性对象的最新数据时间
  - AC-2 超期（可配 stale_days）触发告警
  - AC-3 时间属性全空不误报为"很旧"
  - AC-4 独立 source/kind，不碰本体版本新鲜度
  - AC-5 进健康度小节
"""
import datetime as dt
import unittest
from contextlib import contextmanager

import duckdb

from core.data_freshness import scan
from core.gateway import OntologyReadGateway
from core.ontology import build_ontology
from core.run_health import RunHealth
from tests.test_one2one import _PackCtx

_OBJ = {"name": "txn", "title": "交易", "pk": "txn_id", "kind": "event",
        "name_property": "person_raw",
        "properties": {"person_raw": "string", "date": "date"}}

_AS_OF = dt.date(2025, 9, 6)


@contextmanager
def _pack(rows):
    with _PackCtx([_OBJ], [{
            "object": "txn",
            "source": {"table": "TXN",
                       "columns": {"person_raw": "主体", "date": "日期"}}}]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TXN ("主体" VARCHAR, "日期" VARCHAR)')
        conn.executemany("INSERT INTO TXN VALUES (?,?)", rows)
        build_ontology(conn, pack="p")
        yield conn


def _gw(conn):
    return OntologyReadGateway(conn, pack="p")


class TestDataFreshness(unittest.TestCase):
    def test_AC1_latest_data_time_per_object(self):
        """返回每个含时间属性对象的最新数据时间与账龄（天）。"""
        with _pack([("张三", "2024-03-01"), ("李四", "2025-01-15")]) as conn:
            r = scan(_gw(conn), as_of=_AS_OF, stale_days=180)
            self.assertEqual(len(r["objects"]), 1)
            e = r["objects"][0]
            self.assertEqual(e["object"], "txn")
            self.assertEqual(e["property"], "date")
            self.assertEqual(e["latest"], "2025-01-15")   # MAX 取最新
            self.assertEqual(e["age_days"], (_AS_OF - dt.date(2025, 1, 15)).days)

    def test_AC2_stale_beyond_threshold_alerted(self):
        """半年前数据超过 180 天阈值 → 告警；近期数据不告警（阈值可配）。"""
        with _pack([("张三", "2024-03-01")]) as conn:
            r_old = scan(_gw(conn), as_of=_AS_OF, stale_days=180)
            self.assertEqual(r_old["stale"], 1)
        with _pack([("张三", "2025-09-01")]) as conn:
            r_fresh = scan(_gw(conn), as_of=_AS_OF, stale_days=180)
            self.assertEqual(r_fresh["stale"], 0)

    def test_AC3_empty_date_not_reported_stale(self):
        """时间属性全空 → 跳过，不误报为"很旧"。"""
        with _pack([("张三", None), ("李四", None)]) as conn:
            r = scan(_gw(conn), as_of=_AS_OF, stale_days=180)
            self.assertEqual(r["objects"], [])
            self.assertEqual(r["stale"], 0)

    def test_AC4_separate_from_version_freshness(self):
        """独立 source=data_freshness / kind=data_freshness_stale，不读本体版本。"""
        with _pack([("张三", "2024-03-01")]) as conn:
            rh = RunHealth(conn)
            scan(_gw(conn), health=rh, as_of=_AS_OF, stale_days=180)
            s = rh.summary()
            self.assertIn("data_freshness", s["by_source"])
            self.assertIn("data_freshness_stale", s["by_kind"])

    def test_AC5_recorded_to_health(self):
        """超期告警落 run_diagnostic，进健康度小节。"""
        with _pack([("张三", "2024-03-01")]) as conn:
            rh = RunHealth(conn)
            scan(_gw(conn), health=rh, as_of=_AS_OF, stale_days=30)
            s = rh.summary()
            self.assertGreaterEqual(s["by_kind"].get("data_freshness_stale", 0), 1)


if __name__ == "__main__":
    unittest.main()
