"""REQ-D-008 清洗统计落健康度测试。

clean_stats（D2 双通道剔除计数）增强：entries 增加 rows_before（清洗前行数，
AC-6 画像前后行数的数据源）；record_clean_stats 按 (object, property) 聚合落
run_diagnostic（kind=clean_drop_rate），剔除率 >30% 升 warning（AC-4），
样本保持脱敏（AC-3）；无剔除零诊断（不产生噪音）；画像经 clean_stats 参数
展示每属性清洗前后行数（AC-6）。
"""
import json
import unittest

import duckdb

from core.ontology import build_ontology
from core.gateway import OntologyReadGateway
from core.ontology_profile import OntologyProfiler
from core.run_health import RunHealth, record_clean_stats
from core import clean_ops
from tests.test_one2one import _PackCtx

# 测试专用 op（模块级注册一次；滤行谓词 + (value, keep) 双通道）
clean_ops.register_op(
    "d4c_drop_org", impl="py", layer="clean",
    fn=lambda v, ctx=None: any(k in (v or "") for k in ("公司", "厂")),
    description="测试：组织 token 滤行")
clean_ops.register_op(
    "d4c_drop_x", impl="py", layer="clean",
    fn=lambda v, ctx=None: (v, "X" not in (v or "")),
    description="测试：含 X 剔除（二元组契约）")

_OBJ = {"name": "person", "title": "人员", "pk": "person_id", "kind": "entity",
        "name_property": "name",
        "properties": {"name": "string", "memo": "string"}}


def _bind():
    return {"object": "person",
            "source": {"table": "PERS",
                       "columns": {"name": "名称", "memo": "备注"}},
            "clean": {"name": ["d4c_drop_org"], "memo": ["d4c_drop_x"]}}


def _build(rows):
    with _PackCtx([_OBJ], [_bind()]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "备注" VARCHAR)')
        conn.executemany("INSERT INTO PERS VALUES (?,?)", rows)
        stats = build_ontology(conn, pack="p")
        return conn, stats


def _diag_rows(conn, run_id):
    return conn.execute(
        "SELECT severity, reason, detail FROM run_diagnostic "
        "WHERE kind='clean_drop_rate' AND run_id=? ORDER BY seq",
        [run_id]).fetchall()


class TestCleanStats(unittest.TestCase):
    def test_entries_have_rows_before(self):
        """clean_stats entries 带 rows_before = 清洗前 binding 行数（AC-6 数据源）。"""
        rows = [("张三", "m"), ("李四", "m"), ("某某公司", "m")]
        _, stats = _build(rows)
        e = next(x for x in stats["clean_stats"]
                 if x["property"] == "name" and x["rule"] == "d4c_drop_org")
        self.assertEqual(e["rows_before"], 3)
        self.assertEqual(e["dropped_rows"], 1)
        self.assertTrue(e["sample_masked"])     # 样本随落账可审计（AC-3）

    def test_below_threshold_info(self):
        """剔除率 ≤30% → severity=info（1/6≈17%）。"""
        rows = [("张三", "m"), ("李四", "m"), ("王五", "m"),
                ("赵六", "m"), ("钱七", "m"), ("某某公司", "m")]
        conn, stats = _build(rows)
        n = record_clean_stats(conn, stats, run_id="t")
        self.assertEqual(n, 1)
        rows_ = _diag_rows(conn, "t")
        self.assertEqual(rows_[0][0], "info")

    def test_above_threshold_warning(self):
        """AC-4：剔除率 >30% → severity=warning（2/4=50%）。"""
        rows = [("某某公司", "m"), ("某厂", "m"), ("张三", "m"), ("李四", "m")]
        conn, stats = _build(rows)
        record_clean_stats(conn, stats, run_id="t")
        rows_ = _diag_rows(conn, "t")
        self.assertEqual(rows_[0][0], "warning")
        self.assertIn("50%", rows_[0][1])
        d = json.loads(rows_[0][2])
        self.assertEqual(d["dropped_rows"], 2)
        self.assertEqual(d["rows_before"], 4)
        self.assertEqual(d["rows_after"], 2)

    def test_sample_masked_in_detail(self):
        """AC-3：诊断样本脱敏——原值不出现在 reason/detail。"""
        rows = [("某某公司", "m"), ("张三", "m"), ("李四", "m"), ("王五", "m")]
        conn, stats = _build(rows)
        record_clean_stats(conn, stats, run_id="t")
        blob = json.dumps(_diag_rows(conn, "t"), ensure_ascii=False)
        self.assertNotIn("某某公司", blob)
        self.assertIn("某", blob)               # 脱敏后保留首字符
        self.assertIn("*", blob)

    def test_multi_rule_same_property_aggregated(self):
        """同属性多规则剔除聚合为一条诊断（dropped 求和）。"""
        rows = [("某某公司", "m"), ("张三", "X"), ("李四", "m"), ("王五", "m")]
        conn, stats = _build(rows)
        # name 剔 1（公司）+ memo 剔 1（X）→ 两个属性各一条
        n = record_clean_stats(conn, stats, run_id="t")
        self.assertEqual(n, 2)
        rows_ = _diag_rows(conn, "t")
        by_prop = {json.loads(r[2])["prop"]: json.loads(r[2]) for r in rows_}
        self.assertEqual(by_prop["name"]["dropped_rows"], 1)
        self.assertEqual(by_prop["memo"]["dropped_rows"], 1)
        self.assertEqual(by_prop["memo"]["rules"], ["d4c_drop_x"])

    def test_no_drops_no_diagnostics(self):
        """无剔除 → 返回 0、零诊断（不产生噪音）。"""
        conn, stats = _build([("张三", "m"), ("李四", "m")])
        self.assertEqual(stats["clean_stats"], [])
        RunHealth(conn, run_id="t")             # 主管道先建诊断表（同 run_all 口径）
        n = record_clean_stats(conn, stats, run_id="t")
        self.assertEqual(n, 0)
        self.assertEqual(_diag_rows(conn, "t"), [])

    def test_profiler_clean_before_after(self):
        """AC-6：画像 entry["clean"] 展示每属性清洗前后行数。"""
        rows = [("某某公司", "m"), ("张三", "m"), ("李四", "m"), ("王五", "m")]
        with _PackCtx([_OBJ], [_bind()]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "备注" VARCHAR)')
            conn.executemany("INSERT INTO PERS VALUES (?,?)", rows)
            stats = build_ontology(conn, pack="p")
            gw = OntologyReadGateway(conn, pack="p")
            prof = OntologyProfiler(gw, pack="p", clean_stats=stats["clean_stats"])
            report = prof.profile_all()
        entry = next(e for e in report["l1_l2"]
                     if e["obj"] == "person" and e["prop"] == "name")
        self.assertEqual(entry["clean"], {"rows_before": 4, "rows_after": 3,
                                          "dropped_rows": 1,
                                          "rules": ["d4c_drop_org"]})
        plain = next(e for e in report["l1_l2"]
                     if e["obj"] == "person" and e["prop"] == "memo")
        self.assertNotIn("clean", plain)   # memo 无剔除 → 不挂 clean 键


if __name__ == "__main__":
    unittest.main()
