"""数据元驱动的数据接入全链路回归（default 包）。

覆盖：
  1. 类型层挂数据元（transaction 金额/日期/币种 + person_identity 五要素）；
  2. bindings transform 把千分位金额/中文日期在编译期抢救回合法值（非 string
     属性 TRY_CAST 前最后一道），不可救值仍 NULL 并计入 stats["dirty"]；
  3. string 属性走 py 层 DE clean_rule：身份证 despace、手机 strip_cc/digits_only；
  4. 合规扫描四类违规码（format_mismatch/checksum_failed/range_violation/enum_unknown）；
  5. person_identity 为 optional 源：主案件包缺该表时优雅跳过（不中断 build）；
  6. 属性级敏感遮蔽：见习读 person_identity.id_card 部分遮蔽，主办见原文；
  7. 五格式接入夹具（data/test/ingest/，由 scripts/gen_ingest_fixtures.py 生成）：
     read_table 全解析 + analyze_source 自动选表/数据元推荐 + 别名列人工映射提示。
"""
import unittest
from pathlib import Path

import duckdb

from core.ontology import build_ontology
from core.ontology_loader import load_pack
from core.gateway import OntologyReadGateway
from core import compliance
from core.access import AccessContext
from core.policy import PolicyEngine
from core.run_health import RunHealth

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "data" / "test" / "ingest"

# 与 test_dirty_date 同口径：person 原始 UNION 引用的 7 张基础表（全 VARCHAR）
_BASE_DDL = [
    ('CREATE TABLE "银行流水" ("主体" VARCHAR, "对方" VARCHAR, '
     '"金额" VARCHAR, "日期" VARCHAR, "币种" VARCHAR)'),
    ('CREATE TABLE "通话记录" ("主体" VARCHAR, "对端" VARCHAR, '
     '"日期" VARCHAR, "次数" VARCHAR)'),
    ('CREATE TABLE "轨迹出行" ("主体" VARCHAR, "地点" VARCHAR, "日期" VARCHAR)'),
    ('CREATE TABLE "公开OSINT" ("主体" VARCHAR, "公开信息" VARCHAR, '
     '"发布日期" VARCHAR, "来源" VARCHAR, "采集时间" VARCHAR, "保留天数" VARCHAR)'),
    ('CREATE TABLE "举报材料" ("分类" VARCHAR, "举报日期" VARCHAR, '
     '"被举报人" VARCHAR, "举报人" VARCHAR, "内容" VARCHAR)'),
    ('CREATE TABLE "工商信息" ("主体" VARCHAR, "法人" VARCHAR, '
     '"状态" VARCHAR, "关联" VARCHAR)'),
    ('CREATE TABLE "招投标档案" ("项目" VARCHAR, "中标方" VARCHAR, '
     '"中标公示日" VARCHAR, "分管领导" VARCHAR)'),
]
_PERSON_DDL = ('CREATE TABLE "人员信息" ("姓名" VARCHAR, "证件类型" VARCHAR, '
               '"身份证号" VARCHAR, "手机号" VARCHAR, "性别" VARCHAR, '
               '"案件类别" VARCHAR)')


def _build_conn(flow_rows=None, person_rows=None):
    """建 default 包内存库；flow/person 缺省为空表。返回 (conn, stats)。"""
    conn = duckdb.connect(":memory:")
    for ddl in _BASE_DDL:
        conn.execute(ddl)
    if flow_rows:
        conn.executemany(
            'INSERT INTO "银行流水" VALUES (?, ?, ?, ?, ?)', flow_rows)
    if person_rows is not None:
        conn.execute(_PERSON_DDL)
        if person_rows:
            conn.executemany(
                'INSERT INTO "人员信息" VALUES (?, ?, ?, ?, ?, ?)', person_rows)
    return conn, build_ontology(conn, pack="default")


