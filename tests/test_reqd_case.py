"""
tests/test_reqd_case.py
REQ-D 业务测试案例：海州"11·03"刷单返利电诈团伙案 —— 24 探针端到端验收。

对位 test_golden 模式：场景固化在 tests/fixtures/reqd_case.json（合成数据，
生成器 scripts.gen_reqd_case 为单一真源），内存库灌行 → build_ontology
(pack=reqd_case) → 装载生存/清洗管道/质量扫描/复合列/治理贯通五组探针。

判定基线（案例文档 §4）：PASS 21 / N-A 3（D-04 走 quarantine 计数核对降级；
D-19/20 因双路径均已实现而升格 PASS）；陷阱列（D-11/D-18）误报即 FAIL。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

import core.ontology_loader as ol
from core.ontology import build_ontology
from core.run_health import (
    RunHealth,
    record_build_degraded,
    record_build_dirty,
    record_clean_stats,
)
from core.compliance import scan as compliance_scan
from core.sensitive_scan import scan as sensitive_scan
from core.ontology_profile import OntologyProfiler
from core.gateway import OntologyReadGateway
from core.ontology_loader import load_data_elements, load_pack
from core.de_recommend import recommend_for_column
from scripts.scan_hardcoded_names import scan_pack_enum
from scripts.gen_reqd_case import SUBJECTS, PERSONAL_POOL

FIXTURE = Path(__file__).parent / "fixtures" / "reqd_case.json"
PACK_DIR = Path(__file__).parent.parent / "ontology" / "reqd_case"

# R-1 红线：同名实体强证据分区（与 test_entity_redline 同口径加载）
from core.entity import _load_person_resolver

EntityResolver = _load_person_resolver()
try:
    from entity_resolution import normalize_person_name
except ImportError:                                     # pragma: no cover
    normalize_person_name = None


class ReqdCaseBase(unittest.TestCase):
    """整案一次性构建（内存库），24 探针共享同一语义层快照。"""

    @classmethod
    def setUpClass(cls):
        fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.fx = fx
        conn = duckdb.connect(":memory:")
        for name, spec in fx["tables"].items():
            cols = spec["columns"]
            ddl = ", ".join(f'"{c}" VARCHAR' for c in cols)   # 真实导出即文本
            conn.execute(f'CREATE TABLE "{name}" ({ddl})')
            ph = ", ".join("?" * len(cols))
            conn.executemany(
                f'INSERT INTO "{name}" VALUES ({ph})',
                [[str(r[c]) for c in cols] for r in spec["rows"]])
        cls.conn = conn
        cls.stats = build_ontology(conn, pack="reqd_case")
        cls.rh = RunHealth(conn, run_id="reqd")
        record_build_dirty(conn, cls.stats, run_id="reqd")
        record_build_degraded(conn, cls.stats, run_id="reqd")
        record_clean_stats(conn, cls.stats, run_id="reqd")
        cls.gw = OntologyReadGateway(conn, pack="reqd_case")
        cls.comp = compliance_scan(cls.gw, health=cls.rh)
        cls.sens = sensitive_scan(cls.gw, health=cls.rh)
        prof = OntologyProfiler(cls.gw, pack="reqd_case", health=cls.rh,
                                clean_stats=cls.stats["clean_stats"])
        cls.rep = prof.profile_all()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # ---- 断言辅助 ----
    def one(self, sql, params=None):
        return self.conn.execute(sql, params or []).fetchone()

    def rows(self, sql, params=None):
        return self.conn.execute(sql, params or []).fetchall()

    def diag(self, kind, obj=None, prop=None):
        """健康度诊断明细（run_diagnostic 行；detail JSON 已展平到顶层）。"""
        out = []
        for r in self.rh.rows():
            if r.get("kind") != kind:
                continue
            row = {**r, **(r.get("detail") or {})}
            if obj is not None and row.get("object") != obj:
                continue
            if prop is not None and row.get("prop") != prop:
                continue
            out.append(row)
        return out

    def comp_prop(self, key):
        return self.comp["by_property"].get(key)

    def clean_entry(self, obj, prop):
        for e in self.stats["clean_stats"]:
            if e["object"] == obj and e["property"] == prop:
                return e
        return None

    def _temp_pack(self, mutate):
        """复制声明包到临时目录并做声明变异（D-06/D-20 负路径），返回包名。"""
        tmp = Path(tempfile.mkdtemp())
        name = "reqd_case_probe_neg"
        dst = tmp / name
        shutil.copytree(PACK_DIR, dst)
        mutate(dst)
        return tmp, name


class TestLoadSurvival(ReqdCaseBase):
    """装载生存组 D-01~D-06。"""

    def test_d01_all_six_sources_build_without_break(self):
        """D-01：6 源表一次装载 0 中断，8 对象全部物化且行数锁定。"""
        self.assertEqual(
            self.stats["objects"],
            {"person": 24, "account": 72, "transaction": 74, "call": 80,
             "call_old": 10, "case_ledger": 1, "cp_split": 15, "cp_whole": 15})
        self.assertEqual(self.stats["links"],
                         {"transfers": 74, "calls_to": 80})
        self.assertEqual(self.stats["skipped"], [])

    def test_d02_thousands_amount_cast_exact_value(self):
        """D-02（009 AC-1）：15 行千分位/货币符金额经 transform 可 CAST 且值正确。"""
        n1 = self.one("SELECT COUNT(*) FROM obj_transaction "
                      "WHERE amount = 1280.5")[0]
        n2 = self.one("SELECT COUNT(*) FROM obj_transaction "
                      "WHERE amount = 48000.0")[0]
        self.assertEqual(n1, 5)          # ￥1,280.50 ×5
        self.assertEqual(n2, 10)         # 48,000.00 ×10
        self.assertEqual(self.one("SELECT COUNT(amount) FROM obj_transaction")[0],
                         74)             # 全部金额非空

    def test_d03_scientific_notation_parsed_no_silent(self):
        """D-03（009 边界）：科学计数法 '1.28e3' 被 DECIMAL TRY_CAST 解析为
        1280.0×3（DuckDB 边界行为，非 NULL）；失败诊断只挂 date，不静默。"""
        n = self.one("SELECT COUNT(*) FROM obj_transaction "
                     "WHERE amount = 1280.0")[0]
        self.assertEqual(n, 3)
        casts = [r for r in self.diag("source_value_cast_failed")
                 if str(r.get("reason", "")).startswith("obj_transaction.")]
        self.assertTrue(casts, "transaction 应有 date CAST 失败诊断（不静默）")
        self.assertTrue(all(".amount" not in str(r.get("reason")) for r in casts),
                        f"amount 不得有 CAST 失败诊断: {casts}")

    def test_d04_masked_card_rejected_counted(self):
        """D-04（010 AC-1/2，N-A 降级口径）：6 行星号卡号被 reject_if 剔除，
        clean_stats 记录规则/行数/脱敏样本；本包 reject 剔行不落 build_quarantine
        （quarantine 声明仅挂 on_cast_error），按文档 N-A 降级为计数核对。"""
        e = self.clean_entry("transaction", "card")
        self.assertIsNotNone(e)
        self.assertEqual(e["rule"], "reject_if:contains_mask")
        self.assertEqual(e["dropped_rows"], 6)
        self.assertEqual(e["rows_before"], 120)
        self.assertTrue(e["sample_masked"])
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_transaction WHERE card LIKE '%*%'")[0], 0)
        self.assertEqual(self.stats["quarantine"], [])

    def test_d05_old_calls_missing_column_degraded(self):
        """D-05（B5-01）：旧版通话缺'对端'列 → 降级类型化 NULL，不崩且留痕。"""
        self.assertEqual(self.one(
            "SELECT COUNT(*), COUNT(callee_raw) FROM obj_call_old")[0:2],
            (10, 0))
        self.assertTrue(any("对端" in d for d in self.stats["degraded"]))
        self.assertGreaterEqual(self.rh.summary()["by_kind"]
                                .get("source_column_missing", 0), 1)

    def test_d06_missing_sensitive_policy_hard_fail(self):
        """D-06（AD-5/002 AC-4）：policies 漏声明 id_card 遮蔽 → 装载前硬失败
        并提示补声明；声明在位（现包）则正常装载。"""
        def remove_person_idcard_policy(pack_dir: Path):
            pf = pack_dir / "policies.json"
            data = json.loads(pf.read_text(encoding="utf-8"))
            data["property_policies"] = [
                p for p in data["property_policies"]
                if not (p.get("object") == "person"
                        and p.get("property") == "id_card")]
            pf.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")

        tmp, name = self._temp_pack(remove_person_idcard_policy)
        orig = ol.PACK_ROOT
        try:
            ol.PACK_ROOT = tmp
            with self.assertRaises(ValueError) as cm:
                load_pack(name)
            msg = str(cm.exception)
            self.assertIn("property_policies", msg)
            self.assertIn("遮蔽", msg)
            self.assertIn("id_card", msg)
        finally:
            ol.PACK_ROOT = orig
            shutil.rmtree(tmp, ignore_errors=True)
        # 补声明后在位包正常装载（对照组）
        self.assertIn("person", load_pack("reqd_case").object_bindings)


class TestCleanPipeline(ReqdCaseBase):
    """清洗管道组 D-07~D-12。"""

    def test_d07_phone_three_formats_normalized(self):
        """D-07（006 AC-1）：三种脏电话格式有序归一为同一 11 位号。"""
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_call WHERE phone_raw = '13800138000'")[0],
            9)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_call WHERE phone_raw IN "
            "('138 0013 8000', '+86 138-0013-8000', '008613800138000')")[0], 0)

    def test_d08_name_variant_same_surrogate_key(self):
        """D-08（005 AC-1）：'李  强' 经 despace 改值与 '李强' 同代理键（合并）。"""
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_person WHERE raw_name = '李强'")[0], 1)
        row = self.one("SELECT phone, id_card FROM obj_person "
                       "WHERE raw_name = '李强'")
        self.assertEqual(row, ("13800001001", "33010219880512431X"))
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_person "
            "WHERE raw_name LIKE '% %' OR raw_name LIKE '%　%'")[0], 0)

    def test_d09_two_liqiang_strong_evidence_split(self):
        """D-09（R-1 红线）：两'李强'强证据全异 → 不合并、强制 needs_review。"""
        rows = [{"name": r["姓名"], "phone": r["电话"],
                 "id_card": r["身份证号"], "source_row_id": f"人员身份_电诈.{i}"}
                for i, r in enumerate(self.fx["tables"]["人员身份_电诈"]["rows"])]
        er = EntityResolver()
        er.ingest(rows)
        clusters = er.resolve()
        liq = [c for c in clusters
               if (normalize_person_name(c.canonical_name)
                   if normalize_person_name else c.canonical_name) == "李强"]
        self.assertEqual(len(liq), 2, "同名强证据互斥必须拆两簇")
        self.assertTrue(all(c.needs_review for c in liq))
        self.assertEqual(len({c.entity_id for c in liq}), 2)
        self.assertNotIn("李强", er.mapping())

    def test_d10_org_tokens_dropped_with_stats_and_warning(self):
        """D-10（008 AC-1/2/4）：电诈词表剔除 40 行组织名，clean_stats 留痕，
        33%>30% 阈值升 clean_drop_rate 告警。"""
        acc = self.clean_entry("account", "raw_name")
        txn = self.clean_entry("transaction", "to_raw")
        self.assertEqual(acc["dropped_rows"], 40)
        self.assertEqual(txn["dropped_rows"], 40)
        self.assertEqual(acc["rule"], "exclude_org_tokens")
        self.assertTrue(acc["sample_masked"])
        # 40/120 = 33% 超阈值 → 告警诊断
        self.assertGreaterEqual(self.rh.summary()["by_kind"]
                                .get("clean_drop_rate", 0), 3)

    def test_d11_wangyinhang_not_false_dropped(self):
        """D-11（005 AC-5 陷阱）：'王银行' 在电诈词表（replace）下不得被误剔。"""
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_person WHERE raw_name = '王银行'")[0], 1)

    def test_d12_profile_exposes_clean_before_after(self):
        """D-12（008 AC-6）：画像页逐属性可见清洗前后行数。"""
        by_key = {(e["obj"], e["prop"]): e for e in self.rep["l1_l2"]}
        for obj, prop, before, dropped in (
                ("account", "raw_name", 112, 40),
                ("transaction", "card", 120, 6),
                ("transaction", "to_raw", 120, 40)):
            e = by_key.get((obj, prop))
            self.assertIsNotNone(e, f"{obj}.{prop} 无画像条目")
            self.assertIn("clean", e)
            self.assertEqual(e["clean"]["rows_before"], before)
            self.assertEqual(e["clean"]["dropped_rows"], dropped)


class TestQualityScan(ReqdCaseBase):
    """质量扫描组 D-13~D-18。"""

    def test_d13_bad_checksum_idcards_all_detected(self):
        """D-13（016 AC-1/7）：6 行校验位错身份证逐行检出，代理键可下钻。"""
        p = self.comp_prop("person.id_card")
        self.assertIsNotNone(p)
        self.assertEqual(p["violations"], 12)   # 6 错校验位 + 3 短位双计 + 3 format
        self.assertEqual(p["codes"], {"checksum_failed": 9,
                                      "format_mismatch": 3})
        keys = {r.get("key") for r in self.diag("compliance_violation",
                                                obj="person", prop="id_card")}
        # 12 条违规记录 → 9 个唯一代理键（3 行短位身份证 checksum+format 双计）
        self.assertEqual(len(keys), 9)
        bad6 = [r[0] for r in self.rows(
            "SELECT person_id FROM obj_person WHERE raw_name IN "
            "('尤志远','卞国强','涂长海','詹淑英','廖春梅','蒲建军')")]
        self.assertTrue(set(bad6) <= keys, "6 个校验位错行必须逐行落账可下钻")

    def test_d14_invalid_phone_segment_detected(self):
        """D-14（016 AC-2）：12 位非法号段 2 行检出。"""
        p = self.comp_prop("call.phone_raw")
        self.assertEqual(p["codes"], {"format_mismatch": 2})

    def test_d15_negative_amount_detected(self):
        """D-15（016 AC-3）：5 行负金额经 DE_AMOUNT range.min 检出。"""
        p = self.comp_prop("transaction.amount")
        self.assertEqual(p["codes"], {"range_violation": 5})

    def test_d16_future_date_detected(self):
        """D-16（016 AC-4）：2 行未来日期（2026-12-31）经 DE_DATE range.max 检出。"""
        p = self.comp_prop("transaction.date")
        self.assertEqual(p["codes"], {"range_violation": 2})

    def test_d17_off_enum_case_type_detected(self):
        """D-17（016 AC-5/003 AC-3）：'杀猪盘' 不在 DE_CASE_TYPE 代码表 → 检出。"""
        p = self.comp_prop("case_ledger.case_type")
        self.assertEqual(p["codes"], {"enum_unknown": 1})

    def test_d18_lowercase_x_idcard_no_false_positive(self):
        """D-18（016 AC-8 陷阱）：合法小写 x 结尾身份证不得误报。"""
        keys = {r.get("key") for r in self.diag("compliance_violation",
                                                obj="person", prop="id_card")}
        traps = [r[0] for r in self.rows(
            "SELECT person_id FROM obj_person WHERE raw_name IN "
            "('桂文革','彭少芬')")]
        self.assertFalse(set(traps) & keys, "合法 x/X 结尾样本不得进违规账")


class TestCompositeColumn(ReqdCaseBase):
    """复合列组 D-19/D-20（013）。"""

    def test_d19_source_sql_split_path(self):
        """D-19（013 AC-1）：source_sql 上游拆分路径声明合法且物化正确。"""
        pack = load_pack("reqd_case")
        b = pack.object_bindings["cp_split"]
        self.assertTrue(b.source_sql)
        self.assertIn("split_part", b.source_sql)
        self.assertEqual(self.one("SELECT COUNT(*) FROM obj_cp_split")[0], 15)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_cp_split "
            "WHERE cp_name IS NULL OR cp_idcard IS NULL")[0], 0)
        # 拆出的身份证全部合规（0 违规）
        p = self.comp_prop("cp_split.cp_idcard")
        self.assertTrue(p is None or p["violations"] == 0)

    def test_d20_composite_whole_fallback_and_key_reject(self):
        """D-20（013 AC-3/AC-5）：composite 整列降级路径可用；composite 属性
        不得作 name_property/关联键（负路径硬失败）。"""
        pack = load_pack("reqd_case")
        o = next(x for x in pack.objects if x.name == "cp_whole")
        self.assertEqual(o.composite_props, ("cp_info",))
        self.assertNotEqual(o.name_property, "cp_info")
        self.assertEqual(self.one("SELECT COUNT(*) FROM obj_cp_whole")[0], 15)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM obj_cp_whole WHERE cp_info LIKE '%|%'")[0], 15)

        def set_composite_name_property(pack_dir: Path):
            of = pack_dir / "objects.json"
            data = json.loads(of.read_text(encoding="utf-8"))
            for obj in data["objects"]:
                if obj["name"] == "cp_whole":
                    obj["name_property"] = "cp_info"
            of.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")

        tmp, name = self._temp_pack(set_composite_name_property)
        orig = ol.PACK_ROOT
        try:
            ol.PACK_ROOT = tmp
            with self.assertRaises(ValueError) as cm:
                load_pack(name)
            self.assertIn("composite", str(cm.exception))
        finally:
            ol.PACK_ROOT = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestGovernance(ReqdCaseBase):
    """治理贯通组 D-21~D-24。"""

    def test_d21_compliance_rate_scored_in_profile(self):
        """D-21（017 AC-1/3）：合规违规率进 L5 质量分，compliance_ 前缀与统计
        扣分可区分，warn/block 两档齐现，五要素齐备。"""
        deds = [d for d in self.rep["l5"]["deductions"]
                if str(d.get("code", "")).startswith("compliance_")]
        codes = {d["code"] for d in deds}
        self.assertIn("compliance_violation", codes)
        self.assertIn("compliance_violation_high", codes)
        block = next(d for d in deds if d["code"] == "compliance_violation_high")
        self.assertEqual((block["severity"], block["points"]), ("block", -20))
        for d in deds:
            for k in ("scope", "ref", "code", "reason", "severity"):
                self.assertIn(k, d)

    def test_d22_recommend_de_idcard_draft_only(self):
        """D-22（021 AC-1/2/3）：干净列推荐 DE_IDCARD（high 置信）；脏混装列给
        上游拆分提示；推荐只读不写（永不自动生效）。"""
        els = load_data_elements("reqd_case")
        snapshot = json.dumps(els, ensure_ascii=False, sort_keys=True)
        good = [r[0] for r in self.rows(
            "SELECT cp_idcard FROM obj_cp_split WHERE cp_idcard IS NOT NULL")]
        rec = recommend_for_column("身份证号", good, els)
        self.assertTrue(rec["recommendations"])
        hit = next(r for r in rec["recommendations"]
                   if r["data_element"] == "DE_IDCARD")
        self.assertEqual(hit["match_by"], "format")
        self.assertEqual(hit["confidence"], "high")
        self.assertFalse(hit["needs_confirmation"])
        self.assertIsNone(rec["split_hint"])
        # 脏混装列（17/18 位混杂）→ 不给单一数据元，只给拆分提示
        dirty = [r["身份证号"]
                 for r in self.fx["tables"]["人员身份_电诈"]["rows"]]
        rec_dirty = recommend_for_column("身份证号", dirty, els)
        self.assertEqual(rec_dirty["recommendations"], [])
        self.assertTrue(rec_dirty["split_hint"])
        self.assertTrue(rec_dirty["needs_confirmation"])
        # 红线：推荐纯只读，数据元表零变更（不自动生效）
        self.assertEqual(snapshot,
                         json.dumps(load_data_elements("reqd_case"),
                                    ensure_ascii=False, sort_keys=True))

    def test_d23_health_section_four_quality_kinds(self):
        """D-23（022 AC-1/5）：健康度四类质量结果齐现且 source 可区分。"""
        by_kind = self.rh.summary()["by_kind"]
        for kind, least in (("source_value_cast_failed", 1),
                            ("source_column_missing", 1),
                            ("clean_drop_rate", 3),
                            ("sensitive_column_suspect", 4),
                            ("compliance_violation", 22)):
            self.assertGreaterEqual(by_kind.get(kind, 0), least, kind)
        by_source = self.rh.summary()["by_source"]
        self.assertTrue({"build_ontology", "compliance", "sensitive_scan"}
                        <= set(by_source), f"source 不可区分: {by_source}")

    def test_d24_no_hardcoded_person_names_in_enum_space(self):
        """D-24（003 AC-2）：enum_space 派生结果不含任何人名（扫描器零命中）。"""
        self.assertEqual(scan_pack_enum(PACK_DIR), [])
        space_text = (PACK_DIR / "enum_space.json").read_text(encoding="utf-8")
        for nm in set(SUBJECTS) | set(PERSONAL_POOL):
            self.assertNotIn(nm, space_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
