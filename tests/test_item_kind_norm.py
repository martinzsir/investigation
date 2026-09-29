"""标识符种类归一化（界面登记入口）的守卫测试。

为什么单独立文件
----------------
这张别名表有两类失效方式，都不是"报错"，而是静默错位：

1. **抢名**：两个种类共用一个别名，谁命中取决于声明顺序。
   典型是把 "SN" 同时加给 serial 和别的种类——正兵填序列号，
   存进去的可能是另一种凭证，且因为都是"有凭证"，校验全过。
2. **漏覆**：新增了种类却忘了加别名，正兵在下拉里看得见、
   填进去却被判 None——None 是"识别不了"，不是"无凭证"。

两类都不会让测试变红，只会让数据悄悄错。所以这里用穷举断言盯死。

词汇声明化后的真源
------------------
词汇真源已迁入本体声明 ``ontology/_shared/data_elements.json``
（DE_ITEM_IDENTIFIER_KIND / DE_ITEM_TYPE 的 enum_meta），
``core/item.py`` 的常量是 import 期派生物。抢名/悬空引用在装载期硬失败
（loader 侧用例见 tests/test_data_elements.py::TestItemVocabulary）；
本文件守两件事：派生表与 JSON 声明**逐字对拍**（防派生层漂移），
以及归一化行为（识别不了返回 None，绝不猜、绝不落 none）。
"""
import json
import unittest

import core.ontology_loader as ol
from core.data_elements import norm_vocab_alias
from core.item import (
    ITEM_IDENTIFIER_KINDS,
    ITEM_KIND_LABELS,
    IDENTIFIER_KIND_ALIASES,
    ITEM_TYPE_LABELS,
    ITEM_TYPE_SOURCE_MAP,
    SENSITIVE_IDENTIFIER_KINDS,
    identifier_kind_label,
    normalize_identifier_kind,
)


def _shared_item_elements() -> tuple[dict, dict]:
    """读真源声明：DE_ITEM_IDENTIFIER_KIND 与 DE_ITEM_TYPE。"""
    p = ol.PACK_ROOT / "_shared" / "data_elements.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    elems = data["elements"]
    return elems["DE_ITEM_IDENTIFIER_KIND"], elems["DE_ITEM_TYPE"]


