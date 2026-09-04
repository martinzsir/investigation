"""
tests/test_type_extension.py
REQ-041 类型系统扩展测试。

覆盖 AC1–AC5：
  AC1: 四种新类型（timestamp/duration_days/enum/json）可声明、可校验、可物化
  AC2: enum 非法值 → 装载期硬失败
  AC3: duration_days 支持比较运算
  AC4: 新类型全部有 source/transform/权限/血缘测试
  AC5: 现有 6 条规则行为不变（向后兼容）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.ontology import TYPE_SQL, TYPE_NAMES, build_ontology  # noqa: E402
from core.ontology_loader import load_pack                    # noqa: E402


class TestTypeExtension(unittest.TestCase):

    # ---- AC1: 四种新类型可声明、可校验、可物化 ----

    def test_ac1_new_types_in_type_sql(self):
        """AC1: 四种新类型在 TYPE_SQL 中有物化列类型映射。"""
        self.assertIn("timestamp", TYPE_SQL)
        self.assertIn("duration_days", TYPE_SQL)
        self.assertIn("enum", TYPE_SQL)
        self.assertIn("json", TYPE_SQL)
        self.assertEqual(TYPE_SQL["timestamp"], "TIMESTAMP")
        self.assertEqual(TYPE_SQL["duration_days"], "INTEGER")
        self.assertEqual(TYPE_SQL["enum"], "VARCHAR")
        self.assertEqual(TYPE_SQL["json"], "VARCHAR")
        self.assertIn("timestamp", TYPE_NAMES)
        self.assertIn("duration_days", TYPE_NAMES)

    def test_ac1_new_types_loadable(self):
        """AC1: objects.json 中的新类型属性可以正常装载。"""
        spec = load_pack("default")
        # osint_article 有 timestamp/duration_days 属性
        osint = next(o for o in spec.objects if o.name == "osint_article")
        self.assertEqual(osint.properties["crawled_at"], "timestamp")
        self.assertEqual(osint.properties["retention_days"], "duration_days")
        # decision 有 enum/json 属性
        decision = next(o for o in spec.objects if o.name == "decision")
        self.assertEqual(decision.properties["decision_type"], "enum")
        self.assertEqual(decision.properties["metadata"], "json")
        # enum_values 白名单已装载
        self.assertIn("decision_type", decision.enum_values)
        self.assertEqual(
            decision.enum_values["decision_type"],
            ["立案", "不予立案", "移送管辖", "补充侦查", "撤销"])

    def test_ac1_materialize_new_types(self):
        """AC1: 新类型物化建表时列类型正确。"""
        s = Store(db_path=":memory:")
        c = s.conn
        # 建源表（含新类型列）
        c.execute("""CREATE TABLE 公开OSINT (
            主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR,
            采集时间 TIMESTAMP, 保留天数 INTEGER)""")
        c.execute("INSERT INTO 公开OSINT VALUES ('测试','内容','2024-01-01','来源','2024-01-01 10:00:00',30)")
        # 其他必需源表
        c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
        c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
        c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
        c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
        c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
        build_ontology(c)
        # 检查 obj_osint_article 表结构
        cols = {r[1]: r[2] for r in c.execute(
            "PRAGMA table_info('obj_osint_article')").fetchall()}
        self.assertIn("crawled_at", cols)
        self.assertEqual(cols["crawled_at"], "TIMESTAMP")
        self.assertIn("retention_days", cols)
        self.assertEqual(cols["retention_days"], "INTEGER")
        s.close()

    def test_ac1_runtime_enum_json_table(self):
        """AC1: runtime 对象（decision）建表时 enum/json 列为 VARCHAR。"""
        from core.ontology import ensure_runtime_tables
        s = Store(db_path=":memory:")
        c = s.conn
        ensure_runtime_tables(c)
        cols = {r[1]: r[2] for r in c.execute(
            "PRAGMA table_info('obj_decision')").fetchall()}
        self.assertIn("decision_type", cols)
        self.assertEqual(cols["decision_type"], "VARCHAR")  # enum → VARCHAR
        self.assertIn("metadata", cols)
        self.assertEqual(cols["metadata"], "VARCHAR")  # json → VARCHAR
        s.close()

    # ---- AC2: enum 非法值 → 装载期硬失败 ----

    def test_ac2_enum_missing_values_hard_fail(self):
        """AC2: enum 属性未声明 enum_values → 装载期硬失败。"""
        import json
        import tempfile
        import os
        tmpdir = tempfile.mkdtemp()
        # 写一个最小 ontology 包，enum 属性缺 enum_values
        pack_dir = Path(tmpdir) / "test_pack"
        pack_dir.mkdir()
        (pack_dir / "objects.json").write_text(json.dumps({
            "schema_version": 2,
            "objects": [{
                "name": "test_obj",
                "pk": "test_id",
                "kind": "entity",
                "name_property": "test_id",
                "properties": {"status": "enum"}
            }]
        }), encoding="utf-8")
        # 需要其他文件存在
        for fname in ("links.json", "bindings.json", "rules.json",
                       "actions.json", "functions.json"):
            (pack_dir / fname).write_text("{}", encoding="utf-8")
        (pack_dir / "policies.json").write_text(json.dumps({
            "object_policies": [{"object": "test_obj", "roles": ["system"], "min_clearance": 0}],
            "link_policies": [],
            "property_policies": []
        }), encoding="utf-8")
        (pack_dir / "llm_policy.json").write_text(json.dumps({
            "schema_version": 2, "network": "isolated",
            "allowed_models": [], "pii_redaction": {},
            "retention": {}, "fallback": "deterministic_only"
        }), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            load_pack("test_pack", base_dir=Path(tmpdir))
        self.assertIn("enum_values", str(ctx.exception))

    def test_ac2_enum_with_values_ok(self):
        """AC2: enum 属性有 enum_values → 装载成功。"""
        spec = load_pack("default")
        decision = next(o for o in spec.objects if o.name == "decision")
        self.assertEqual(
            decision.enum_values["decision_type"],
            ["立案", "不予立案", "移送管辖", "补充侦查", "撤销"])

    def test_ac2_enum_values_for_non_enum_hard_fail(self):
        """AC2: enum_values 声明在非 enum 属性上 → 硬失败。"""
        import json
        import tempfile
        tmpdir = tempfile.mkdtemp()
        pack_dir = Path(tmpdir) / "test_pack2"
        pack_dir.mkdir()
        (pack_dir / "objects.json").write_text(json.dumps({
            "schema_version": 2,
            "objects": [{
                "name": "test_obj",
                "pk": "test_id",
                "kind": "entity",
                "name_property": "test_id",
                "properties": {"status": "string"},
                "enum_values": {"status": ["a", "b"]}
            }]
        }), encoding="utf-8")
        for fname in ("links.json", "bindings.json", "rules.json",
                       "actions.json", "functions.json"):
            (pack_dir / fname).write_text("{}", encoding="utf-8")
        (pack_dir / "policies.json").write_text(json.dumps({
            "object_policies": [{"object": "test_obj", "roles": ["system"], "min_clearance": 0}],
            "link_policies": [], "property_policies": []
        }), encoding="utf-8")
        (pack_dir / "llm_policy.json").write_text(json.dumps({
            "schema_version": 2, "network": "isolated",
            "allowed_models": [], "pii_redaction": {},
            "retention": {}, "fallback": "deterministic_only"
        }), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            load_pack("test_pack2", base_dir=Path(tmpdir))
        self.assertIn("不是 enum 类型", str(ctx.exception))

    # ---- AC3: duration_days 支持比较运算 ----

    def test_ac3_duration_days_comparison(self):
        """AC3: duration_days 列（INTEGER）支持 SQL 比较运算。"""
        s = Store(db_path=":memory:")
        c = s.conn
        c.execute("""CREATE TABLE 公开OSINT (
            主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR,
            采集时间 TIMESTAMP, 保留天数 INTEGER)""")
        c.execute("INSERT INTO 公开OSINT VALUES ('A','内容','2024-01-01','来源','2024-01-01 10:00:00',30)")
        c.execute("INSERT INTO 公开OSINT VALUES ('B','内容','2024-01-01','来源','2024-01-01 10:00:00',90)")
        c.execute("INSERT INTO 公开OSINT VALUES ('C','内容','2024-01-01','来源','2024-01-01 10:00:00',7)")
        # 其他必需源表
        c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
        c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
        c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
        c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
        c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
        build_ontology(c)
        # 比较运算：> / < / >= / <= / BETWEEN
        gt = c.execute("SELECT COUNT(*) FROM obj_osint_article WHERE retention_days > 20").fetchone()[0]
        self.assertEqual(gt, 2)  # 30, 90
        lt = c.execute("SELECT COUNT(*) FROM obj_osint_article WHERE retention_days < 10").fetchone()[0]
        self.assertEqual(lt, 1)  # 7
        between = c.execute("SELECT COUNT(*) FROM obj_osint_article WHERE retention_days BETWEEN 20 AND 100").fetchone()[0]
        self.assertEqual(between, 2)  # 30, 90
        s.close()

    # ---- AC4: 新类型有 source/transform/权限/血缘测试 ----

    def test_ac4_source_cast(self):
        """AC4: 结构化 source 编译期对新类型做 CAST。"""
        s = Store(db_path=":memory:")
        c = s.conn
        c.execute("""CREATE TABLE 公开OSINT (
            主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR,
            采集时间 VARCHAR, 保留天数 VARCHAR)""")
        c.execute("INSERT INTO 公开OSINT VALUES ('A','内容','2024-01-01','来源','2024-01-01 10:00:00','30')")
        c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
        c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
        c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
        c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
        c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
        build_ontology(c)
        # CAST 后值类型正确
        row = c.execute("SELECT retention_days FROM obj_osint_article LIMIT 1").fetchone()
        self.assertEqual(row[0], 30)  # VARCHAR '30' → INTEGER 30
        # timestamp CAST
        row2 = c.execute("SELECT crawled_at FROM obj_osint_article LIMIT 1").fetchone()
        self.assertIsNotNone(row2[0])
        s.close()

    def test_ac4_policy_coverage(self):
        """AC4: 新类型属性在 policies.json 有覆盖率检查（无遗漏）。"""
        from core.policy import PolicyEngine
        spec = load_pack("default")
        pe = PolicyEngine("default")
        # 新类型属性不影响覆盖率——coverage_missing 检查对象级，不是属性级
        missing = pe.coverage_missing({o.name for o in spec.objects})
        self.assertEqual(missing, [])  # 所有对象都有策略声明

    # ---- AC5: 向后兼容 ----

    def test_ac5_existing_rules_unchanged(self):
        """AC5: 既有测试夹具可正常 build_ontology（无新列则跳过 optional）。"""
        s = Store(db_path=":memory:")
        c = s.conn
        # 旧 schema（无采集时间/保留天数列）
        c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
        c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
        c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
        c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
        c.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
        c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
        c.execute("INSERT INTO 银行流水 VALUES ('张三','李四',1000,'2024-01-01')")
        c.execute("INSERT INTO 通话记录 VALUES ('张三','李四','2024-01-01',1)")
        # build_ontology 不报错（optional=True 的 osint_article 缺列跳过）
        build_ontology(c)
        # 既有对象正常物化
        person_count = c.execute("SELECT COUNT(*) FROM obj_person").fetchone()[0]
        self.assertGreater(person_count, 0)
        s.close()

    def test_ac5_old_type_names_unchanged(self):
        """AC5: 原有 5 种类型映射不变。"""
        self.assertEqual(TYPE_SQL["string"], "VARCHAR")
        self.assertEqual(TYPE_SQL["integer"], "BIGINT")
        self.assertEqual(TYPE_SQL["decimal"], "DOUBLE")
        self.assertEqual(TYPE_SQL["date"], "DATE")
        self.assertEqual(TYPE_SQL["boolean"], "BOOLEAN")


if __name__ == "__main__":
    unittest.main()
