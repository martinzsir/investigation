"""
tests/test_row_uri.py — REQ-008 Content-addressable 溯源行 URI

  AC1 make_row_uri → parse_row_uri 往返无损；
  AC2 resolve 取回与 obj_*.source_rows 同源一致的原始行；
  AC3 非法 URI（缺段/空段/非 hex）抛 MalformedUriError；
  AC4 同一原始行两次生成的 URI 相同（内容寻址稳定）；
  AC5 跨版本 resolve 返回对应版本的行（旧 URI 取旧内容、版本归属不混）；
  附加：增量版本索引继承未受影响数据集（镜像"未变类型不重写"）。
"""
import unittest

import duckdb

from core.row_uri import (
    BOOTSTRAP_PARTITION, MalformedUriError, RowNotFoundError,
    make_row_uri, parse_row_uri, resolve_row_uri, row_id_for,
    snapshot_source_rows,
)
from core.ontology_version import current_version
from core.rebuild_planner import plan_from_seeds
from core.ontology import build_ontology, materialize_changed
from core import Store

V1 = "a" * 32   # 合法 build_id 形态（uuid hex 32 位）
V2 = "b" * 32
RID = "0123456789abcdef"


def make_store() -> Store:
    """内存库夹具：七张中文源表 + baseline 行 + 语义层全量构建。"""
    s = Store(db_path=":memory:")
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    s.execute(
        "INSERT INTO 银行流水 VALUES ('张卫国','现金存入',100000,'2021-09-28'),"
        "('宏业建设','A建材',4600000,'2021-10-01')")
    s.execute("INSERT INTO 通话记录 VALUES "
              "('张卫国','李志强','2021-10-01',3),('张卫国','李志强','2021-09-30',5)")
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设','李志强','存续',NULL)")
    s.execute("INSERT INTO 轨迹出行 VALUES "
              "('2021-10-02','张卫国','项目B'),('2021-09-30','张卫国','项目A'),"
              "('2021-10-01','李志强','项目A')")
    s.execute("INSERT INTO 招投标档案 VALUES ('项目A','宏业建设','2021-10-01','张卫国')")
    s.execute("INSERT INTO 公开OSINT VALUES ('张卫国','分管招投标','2019-03-01','政府官网')")
    s.execute("INSERT INTO 举报材料 VALUES ('2022-01-10','经济类','张卫国','匿名','x')")
    build_ontology(s.conn)
    return s


class TestRowUriSyntax(unittest.TestCase):
    """AC1/AC3/AC4：纯函数层。"""

    def test_ac1_roundtrip(self):
        uri = make_row_uri("银行流水", V1, "银行流水_2024Q1", RID)
        self.assertEqual(uri, f"银行流水@{V1}#银行流水_2024Q1/{RID}")
        ref = parse_row_uri(uri)
        self.assertEqual((ref.dataset, ref.version, ref.partition, ref.rowid),
                         ("银行流水", V1, "银行流水_2024Q1", RID))
        self.assertEqual(ref.to_uri(), uri)
        self.assertIsInstance(ref.to_uri(), str)

    def test_ac3_malformed_parse(self):
        bad = [
            "银行流水@abc",                       # 缺 #partition/rowid
            "银行流水@abc#p",                     # 缺 /rowid
            "银行流水@abc#p/",                    # 空 rowid
            "银行流水@abc#/abcd1234abcd1234",     # 空 partition
            "@abc#p/abcd1234abcd1234",            # 空 dataset
            "银行流水#abc#p/abcd1234abcd1234",    # 缺 @
            "银行流水@xyz#p/abcd1234abcd1234",    # version 非 hex
            "银行流水@abc#p/zzzz1234zzzz1234",    # rowid 非 hex
            "银行流水@ab c#p/abcd1234abcd1234",   # version 含空格
            42,                                    # 非字符串
        ]
        for u in bad:
            with self.assertRaises(MalformedUriError, msg=f"应拒绝 {u!r}"):
                parse_row_uri(u)

    def test_ac3_malformed_make(self):
        with self.assertRaises(MalformedUriError):
            make_row_uri("银行@流水", V1, "p", RID)      # dataset 含 @
        with self.assertRaises(MalformedUriError):
            make_row_uri("银行流水", V1, "p/1", RID)     # partition 含 /
        with self.assertRaises(MalformedUriError):
            make_row_uri("", V1, "p", RID)               # 空 dataset
        with self.assertRaises(MalformedUriError):
            make_row_uri("ds", "", "p", RID)             # 空 version
        with self.assertRaises(MalformedUriError):
            make_row_uri("ds", V1, "p", "short")         # rowid 非 hex

    def test_ac4_content_address_stable(self):
        cols = ["主体", "对方", "金额", "日期"]
        row = ("张卫国", "现金存入", 100000, "2021-09-28")
        rid1 = row_id_for("银行流水", cols, row)
        rid2 = row_id_for("银行流水", cols, row)
        self.assertEqual(rid1, rid2)                      # 同内容同址
        self.assertEqual(len(rid1), 16)
        self.assertNotEqual(rid1, row_id_for(
            "银行流水", cols, ("张卫国", "现金存入", 200000, "2021-09-28")))  # 值变址变
        self.assertNotEqual(rid1, row_id_for(
            "通话记录", cols, row))                       # dataset 变址变
        # None 归一：与 source_rows 串同口径（空串）
        rid_none = row_id_for("ds", ["a", "b"], ("x", None))
        self.assertEqual(rid_none, row_id_for("ds", ["a", "b"], ("x", "")))