class TestDEWiring(unittest.TestCase):
    """类型层/管道层声明：数据元挂载与 transform 挂钩。"""

    def setUp(self):
        self.spec = load_pack("default")
        self.objs = {o.name: o for o in self.spec.objects}

    def test_transaction_elements(self):
        de = self.objs["transaction"].prop_data_elements
        self.assertEqual(de["amount"], "DE_AMOUNT")
        self.assertEqual(de["date"], "DE_DATE")
        self.assertEqual(de["currency"], "DE_CURRENCY")

    def test_person_identity_elements(self):
        obj = self.objs["person_identity"]
        self.assertEqual(obj.pk, "identity_id")
        self.assertEqual(obj.kind, "entity")
        de = obj.prop_data_elements
        self.assertEqual(de["id_card"], "DE_IDCARD")
        self.assertEqual(de["phone"], "DE_PHONE")
        self.assertEqual(de["gender"], "DE_GENDER")
        self.assertEqual(de["id_type"], "DE_ID_TYPE")
        self.assertEqual(de["case_type"], "DE_CASE_TYPE")

    def test_binding_transform_and_de_clean(self):
        binds = self.spec.object_bindings
        tf = dict(binds["transaction"].transform)
        self.assertEqual(tf["amount"], ("strip_thousands", "strip_currency"))
        self.assertEqual(tf["date"], ("cn_date_norm", "pad_date"))
        # string 属性的 DE clean_rule 走 py 层（prop_de_clean）
        pc = self.objs["person_identity"].prop_de_clean
        self.assertIn("despace", pc["id_card"])
        self.assertIn("digits_only", pc["phone"])

    def test_person_binding_optional(self):
        b = self.spec.object_bindings["person_identity"]
        self.assertTrue(b.optional)


class TestTransformRescue(unittest.TestCase):
    """非 string 属性：transform 在 TRY_CAST 前抢救脏值。"""

    def test_dirty_amount_and_date_rescued(self):
        flow = [
            ("陈学勤", "王润芳", "100,000", "2021年10月1日", "人民币"),
            ("李四", "王润芳", "￥95,000.00", "2021-9-5", "人民币"),
            ("王五", "赵六", "10万元", "2021.10.01", "人民币"),  # 均不可救
        ]
        conn, stats = _build_conn(flow_rows=flow)
        amounts = {r[0] for r in conn.execute(
            "SELECT DISTINCT amount FROM obj_transaction "
            "WHERE amount IS NOT NULL").fetchall()}
        self.assertIn(100000.0, amounts)
        self.assertIn(95000.0, amounts)
        dates = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT date FROM obj_transaction "
            "WHERE date IS NOT NULL").fetchall()}
        self.assertIn("2021-10-01", dates)   # 中文日期
        self.assertIn("2021-09-05", dates)   # 未补零
        # 不可救：'10万元'→amount NULL、'2021.10.01'→date NULL，各计 dirty
        dirty_blob = "\n".join(stats["dirty"])
        self.assertIn("obj_transaction.amount", dirty_blob)
        self.assertIn("obj_transaction.date", dirty_blob)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM obj_transaction WHERE amount IS NULL"
        ).fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM obj_transaction WHERE date IS NULL"
        ).fetchone()[0], 1)


class TestPyLayerClean(unittest.TestCase):
    """string 属性：py 层 DE clean_rule 归一后入语义层。"""

    def test_idcard_phone_normalized(self):
        rows = [
            # 带空格合法身份证（despace 后校验位合法）
            ("温书豪", "居民身份证", "310104 19900101 1233", "13877778888",
             "男", "敲诈勒索"),
            ("岳鸣谦", "居民身份证", "310116199102035678", " 138 0013 8000 ",
             "男", "敲诈勒索"),
            ("阮青山", "居民身份证", "310105198204156784", "+8613755556666",
             "男", "洗钱"),
            (None, "居民身份证", "310104199001019999", "13800000000",
             "男", "洗钱"),   # NULL 姓名：编译期剔除
            ("张卫国", "居民身份证", "310104199003071234", "13800001111",
             "男", "贪污贿赂"),
            ("张卫国", "居民身份证", "310104199003071234", "13800001111",
             "男", "贪污贿赂"),   # 完全重复：折叠
        ]
        conn, stats = _build_conn(person_rows=rows)
        got = {n: (i, p) for n, i, p in conn.execute("""
            SELECT raw_name, id_card, phone FROM obj_person_identity
        """).fetchall()}
        self.assertEqual(got["温书豪"][0], "310104199001011233")
        self.assertEqual(got["岳鸣谦"][1], "13800138000")
        self.assertEqual(got["阮青山"][1], "13755556666")
        # NULL 姓名剔除 + 重复折叠：4 个入语义层
        self.assertEqual(stats["objects"]["person_identity"], 4)
        self.assertTrue(any("person_identity" in d for d in stats["null_identity"]))


