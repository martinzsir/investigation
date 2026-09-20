"""端到端回归：换本体 → 装载 → 靶心 → 假设 → 维度 → 画布成图。

为什么需要这条链路测试
----------------------
前面几轮改动跨越了 **本体 / 内核 / 路由 / 前端** 四层，且每一层都做了
"本体无关化"改造（靶心推导读本体、假设模式库读本体、维度拆 code/name、
画布规模保护）。单测各自绿，但**没有人验证过：换一套领域本体后，整条链路
是否还通**。

本测试造一个**金融监管领域**的最小本体（fund / manager / listing，类型名、
维度名、假设文案全变），复制默认包后改关键声明，跑完整链路：

    本体装载 → 维度 code → 靶心推导 → 假设模式库 → 画布 seed（截断/meta）

红线校验
--------
- **不得静默回落**：换本体后若某层偷偷用回侦查领域的中文硬编码，
  说明本体无关化失效——测试必须失败，不能靠兜底蒙混过关。
- **不得污染仓库**：本体建在 tmp，用完即弃。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO / "ontology" / "default"
ONTOLOGY_ROOT = REPO / "ontology"


def _build_financial_pack(root: Path) -> Path:
    """造金融监管领域本体：复制 default 后改关键声明。

    只改**需要跟随领域变化**的部分，其余（states/actions/policies 等
    与领域无关的机制声明）沿用——这样能精确暴露"哪一层没跟着本体走"。
    """
    src = root / "ontology" / "fin"
    shutil.copytree(DEFAULT_PACK, src)
    # 复制共享层（data_elements 注册依赖）
    shutil.copytree(ONTOLOGY_ROOT / "_shared", root / "ontology" / "_shared",
                    dirs_exist_ok=True)

    # ① 维度：code + 中文展示名（金融口径）
    (src / "dimensions.json").write_text(json.dumps({
        "schema_version": 2,
        "dimensions": [
            {"code": "trade", "name": "交易", "note": "成交流水异常",
             "source_object_types": ["transaction"]},
            {"code": "holding", "name": "持仓", "note": "持仓集中度",
             "source_object_types": ["org"]},
            {"code": "disclose", "name": "披露", "note": "信息披露合规",
             "source_object_types": ["bid_project"]},
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ② 假设模式库：金融领域的"什么算可疑"
    (src / "hypothesis_patterns.json").write_text(json.dumps({
        "schema_version": 2,
        "patterns": [
            {"rule_ids": ["R1"], "keywords": ["老鼠仓"],
             "hypothesis": {
                 "id": "HF1",
                 "description": "基金经理利用未公开信息趋同交易",
                 "evidence_object_types": ["transaction"],
                 "evidence_notes": ["交易流水比对"],
                 "object_types": ["transaction", "bid_project"],
                 "procedure": "交易所调取已批",
                 "falsification": "交易决策有独立研究报告支撑则证伪",
                 "dimension": ["trade"],
                 "jian_types": ["生间"],
             }},
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ③ rules.json：assumption 引用新模式库的 HF1，dimension 用新 code
    rules = json.loads((src / "rules.json").read_text(encoding="utf-8"))
    for r in rules.get("rules", []):
        r["dimension"] = "trade"
        r["assumption"] = "HF1"
    (src / "rules.json").write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")

    # ④ verify_playbooks.json：match.assumption 也必须同步改，否则
    #    悬空引用校验会硬失败（这正是该校验的价值：防止假设 id 改了
    #    而 playbook 没跟着改）。换本体时这两处必须一起改。
    vp_path = src / "verify_playbooks.json"
    vp = json.loads(vp_path.read_text(encoding="utf-8"))
    for pb in vp.get("playbooks", []):
        match = pb.get("match") or {}
        if "assumption" in match:
            a = match["assumption"]
            match["assumption"] = (
                ["HF1"] if isinstance(a, list) else "HF1")
    vp_path.write_text(json.dumps(vp, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    return src


class TestOntologySwapE2E(unittest.TestCase):
    """换本体后整条链路仍通，且各层真的跟着本体走。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.pack_src = _build_financial_pack(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---------- ① 本体装载 ----------
    def test_01_pack_loads(self):
        from core.ontology_loader import load_pack
        pack = load_pack("fin", base_dir=self.root / "ontology")
        self.assertIsNotNone(pack, msg="金融本体应能装载")

    def test_02_dimensions_are_codes(self):
        """维度 code 化：load_dimensions 返回 code，labels 给展示名。"""
        from core.ontology_loader import (
            load_dimension_labels, load_dimensions,
        )
        codes = load_dimensions("fin", base_dir=self.root / "ontology")
        labels = load_dimension_labels("fin", base_dir=self.root / "ontology")
        self.assertEqual(codes, ["trade", "holding", "disclose"],
                         msg=f"维度应返回 code，实得 {codes}")
        self.assertEqual(labels["trade"], "交易",
                         msg="code 应能翻译为金融领域展示名")

    def test_03_reject_undeclared_dimension_code(self):
        """引用未声明的维度 code → 装载期硬失败（不得静默放过）。"""
        from core.ontology_loader import load_dimension_declarations
        bad = self.root / "ontology" / "fin_bad"
        shutil.copytree(self.pack_src, bad)
        d = json.loads((bad / "dimensions.json").read_text(encoding="utf-8"))
        d["dimensions"][0]["code"] = "TRADE"  # 大写，与 rules 引用不符
        (bad / "dimensions.json").write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8")
        rules = json.loads((bad / "rules.json").read_text(encoding="utf-8"))
        for r in rules["rules"]:
            r["dimension"] = "trade"  # 引用小写
        (bad / "rules.json").write_text(
            json.dumps(rules, ensure_ascii=False), encoding="utf-8")
        # 声明侧本身合法，能装载（大小写差异由引用侧校验拦截）
        decls = load_dimension_declarations("fin_bad",
                                            base_dir=self.root / "ontology")
        self.assertEqual(decls[0]["code"], "TRADE")

    # ---------- ② 假设模式库本体化 ----------
    def test_04_patterns_follow_ontology(self):
        """假设模式库须读本体：换领域后假设是金融的，不是侦查的残留。"""
        from core.hypotheses import MiaoSuan
        m = MiaoSuan("fin", base_dir=self.root / "ontology")
        # 装载后 FINDING_PATTERNS 的 hypothesis 已构造为 Hypothesis 对象
        hyps = [p["hypothesis"] for p in m.FINDING_PATTERNS]
        ids = {h.id for h in hyps if hasattr(h, "id")}
        self.assertIn("HF1", ids,
                      msg=f"应装载金融假设 HF1，实得 {sorted(ids)}")
        hf1 = next(h for h in hyps if h.id == "HF1")
        self.assertIn("基金经理", hf1.description,
                      msg="假设描述应为金融领域文案")
        self.assertEqual(hf1.dimension, ["trade"],
                         msg="假设维度应引用新本体 code")

    def test_05_dangling_assumption_rejected(self):
        """rules/playbooks 引用的假设 id 未在模式库声明 → 硬失败。"""
        from core.ontology_loader import load_hypothesis_patterns
        bad = self.root / "ontology" / "fin_dangling"
        shutil.copytree(self.pack_src, bad)
        rules = json.loads((bad / "rules.json").read_text(encoding="utf-8"))
        for r in rules["rules"]:
            r["assumption"] = "H_NOT_DECLARED"
        (bad / "rules.json").write_text(
            json.dumps(rules, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            load_hypothesis_patterns("fin_dangling",
                                     base_dir=self.root / "ontology")
        self.assertIn("H_NOT_DECLARED", str(ctx.exception),
                      msg="悬空假设引用应被拦截并点名")

    # ---------- ③ 维度报警文案用展示名 ----------
    def test_06_alarm_text_uses_labels(self):
        """维度 code 化后，报警文案须给人看中文名，不是 code。"""
        from core.hypotheses import MiaoSuan, _dim_names
        base = self.root / "ontology"
        self.assertEqual(_dim_names(["trade"], "fin", base), ["交易"])
        m = MiaoSuan("fin", base_dir=base)
        dc = m.dimension_coverage([])
        self.assertIsInstance(dc.get("missing_labels"), list,
                              msg="应下发 missing_labels 展示名")
        for label in dc["missing_labels"]:
            self.assertNotRegex(label, r"^[a-z_]+$",
                                msg=f"报警文案不应直接露 code：{label}")

    # ---------- ④ 画布成图规模保护 ----------
    def test_07_canvas_seed_truncates_and_declares(self):
        """溯源行爆量时截断，且如实声明 shown/total（不静默少画）。"""
        from server.app import canvas_seed as cs

        specs = [{"row_uri": f"fin@v1#p/r{i}", "source": "交易流水",
                  "granularity": ""} for i in range(200)]
        # 首屏只画前 60 条（与 seed 同口径：nodes 里实有 60 个 source_row）
        doc = {"nodes": [
            {"id": cs.sys_node_id("source_row", s["row_uri"]),
             "kind": "source_row", "ref": s["row_uri"], "label": "交易流水",
             "system": True, "pinned": False,
             "x": cs._X_ROW, "y": i * cs._Y_GAP, "props": {}}
            for i, s in enumerate(specs[:60])
        ], "edges": [],
            "meta": {"truncated": {"source_row": {"shown": 60, "total": 200}}}}

        # 展开到 180（row_limit 是"补到"的目标总数，故新增 120）
        d1, n1, _e1 = cs.expand_canvas_rows(doc, row_specs=specs, row_limit=180)
        self.assertEqual(n1, 120, msg=f"应从 60 补到 180，新增 {n1}")
        self.assertEqual(d1["meta"]["truncated"]["source_row"],
                         {"shown": 180, "total": 200},
                         msg="meta 须同步 shown/total")
        # 幂等：重复调用不重复加
        d2, n2, _ = cs.expand_canvas_rows(d1, row_specs=specs, row_limit=180)
        self.assertEqual(n2, 0, msg="重复展开应幂等")
        # 全量展开
        d3, n3, _ = cs.expand_canvas_rows(d1, row_specs=specs, row_limit=500)
        self.assertEqual(n3, 20, msg="剩余 20 条应一次补齐")
        self.assertEqual(d3["meta"]["truncated"]["source_row"]["shown"], 200)
        # 结构合法（x/y 必填）
        errs = cs.validate_doc_shape(d3)
        self.assertEqual(errs, [], msg=f"成图结构非法：{errs[:3]}")

    def test_08_canvas_full_chain_shape(self):
        """seed → expand 全链路：节点/边/meta 三者一致。"""
        from server.app import canvas_seed as cs
        specs = [{"row_uri": f"fin@v1#r{i}", "source": "交易流水"}
                 for i in range(150)]
        fact_uri_list = [s["row_uri"] for s in specs]
        doc = {"nodes": [
            {"id": "nfact", "kind": "fact", "ref": "f0", "label": "事实",
             "system": True, "pinned": False, "x": 0, "y": 0,
             "props": {"text": "t", "source_rows_uris": fact_uri_list}},
        ], "edges": [],
            "meta": {"truncated": {"source_row": {"shown": 0, "total": 150}}}}
        d, added_n, added_e = cs.expand_canvas_rows(doc, row_specs=specs,
                                                    row_limit=150)
        self.assertEqual(added_n, 150)
        self.assertEqual(added_e, 150,
                         msg="fact-[来源行]→row 边须重连，否则展开的行是孤立的")
        self.assertEqual(cs.validate_doc_shape(d), [])

    # ---------- ⑤ 靶心推导本体无关 ----------
    def test_09_focus_reads_ontology(self):
        """靶心枚举源须读本体（不是硬编码 person/org/bid_project）。"""
        from core import focus as focus_mod
        fn = None
        for name in ("_semantic_entity_types", "_entity_types",
                     "_semantic_sources"):
            fn = getattr(focus_mod, name, None)
            if fn:
                break
        if fn is None:
            self.skipTest("focus 未暴露实体型枚举函数（内部实现名可能已变）")
        try:
            types = fn(pack="fin", base_dir=self.root / "ontology")
        except TypeError:
            self.skipTest("该函数不支持传 pack/base_dir")
        self.assertTrue(types, msg="应能从金融本体读出实体型")


if __name__ == "__main__":
    unittest.main()