class TestSnapshotIndexInheritance(unittest.TestCase):
    """附加：增量版本索引继承（(obj_type,dataset) 粒度，不依赖完整语义层）。"""

    def setUp(self):
        self.conn = duckdb.connect(":memory:")

    def test_inheritance_and_replacement(self):
        snapshot_source_rows(
            self.conn, V1,
            [("alpha", "dsA", ["x"], [("1",), ("2",)]),
             ("beta", "dsB", ["y"], [("9",)])],
            partition=BOOTSTRAP_PARTITION)
        snapshot_source_rows(
            self.conn, V2,
            [("alpha", "dsA", ["x"], [("1",), ("3",)])],   # alpha 重算：1 留、2 删、3 增
            partition="incremental",
            prev_build_id=V1)

        def ids(build, obj, ds):
            return {r[0] for r in self.conn.execute(
                "SELECT rowid FROM row_build_index "
                "WHERE build_id=? AND obj_type=? AND dataset=?",
                [build, obj, ds]).fetchall()}

        # beta/dsB 未重算：v2 继承 v1 的行
        self.assertEqual(ids(V2, "beta", "dsB"), ids(V1, "beta", "dsB"))
        # alpha/dsA 替换：v2 不含已消失行 2，含新增行 3
        rid2 = row_id_for("dsA", ["x"], ("2",))
        rid3 = row_id_for("dsA", ["x"], ("3",))
        self.assertNotIn(rid2, ids(V2, "alpha", "dsA"))
        self.assertIn(rid3, ids(V2, "alpha", "dsA"))
        # 内容归档 append-only：旧行 2 的内容仍可取回（AC5 基础）
        content = self.conn.execute(
            "SELECT content FROM row_archive WHERE dataset='dsA' AND rowid=?",
            [rid2]).fetchone()
        self.assertIsNotNone(content)

    def test_same_dataset_multi_object_type(self):
        """同表多对象消费：只重算 alpha 时，同表 beta 的行索引必须保留。"""
        snapshot_source_rows(
            self.conn, V1,
            [("alpha", "dsT", ["x"], [("1",)]),
             ("beta", "dsT", ["y"], [("8",), ("9",)])],
            partition=BOOTSTRAP_PARTITION)
        snapshot_source_rows(
            self.conn, V2,
            [("alpha", "dsT", ["x"], [("2",)])],   # 仅 alpha 重算
            partition="incremental",
            prev_build_id=V1)

        beta_v2 = {r[0] for r in self.conn.execute(
            "SELECT rowid FROM row_build_index WHERE build_id=? "
            "AND obj_type='beta' AND dataset='dsT'", [V2]).fetchall()}
        self.assertEqual(len(beta_v2), 2)   # beta 未重算，两行全部继承
        alpha_v2 = {r[0] for r in self.conn.execute(
            "SELECT rowid FROM row_build_index WHERE build_id=? "
            "AND obj_type='alpha' AND dataset='dsT'", [V2]).fetchall()}
        self.assertEqual(alpha_v2, {row_id_for("dsT", ["x"], ("2",))})