class TestComplianceOnDefaultPack(unittest.TestCase):
    """合规扫描：四类违规码齐全（system 网关不经遮蔽）。"""

    def test_violation_codes(self):
        flow = [
            ("某主体", "某对方", "-500", "2024-01-15", "人民币"),       # 金额越下界
            ("某主体", "某对方", "9000", "2099-01-01", "人民币"),       # 日期越上界
            ("某主体", "某对方", "8000", "2024-02-15", "韩元"),         # 币种越枚举
        ]
        persons = [
            ("合法员", "居民身份证", "310104199001011233", "13800002222",
             "男", "洗钱"),
            ("错校验", "居民身份证", "310104199001011234", "13800003333",
             "男", "洗钱"),                                        # checksum_failed
            ("短位证", "居民身份证", "31010419900101123", "13800004444",
             "男", "洗钱"),                                        # format_mismatch
            ("坏手机", "居民身份证", "310116199102035678", "12345",
             "男", "洗钱"),                                        # format_mismatch
            ("越性别", "居民身份证", "310105198204156784", "13800005555",
             "中性", "洗钱"),                                      # enum_unknown
            ("越证件", "驾驶证", "310104199001011233", "13800006666",
             "女", "洗钱"),                                        # 证件类型 enum（身份证号合法）
            ("越案件", "居民身份证", "310116199102035678", "13800007777",
             "女", "盗窃罪"),                                      # 案件类别 enum
        ]
        conn, _ = _build_conn(flow_rows=flow, person_rows=persons)
        gw = OntologyReadGateway(conn, pack="default")
        s = compliance.scan(gw, health=RunHealth(conn, run_id="t"))
        by = s["by_property"]
        self.assertGreaterEqual(s["targets"], 8)
        idc = by["person_identity.id_card"]["codes"]
        self.assertIn("checksum_failed", idc)
        self.assertIn("format_mismatch", idc)
        self.assertIn("format_mismatch",
                      by["person_identity.phone"]["codes"])
        self.assertIn("enum_unknown", by["person_identity.gender"]["codes"])
        self.assertIn("enum_unknown", by["person_identity.id_type"]["codes"])
        self.assertIn("enum_unknown", by["person_identity.case_type"]["codes"])
        self.assertIn("range_violation", by["transaction.amount"]["codes"])
        self.assertIn("range_violation", by["transaction.date"]["codes"])
        self.assertIn("enum_unknown", by["transaction.currency"]["codes"])
        # 身份证违规合计 3：错校验仅 checksum(1) + 短位证 format&checksum(2)
        self.assertEqual(by["person_identity.id_card"]["violations"], 3)

    def test_clean_rescued_values_not_flagged(self):
        """py 层抢救成功的值（空格身份证/86 手机）合规零违规。"""
        persons = [
            ("温书豪", "居民身份证", "310104 19900101 1233", "13877778888",
             "男", "敲诈勒索"),
            ("阮青山", "居民身份证", "310105198204156784", "+8613755556666",
             "男", "洗钱"),
        ]
        conn, _ = _build_conn(person_rows=persons)
        gw = OntologyReadGateway(conn, pack="default")
        s = compliance.scan(gw, health=RunHealth(conn, run_id="t"))
        self.assertEqual(s["violations"], 0)


class TestPersonIdentityOptional(unittest.TestCase):
    """主案件包无人员信息表：optional 整对象跳过，不中断 build。"""

    def test_missing_table_skipped(self):
        conn, stats = _build_conn()   # 7 张基础表，无人员信息
        self.assertIn("obj_person_identity(源表缺失,optional)", stats["skipped"])
        self.assertGreater(stats["objects"].get("transaction", 0), 0)


