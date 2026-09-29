"""物品源表预装配测试（prep_item_registry：L3 原始三表 → 宽表两表）。

命名沿用项目惯例：test_<模块>_<行为>__<条件>。
unittest.TestCase 风格——run_tests.py GROUPS 经 `python -m unittest` 发现，
模块级 pytest 函数不会被收集（0 用例的"绿"是假绿，见 AGENTS.md 验收口径）。

反向验证清单（改动本文件时必须重跑）：
  M1 无凭证键退化为空串           —— 所有赃物合并成一件（D4 失效）
  P1 digest 与 core.item 口径分叉 —— 语义层与画布层对不上同一件物品
  P2 无凭证无特征行静默合成一件   —— D4 失效，赃物坍缩
  P3 持有产物带凭证明文           —— R14 失守，明文进语义层
  P4 日期/时刻混进同一列          —— 时刻级套牌信号被截断丢档
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.item import build_identifier, digest_of, primary_digest
from scripts import prep_item_registry as prep


# ------------------------------------------------------------------ #
# 夹具行（与 data/gen_sim_item.py 的演示形态同构，但更小）
# ------------------------------------------------------------------ #

ITEM_ROWS = [
    {"物品名称": "黑色别克轿车", "物品类型": "车辆", "凭证号": "浙A12345",
     "所在地": "浙江省杭州市拱墅区莫干山路111号", "特征描述": "",
     "经度": 120.1501, "纬度": 30.3147},
    {"物品名称": "黑色双肩包", "物品类型": "涉案物品", "凭证号": "",
     "所在地": "", "特征描述": "黑色双肩包，内有账本", "经度": None, "纬度": None},
    {"物品名称": "银色U盘", "物品类型": "涉案物品", "凭证号": "",
     "所在地": "", "特征描述": "银色U盘，容量64G", "经度": None, "纬度": None},
]

TRACK_ROWS = [
    {"物品名称": "黑色别克轿车", "凭证号": "浙A12345", "日期": "2021-09-08",
     "时刻": "2021-09-08 14:35:00", "地点": "浙江省杭州市拱墅区莫干山路111号",
     "经度": 120.1501, "纬度": 30.3147},
    {"物品名称": "不存在的车", "凭证号": "浙X99999", "日期": "2021-09-08",
     "时刻": "", "地点": "", "经度": None, "纬度": None},
]

HOLD_ROWS = [
    {"物品名称": "黑色别克轿车", "凭证号": "浙A12345", "持有人": "王五",
     "开始日期": "2019-03-01", "结束日期": "2021-06-30", "登记序号": 1},
    # 时刻级：卡口抓拍，套牌信号依赖这一层
    {"物品名称": "黑色别克轿车", "凭证号": "浙A12345", "持有人": "王五",
     "开始日期": "2021-09-15 14:20:00", "结束日期": "2021-09-15 16:40:00",
     "登记序号": 5},
    # 错序信号：序号 0 时间最晚
    {"物品名称": "黑色别克轿车", "凭证号": "浙A12345", "持有人": "李志强",
     "开始日期": "2021-09-02", "结束日期": "", "登记序号": 0},
    # 无凭证按名称关联
    {"物品名称": "黑色双肩包", "凭证号": "", "持有人": "张卫国",
     "开始日期": "2020-03-10", "结束日期": "", "登记序号": 1},
    # 起止皆空：区间不可判定
    {"物品名称": "银色U盘", "凭证号": "", "持有人": "李志强",
     "开始日期": "", "结束日期": "", "登记序号": 1},
    # 指向未登记物品：如实跳过
    {"物品名称": "不存在的车", "凭证号": "浙X99999", "持有人": "张三",
     "开始日期": "2021-01-01", "结束日期": "", "登记序号": 1},
]


def _prep(items=None, holds=None, tracks=None):
    wide, index, sk1 = prep.prep_items(
        list(ITEM_ROWS if items is None else items),
        list(TRACK_ROWS if tracks is None else tracks))
    hold_rows, sk2 = prep.prep_holds(
        list(HOLD_ROWS if holds is None else holds), index)
    return wide, hold_rows, sk1 + sk2


# ------------------------------------------------------------------ #
# 关联键（M1：持有与台账能对上同一件物品的全部前提）
# ------------------------------------------------------------------ #

class TestCredKey(unittest.TestCase):
    def test_cred_key__credentialed_uses_credential(self):
        """有凭证按凭证：同码即同一件，不因名称不同分列。"""
        self.assertEqual(prep._cred_key("浙A12345", "黑色别克轿车"),
                         prep._cred_key("浙A12345", "别的名字"))

    def test_cred_key__no_credential_uses_name_not_empty(self):
        """M1：无凭证键统一返回空串会让所有赃物坍缩成一件（D4 失效）。"""
        self.assertNotEqual(prep._cred_key("", "黑色双肩包"),
                            prep._cred_key("", "银色U盘"))


# ------------------------------------------------------------------ #
# digest 口径（P1：与 core/item.py 对拍，绝不许第二口径）
# ------------------------------------------------------------------ #

class TestPrepDigest(unittest.TestCase):
    def test_prep_digest__matches_core_item(self):
        """宽表标识摘要必须等于 core.item 同输入产出——这是两轨同源的根基。"""
        wide, _, _ = _prep()
        car = [r for r in wide if r["类型"] == "vehicle"][0]
        expect_ids = [build_identifier("plate", "浙A12345")]
        self.assertEqual(car["标识摘要"], primary_digest(expect_ids))
        self.assertEqual(car["标识摘要"], digest_of("浙A12345", kind="plate"))
        ids = json.loads(car["标识"])
        self.assertEqual(ids[0]["kind"], "plate")
        self.assertNotIn("value", ids[0], "identifiers 不得带明文字段（R14）")
        self.assertEqual(ids[0]["value_digest"], car["标识摘要"])

    def test_prep_uncredentialed__separate_entities(self):
        """两件无凭证赃物：descriptors 摘要各异，天然各成实体（D4）。"""
        wide, _, _ = _prep()
        goods = [r for r in wide if r["类型"] == "goods_other"]
        self.assertEqual(len(goods), 2)
        self.assertNotEqual(goods[0]["标识摘要"], goods[1]["标识摘要"])
        self.assertTrue(goods[0]["标识摘要"] and goods[1]["标识摘要"])
        # 与 core.item 口径对拍：descriptors json 摘要
        bag = [g for g in goods if g["名称"] == "黑色双肩包"][0]
        self.assertEqual(bag["标识摘要"], primary_digest(
            [], {"特征描述": "黑色双肩包，内有账本"}))

    def test_prep_reject__no_credential_no_descriptor(self):
        """R-4/D4：无凭证且无特征 → 跳过并给原因，绝不静默成行。"""
        bad = [{"物品名称": "无主物", "物品类型": "涉案物品", "凭证号": "",
                "所在地": "", "特征描述": "", "经度": None, "纬度": None}]
        wide, _, skipped = _prep(items=bad)
        self.assertEqual(wide, [])
        self.assertTrue(any("无凭证且无特征" in s for s in skipped), skipped)

    def test_prep_reject__unknown_type(self):
        """未知物品类型：跳过并给原因，不猜（猜错会把车存成别的凭证种类）。"""
        bad = [{"物品名称": "神秘物", "物品类型": "核武器", "凭证号": "X-1",
                "所在地": "", "特征描述": "", "经度": None, "纬度": None}]
        wide, _, skipped = _prep(items=bad)
        self.assertEqual(wide, [])
        self.assertTrue(any("未知物品类型" in s for s in skipped), skipped)


# ------------------------------------------------------------------ #
# 持有装配（P3/P4）
# ------------------------------------------------------------------ #

class TestPrepHolds(unittest.TestCase):
    def test_holds__no_credential_plaintext_in_output(self):
        """P3：持有产物列集无凭证号——R14 下关联键只有 (类型, 标识摘要)。"""
        _, holds, _ = _prep()
        self.assertTrue(holds, "夹具应产出持有行")
        for r in holds:
            self.assertNotIn("凭证号", r, f"产物带凭证明文列：{r}")
            self.assertLessEqual(set(r), set(prep.HOLD_COLUMNS),
                                 set(r) - set(prep.HOLD_COLUMNS))

    def test_holds__join_key_matches_registry_digest(self):
        """持有行的 (类型, 标识摘要) 必须能在宽表找到同键物品——等值 JOIN 前提。"""
        wide, holds, _ = _prep()
        reg = {(r["类型"], r["标识摘要"]) for r in wide}
        hit = [r for r in holds if (r["类型"], r["标识摘要"]) in reg]
        # 6 条夹具持有里只有「不存在的车」应跳过
        self.assertEqual(len(hit), len(holds))
        self.assertEqual(len(holds), 5)

    def test_holds__uncredentialed_matches_by_name(self):
        """无凭证持有按物品名称关联（特征描述只在台账里，按描述做键关联不上）。"""
        _, holds, _ = _prep()
        bag = [r for r in holds if r["物品名称"] == "黑色双肩包"]
        self.assertEqual(len(bag), 1)
        self.assertEqual(bag[0]["类型"], "goods_other")
        self.assertTrue(bag[0]["标识摘要"])

    def test_holds__unknown_item_skipped_with_reason(self):
        """持有流水指向台账外物品：如实跳过，不猜归属。"""
        _, holds, skipped = _prep()
        self.assertFalse([r for r in holds if r["物品名称"] == "不存在的车"])
        self.assertTrue(any("未在台账中登记" in s for s in skipped), skipped)

    def test_holds__order_zero_not_swallowed(self):
        """登记序号 0 是合法值（用 `or` 取值会短路成 None，链序错乱）。"""
        _, holds, _ = _prep()
        self.assertIn(0, [r["登记序号"] for r in holds])

    def test_holds__datetime_split_by_precision(self):
        """P4：时刻级落时刻列、日期级落日期列、缺失留 None——不填哨兵不补零。"""
        _, holds, _ = _prep()
        by_order = {r["登记序号"]: r for r in holds
                    if r["物品名称"] == "黑色别克轿车"}
        # 序号 1：日期级
        self.assertEqual(by_order[1]["开始日期"], "2019-03-01")
        self.assertIsNone(by_order[1]["开始时刻"])
        # 序号 5：时刻级（套牌信号依赖）
        self.assertEqual(by_order[5]["开始时刻"], "2021-09-15 14:20:00")
        self.assertIsNone(by_order[5]["开始日期"])
        # 序号 0：开放终点
        self.assertIsNone(by_order[0]["结束日期"])
        self.assertIsNone(by_order[0]["结束时刻"])
        # 银色U盘：起止皆空 → 全 None（unknownRange 前提，绝不填哨兵）
        ud = [r for r in holds if r["物品名称"] == "银色U盘"][0]
        self.assertTrue(all(ud[c] is None for c in
                            ("开始日期", "结束日期", "开始时刻", "结束时刻")))


# ------------------------------------------------------------------ #
# 轨迹（R-6/R-7）
# ------------------------------------------------------------------ #

class TestPrepTrack(unittest.TestCase):
    def test_track__serialized_into_registry_row(self):
        """轨迹序列化为宽表行 json 属性；未登记物品的轨迹跳过并给原因。"""
        wide, _, skipped = _prep()
        car = [r for r in wide if r["类型"] == "vehicle"][0]
        pts = json.loads(car["轨迹"])
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["time_precision"], "minute", pts[0])
        self.assertIs(pts[0]["mappable"], True)
        self.assertTrue(any("未在物品台账中登记" in s for s in skipped), skipped)
        # 无轨迹物品：空列表（json "[]"），不留 NULL 让下游 json.loads 崩
        bag = [r for r in wide if r["名称"] == "黑色双肩包"][0]
        self.assertEqual(json.loads(bag["轨迹"]), [])

    def test_track_point__hour_precision_not_minute(self):
        """整点派生 hour 档（R-7）：判不出『同时』，绝不提精度。"""
        r = {"物品名称": "x", "凭证号": "", "日期": "",
             "时刻": "2021-09-08 14:00:00", "地点": "", "经度": None, "纬度": None}
        p = prep._track_point(r)
        self.assertEqual(p["time_precision"], "hour")


# ------------------------------------------------------------------ #
# run() 端到端（临时目录隔离，不碰真实 data/）
# ------------------------------------------------------------------ #

def _write_source_parquet(d: Path) -> None:
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE items(物品名称 VARCHAR, 物品类型 VARCHAR,"
                    " 凭证号 VARCHAR, 所在地 VARCHAR, 特征描述 VARCHAR,"
                    " 经度 DOUBLE, 纬度 DOUBLE)")
        con.executemany(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?)",
            [[r.get(c) for c in ("物品名称", "物品类型", "凭证号", "所在地",
                                 "特征描述", "经度", "纬度")] for r in ITEM_ROWS])
        con.execute(f"COPY items TO '{(d / prep.ITEM_TABLE).as_posix()}'"
                    " (FORMAT PARQUET)")
        con.execute("CREATE TABLE holds(物品名称 VARCHAR, 凭证号 VARCHAR,"
                    " 持有人 VARCHAR, 开始日期 VARCHAR, 结束日期 VARCHAR,"
                    " 登记序号 INTEGER)")
        con.executemany(
            "INSERT INTO holds VALUES (?,?,?,?,?,?)",
            [[r.get(c) for c in ("物品名称", "凭证号", "持有人", "开始日期",
                                 "结束日期", "登记序号")] for r in HOLD_ROWS])
        con.execute(f"COPY holds TO '{(d / prep.HOLD_TABLE).as_posix()}'"
                    " (FORMAT PARQUET)")
    finally:
        con.close()


class TestPrepRun(unittest.TestCase):
    def test_run__end_to_end(self):
        """run()：三表进两表出；产物列型正确（登记序号 BIGINT、经纬度 DOUBLE）。"""
        import duckdb
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_source_parquet(d)
            stats = prep.run(d)
            self.assertEqual(stats["items"], 3)
            self.assertEqual(stats["holds"], 5)      # 「不存在的车」被跳过
            self.assertEqual(stats["track_points"], 0)  # 未提供轨迹表 → 0，不报错
            self.assertTrue(any("未在台账中登记" in s for s in stats["skipped"]))

            con = duckdb.connect()
            try:
                cols = {r[0]: r[1] for r in con.execute(
                    f"DESCRIBE SELECT * FROM read_parquet("
                    f"'{(d / prep.OUT_HOLDS).as_posix()}')").fetchall()}
                self.assertEqual(cols["登记序号"], "BIGINT", cols)
                # 产物无凭证明文列（R14 落点）
                self.assertNotIn("凭证号", cols)
                reg_cols = {r[0]: r[1] for r in con.execute(
                    f"DESCRIBE SELECT * FROM read_parquet("
                    f"'{(d / prep.OUT_REGISTRY).as_posix()}')").fetchall()}
                self.assertEqual(reg_cols["纬度"], "DOUBLE")
            finally:
                con.close()

    def test_run__missing_sources_reports_reason(self):
        """缺源表：返回原因指向数据源，不静默当『查无物品』。"""
        with tempfile.TemporaryDirectory() as td:
            stats = prep.run(Path(td))
        self.assertEqual(stats["items"], 0)
        self.assertEqual(stats["holds"], 0)
        self.assertTrue(any("数据源" in s for s in stats["skipped"]), stats)


if __name__ == "__main__":
    unittest.main()
