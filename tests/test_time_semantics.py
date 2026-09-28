"""时间精度语义测试：date/timestamp 并存的「可比粒度」口径。

背景（为什么必须有这组测试）
----------------------------
trackpoint 升级后 date（日期级）与 timestamp（带时刻）并存。最大的风险不是
"没有时刻数据"，而是**有时刻列却全 00:00:00 时判出「两人同时同地」**——
这与空间侧「两个不同门牌因共用区县质心被算成 distance_m=0.0」是同一类
伪精确。本组测试守的就是这条红线。
"""

import datetime as dt
import unittest

from core.time_semantics import (
    TIME_PRECISIONS,
    can_judge_simultaneous,
    derive_time_precision,
    precision_rank,
    resolve_time_precision,
    scan_time_conflicts,
    time_conflict,
    weaker,
)


class TestPrecisionDerive(unittest.TestCase):
    def test_日期级派生为date档(self):
        self.assertEqual(derive_time_precision("2020-01-08"), "date")
        self.assertEqual(derive_time_precision(dt.date(2020, 1, 8)), "date")

    def test_零时刻派生为date档而非精确(self):
        """红线：类型是 datetime 但时刻全零 → 仍是 date 档。

        否则两个日期级事件同落 00:00:00，会被判成"同一时刻"。
        """
        self.assertEqual(derive_time_precision("2020-01-08 00:00:00"), "date")

    def test_非零时刻按粗细派生(self):
        self.assertEqual(derive_time_precision("2020-01-08 09:00:00"), "hour")
        self.assertEqual(derive_time_precision("2020-01-08 09:30:00"), "minute")
        self.assertEqual(derive_time_precision("2020-01-08 09:30:15"), "second")

    def test_空值不可比(self):
        for v in (None, "", "   "):
            self.assertIsNone(derive_time_precision(v))


class TestWeakerAndSimultaneous(unittest.TestCase):
    def test_木桶效应取较低档(self):
        self.assertEqual(weaker("date", "second"), "date")
        self.assertEqual(weaker("minute", "second"), "minute")
        self.assertEqual(weaker("hour", "minute"), "hour")

    def test_任一未知即不可比(self):
        self.assertIsNone(weaker("date", None))
        self.assertIsNone(weaker(None, "second"))
        self.assertIsNone(weaker("乱值", "second"))

    def test_仅分钟及以上可判同时(self):
        self.assertFalse(can_judge_simultaneous("date", "date"))
        self.assertFalse(can_judge_simultaneous("date", "second"))
        self.assertFalse(can_judge_simultaneous("hour", "second"))
        self.assertTrue(can_judge_simultaneous("minute", "second"))
        self.assertTrue(can_judge_simultaneous("second", "second"))

    def test_精度序完备(self):
        self.assertEqual(TIME_PRECISIONS, ("date", "hour", "minute", "second"))
        self.assertEqual([precision_rank(p) for p in TIME_PRECISIONS],
                         [0, 1, 2, 3])
        self.assertEqual(precision_rank("乱值"), -1)


class TestOverride(unittest.TestCase):
    def test_显式覆盖优先于派生(self):
        """真午夜事件：派生会误判成 date，接入层可显式覆盖纠正。"""
        row = {"timestamp": "2020-01-08 00:00:00", "time_precision": "minute"}
        self.assertEqual(resolve_time_precision(row), "minute")

    def test_无覆盖回落派生(self):
        row = {"timestamp": "2020-01-08 00:00:00"}
        self.assertEqual(resolve_time_precision(row), "date")

    def test_非法覆盖值回落派生不硬失败(self):
        row = {"timestamp": "2020-01-08 14:30:00", "time_precision": "乱写"}
        self.assertEqual(resolve_time_precision(row), "minute")

    def test_仅date字段时派生date档(self):
        self.assertEqual(resolve_time_precision({"date": "2020-01-08"}), "date")

    def test_无时间字段返回None(self):
        self.assertIsNone(resolve_time_precision({"person_raw": "张三"}))


class TestConflict(unittest.TestCase):
    def test_日期部分不等即冲突(self):
        self.assertTrue(time_conflict("2020-01-08", "2020-01-09 10:00:00"))

    def test_日期部分相同不算冲突(self):
        self.assertFalse(time_conflict("2020-01-08", "2020-01-08 10:00:00"))

    def test_任一为空不算冲突(self):
        """timestamp 全 NULL 是正常状态（未接入时刻数据），不是冲突。"""
        self.assertFalse(time_conflict("2020-01-08", None))
        self.assertFalse(time_conflict(None, "2020-01-08 10:00:00"))

    def test_缺表不硬失败(self):
        self.assertEqual(scan_time_conflicts(None, "obj_trackpoint"), [])


if __name__ == "__main__":
    unittest.main()


class TestDatetimeCleanOp(unittest.TestCase):
    """cn_datetime_norm：中文日期时间归一（时刻保留、日期段补零）。

    为什么不能复用 cn_date_norm + pad_date：后者按 "-" 切段补零时末段是
    "8 14:30"（非纯数字）→ 补零静默失效 → TRY_CAST 得 NULL，表现为
    "没有时刻数据"，与真实无时刻无法区分（静默失败比报错危险）。
    """

    def setUp(self):
        from core.clean_ops import OPS
        self.fn = OPS["cn_datetime_norm"].fn

    def test_中文全格式(self):
        self.assertEqual(self.fn("2024年3月15日 14时5分20秒"),
                         "2024-03-15 14:05:20")

    def test_中文时分无秒不留尾随冒号(self):
        self.assertEqual(self.fn("2024年3月15日 14时5分"), "2024-03-15 14:05")

    def test_标准格式日期段补零(self):
        self.assertEqual(self.fn("2020-1-8 14:30"), "2020-01-08 14:30")

    def test_纯日期不添加时刻(self):
        """纯日期不该被补出 00:00:00——那是伪造时刻。"""
        self.assertEqual(self.fn("2020-01-08"), "2020-01-08")

    def test_空值原样返回(self):
        self.assertEqual(self.fn(""), "")