class TestIdCardMasking(unittest.TestCase):
    """属性级敏感遮蔽（policies.json 声明，未声明 fail-closed）。"""

    def setUp(self):
        self.engine = PolicyEngine("default")

    def test_low_role_masked_host_role_raw(self):
        rows = [{"raw_name": "测试员", "id_card": "31010419900307888X"}]
        low = self.engine.apply_row_masks(
            AccessContext(operator="见习甲", role="见习", clearance=0),
            "person_identity", rows)
        host = self.engine.apply_row_masks(
            AccessContext(operator="主办乙", role="主办", clearance=3),
            "person_identity", rows)
        self.assertIn("*", low[0]["id_card"])
        self.assertNotEqual(low[0]["id_card"], rows[0]["id_card"])
        self.assertEqual(low[0]["raw_name"], "测试员")    # 非敏感列不动
        self.assertEqual(host[0]["id_card"], rows[0]["id_card"])


class TestFiveFormatFixtures(unittest.TestCase):
    """data/test/ingest 五格式夹具：解析 + analyze 推荐 + 别名映射。"""

    def setUp(self):
        if not FIXTURE_DIR.exists():
            self.skipTest("接入夹具未生成：python -m scripts.gen_ingest_fixtures")
        from server.app.ingest_io import read_table, sniff_format, \
            list_sqlite_tables
        from server.app.source_analyze import analyze_source
        self.read_table = read_table
        self.sniff_format = sniff_format
        self.list_sqlite_tables = list_sqlite_tables
        self.analyze_source = analyze_source

    def test_all_fourteen_files_parse(self):
        person_n = flow_n = 0
        for p in sorted(FIXTURE_DIR.iterdir()):
            df = self.read_table(p, self.sniff_format(p.name))
            if p.name.startswith("人员信息"):
                self.assertEqual((len(df), len(df.columns)), (14, 6))
                person_n += 1
            else:
                self.assertEqual((len(df), len(df.columns)), (10, 5))
                flow_n += 1
        self.assertEqual(person_n, 7)
        self.assertEqual(flow_n, 7)

    def test_sqlite_multi_table(self):
        tabs = [t["name"] for t in self.list_sqlite_tables(
            FIXTURE_DIR / "人员信息.sqlite")]
        self.assertEqual(tabs, ["人员信息", "附件清单"])
        tabs = [t["name"] for t in self.list_sqlite_tables(
            FIXTURE_DIR / "银行流水_增量.sqlite")]
        self.assertEqual(tabs, ["流水增量", "手续费明细"])

    def test_analyze_person_each_format(self):
        for fname in ["人员信息.parquet", "人员信息_嵌套.json",
                      "人员信息_分号.csv", "人员信息.sqlite"]:
            df = self.read_table(FIXTURE_DIR / fname,
                                 self.sniff_format(fname))
            a = self.analyze_source(df=df, pack="default",
                                    base_dir=ROOT / "ontology")
            self.assertEqual(a["suggestion"]["target_table"], "人员信息",
                             msg=fname)
            hints = {h["element_id"] for h in a["element_hints"]}
            self.assertEqual(
                hints,
                {"DE_IDCARD", "DE_PHONE", "DE_GENDER", "DE_ID_TYPE",
                 "DE_CASE_TYPE"}, msg=fname)

    def test_analyze_flow_alias_needs_manual_mapping(self):
        df = self.read_table(FIXTURE_DIR / "银行流水_增量.parquet", "parquet")
        a = self.analyze_source(df=df, pack="default",
                                base_dir=ROOT / "ontology")
        sug = a["suggestion"]
        self.assertEqual(sug["target_table"], "银行流水")
        self.assertEqual(set(sug["missing_required"]), {"主体", "对方"})
        hints = {h["element_id"] for h in a["element_hints"]}
        self.assertIn("DE_CURRENCY", hints)


if __name__ == "__main__":
    unittest.main()