class TestRowUriIntegration(unittest.TestCase):
    """AC2/AC5：完整语义层构建 + 增量重建后的取回。"""

    def setUp(self):
        self.s = make_store()
        self.conn = self.s.conn
        self.ver1 = current_version(self.conn)
        self.assertIsNotNone(self.ver1)

    def _uri_for_dataset_row(self, build_id, dataset, predicate):
        """在指定版本索引中找 content 满足 predicate 的行，返回 (uri, data)。"""
        rids = [r[0] for r in self.conn.execute(
            "SELECT rowid FROM row_build_index WHERE build_id=? AND dataset=?",
            [build_id, dataset]).fetchall()]
        for rid in rids:
            uri = make_row_uri(dataset, build_id,
                               BOOTSTRAP_PARTITION if build_id == self.ver1.build_id
                               else "incremental", rid)
            res = resolve_row_uri(self.conn, uri)
            if predicate(res["data"]):
                return uri, res["data"]
        self.fail(f"版本 {build_id} 的 {dataset} 中未找到匹配行")

    def test_ac2_bootstrap_archive_and_resolve(self):
        # bootstrap 后每个源数据集都有版本索引
        n_datasets = self.conn.execute(
            "SELECT COUNT(DISTINCT dataset) FROM row_build_index "
            "WHERE build_id=?", [self.ver1.build_id]).fetchone()[0]
        self.assertGreaterEqual(n_datasets, 5)

        # 取回张卫国那笔流水，内容与源行一致（列为 binding 输出属性名）
        uri, data = self._uri_for_dataset_row(
            self.ver1.build_id, "银行流水",
            lambda d: d.get("from_raw") == "张卫国" and "amount" in d)
        self.assertEqual(str(data["amount"]), "100000.0")
        self.assertEqual(data["date"], "2021-09-28")
        self.assertEqual(data["to_raw"], "现金存入")

        # 与 obj_transaction.source_rows（JSON 数组串）同源一致
        import json as _json
        sr = self.conn.execute(
            "SELECT source_rows FROM obj_transaction "
            "WHERE source_rows LIKE '%张卫国%' LIMIT 1").fetchone()[0]
        first = _json.loads(sr)[0]
        body = first.split(":", 1)[1]
        fields = dict(kv.split("=", 1) for kv in body.split(","))
        self.assertEqual(str(data["amount"]), fields["amount"])
        self.assertEqual(data["date"], fields["date"])

    def test_ac2_unknown_version_and_row(self):
        with self.assertRaises(RowNotFoundError):
            resolve_row_uri(self.conn,
                            make_row_uri("银行流水", "f" * 32,
                                         BOOTSTRAP_PARTITION, RID))
        with self.assertRaises(RowNotFoundError):
            resolve_row_uri(self.conn,
                            make_row_uri("不存在的表", self.ver1.build_id,
                                         BOOTSTRAP_PARTITION, RID))

    def test_ac5_cross_version_no_mixing(self):
        # 修改源行：张卫国那笔 100000 → 300000，触发增量重建
        self.conn.execute(
            "UPDATE 银行流水 SET 金额=300000 "
            "WHERE 主体='张卫国' AND 日期='2021-09-28'")
        pid = self.conn.execute(
            "SELECT person_id FROM obj_person WHERE raw_name='张卫国'"
        ).fetchone()[0]
        # include_objects 强制 transaction 重算（金额变更不改邻接拓扑，
        # 邻域扩展不必然命中事件对象；分区到达时 direct 对象经同机制纳入）
        plan = plan_from_seeds(self.conn, {"person": {pid}}, reason="seed",
                               include_objects=["transaction"])
        self.assertIn("transaction", plan.affected_objects)
        stats = materialize_changed(self.conn, plan)
        ver2 = current_version(self.conn)
        self.assertNotEqual(ver2.build_id, self.ver1.build_id)
        self.assertIn("row_snapshot", stats)

        # v1 URI：旧内容（100000），永远可取回
        uri_old, data_old = self._uri_for_dataset_row(
            self.ver1.build_id, "银行流水",
            lambda d: d.get("from_raw") == "张卫国"
            and "amount" in d and str(d.get("amount")) == "100000.0")
        self.assertEqual(str(resolve_row_uri(self.conn, uri_old)["data"]["amount"]),
                         "100000.0")

        # v2 URI：新内容（300000）
        uri_new, data_new = self._uri_for_dataset_row(
            ver2.build_id, "银行流水",
            lambda d: d.get("from_raw") == "张卫国" and "amount" in d)
        self.assertEqual(str(data_new["amount"]), "300000.0")
        self.assertNotEqual(uri_old, uri_new)

        # 版本归属不混：旧 rowid 不属于 v2、新 rowid 不属于 v1
        old_rid = parse_row_uri(uri_old).rowid
        new_rid = parse_row_uri(uri_new).rowid
        with self.assertRaises(RowNotFoundError):
            resolve_row_uri(self.conn, make_row_uri(
                "银行流水", ver2.build_id, "incremental", old_rid))
        with self.assertRaises(RowNotFoundError):
            resolve_row_uri(self.conn, make_row_uri(
                "银行流水", self.ver1.build_id, BOOTSTRAP_PARTITION, new_rid))

        # 全量重建后旧版本 URI 仍可 resolve（版本时钟保留历史 build）
        build_ontology(self.conn)
        self.assertEqual(
            str(resolve_row_uri(self.conn, uri_old)["data"]["amount"]),
            "100000.0")


if __name__ == "__main__":
    unittest.main()