class TestItemKindNorm(unittest.TestCase):

    # ---- 结构守卫：派生表与 JSON 声明逐字对拍（穷举，不挑样例）----

    def test_kinds_match_declaration_order(self):
        """种类集合与顺序 = 声明的 enum 顺序。

        顺序即 primary_digest 选取优先级——派生层若重排，同批数据的主标识
        选取会静默漂移，digest 全对不上。
        """
        de_kind, _ = _shared_item_elements()
        self.assertEqual(tuple(str(v) for v in de_kind["enum"]),
                         ITEM_IDENTIFIER_KINDS)

    def test_labels_cover_all_kinds(self):
        for k in ITEM_IDENTIFIER_KINDS:
            self.assertIn(k, ITEM_KIND_LABELS, f"种类 {k!r} 缺规范中文标签")
            self.assertTrue(ITEM_KIND_LABELS[k], f"种类 {k!r} 的标签为空字符串")

    def test_labels_match_declaration(self):
        de_kind, _ = _shared_item_elements()
        expect = {str(k): str((de_kind.get("enum_meta") or {}).get(k, {}).get("label") or k)
                  for k in de_kind["enum"]}
        self.assertEqual(expect, ITEM_KIND_LABELS)

    def test_alias_index_matches_declaration(self):
        """代码本身/label/别名归一化后都必须命中本种类（索引与声明同源）。"""
        de_kind, _ = _shared_item_elements()
        meta = de_kind.get("enum_meta") or {}
        for k in de_kind["enum"]:
            k = str(k)
            m = meta.get(k) or {}
            terms = [k, str(m.get("label") or k)] + [str(a) for a in (m.get("aliases") or [])]
            for term in terms:
                hit = IDENTIFIER_KIND_ALIASES.get(norm_vocab_alias(term))
                self.assertEqual(k, hit, f"{term!r} 应命中 {k!r}，实得 {hit!r}")

    def test_alias_targets_are_known_kinds(self):
        for alias, kind in IDENTIFIER_KIND_ALIASES.items():
            self.assertIn(kind, ITEM_IDENTIFIER_KINDS,
                          f"别名 {alias!r} 指向未定义种类 {kind!r}")

    def test_every_kind_reachable(self):
        """每个种类至少有一个中文入口，且代码本身也能命中。

        漏一个种类 = 界面上能选、存不进去，且失败形态是 None（识别不了），
        不是 ValueError——正兵无从知道是自己填错了还是系统坏了。
        """
        for kind in ITEM_IDENTIFIER_KINDS:
            self.assertEqual(kind, normalize_identifier_kind(kind),
                             f"种类 {kind!r} 连自己的代码都识别不了")
            self.assertTrue(any(v == kind for v in IDENTIFIER_KIND_ALIASES.values()),
                            f"种类 {kind!r} 没有任何中文别名，界面无法登记")

    def test_source_map_matches_declaration(self):
        """ITEM_TYPE_SOURCE_MAP = DE_ITEM_TYPE.enum_meta 的 source_labels 全量。"""
        _, de_type = _shared_item_elements()
        expect: dict[str, tuple[str, str]] = {}
        for t in de_type["enum"]:
            m = (de_type.get("enum_meta") or {}).get(str(t)) or {}
            for sl in (m.get("source_labels") or []):
                expect[str(sl)] = (str(t), str(m["identifier_kind"]))
        self.assertEqual(expect, ITEM_TYPE_SOURCE_MAP)

    def test_portable_has_no_source_label(self):
        """行为钉死：「便携物」不进中文源表映射。

        收编前 ITEM_TYPE_SOURCE_MAP 就无 portable——预装配收到该词应继续
        报"未知物品类型"，不得因声明化而被静默放行。
        """
        self.assertIn("portable", ITEM_TYPE_LABELS)
        self.assertNotIn("便携物", ITEM_TYPE_SOURCE_MAP)
        self.assertNotIn("portable",
                         {t for t, _k in ITEM_TYPE_SOURCE_MAP.values()})

    # ---- 行为断言 ----

    def test_chinese_labels(self):
        self.assertEqual("plate", normalize_identifier_kind("车牌号"))
        self.assertEqual("plate", normalize_identifier_kind("车牌"))
        self.assertEqual("property_cert", normalize_identifier_kind("权证号"))
        self.assertEqual("invoice_code_no", normalize_identifier_kind("发票代码+号码"))
        self.assertEqual("batch_no", normalize_identifier_kind("批号"))
        self.assertEqual("none", normalize_identifier_kind("无凭证"))

    def test_code_and_case_insensitive(self):
        """代码本身与大小写/下划线变体都要放行，避免调用方二次判断。"""
        for k in ITEM_IDENTIFIER_KINDS:
            self.assertEqual(k, normalize_identifier_kind(k.upper()))
            self.assertEqual(k, normalize_identifier_kind(f" {k} "))
        self.assertEqual("imei", normalize_identifier_kind("  IMEI "))

    def test_unknown_returns_none_not_guess(self):
        """识别不了必须返回 None，绝不猜、绝不落 none。

        "车号" 这种表外说法若被猜成 plate，是猜对了也不可接受——
        下次有人填 "工号" 也会被同一条路径猜成某种凭证。
        """
        for bad in ("车号", "工号", "身份证号", "", None, "  ", "未知凭证"):
            self.assertIsNone(normalize_identifier_kind(bad),
                              f"{bad!r} 应识别不了（None），不能猜")

    def test_unknown_none_is_not_d4_none(self):
        """None（识别不了）与 'none'（D4 无凭证）必须可区分。

        这是最容易混的一对：两者都是"没有凭证"。但前者是数据错误、
        后者是合法登记。混同的结果是一件有凭证的车被记成无凭证赃物，
        从此永不参与消歧——实体永久丢失，且没有任何报错。
        """
        self.assertIsNone(normalize_identifier_kind("车号"))
        self.assertEqual("none", normalize_identifier_kind("无凭证"))
        self.assertNotEqual(normalize_identifier_kind("车号"),
                            normalize_identifier_kind("无凭证"))

    def test_label_roundtrip(self):
        for k in ITEM_IDENTIFIER_KINDS:
            lbl = identifier_kind_label(k)
            self.assertTrue(lbl)
            self.assertEqual(k, normalize_identifier_kind(lbl),
                             f"规范标签 {lbl!r} 应能归一回 {k!r}")
        self.assertEqual("车牌号", identifier_kind_label("plate"))
        self.assertEqual("无凭证", identifier_kind_label("none"))
        # 未知种类原样返回，不编造标签
        self.assertEqual("zzz", identifier_kind_label("zzz"))

    def test_sensitive_kinds_unchanged(self):
        """手机号与设备码仍属敏感：归一不能改变遮蔽属性。

        这条是回归保险——若有人为了"方便登记"把 msisdn 挪出敏感集合，
        装载期 fail-closed 会放过明文，而界面上看起来一切正常。
        """
        self.assertIn("msisdn", SENSITIVE_IDENTIFIER_KINDS)
        self.assertIn("imei", SENSITIVE_IDENTIFIER_KINDS)
        self.assertIn(normalize_identifier_kind("手机号"), SENSITIVE_IDENTIFIER_KINDS)
        self.assertIn(normalize_identifier_kind("IMEI"), SENSITIVE_IDENTIFIER_KINDS)
        self.assertNotIn(normalize_identifier_kind("车牌号"), SENSITIVE_IDENTIFIER_KINDS)


if __name__ == "__main__":
    unittest.main()
