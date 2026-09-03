"""
tests/test_golden.py
REQ-020 Golden Finding 回归：固定 seed 的场景夹具锁定检测器行为。

三件套：
  1. tests/fixtures/golden/*.json —— 固定场景（源表行）+ 锁定期望（finding 数量 /
     命中规则序列 / source_rows 合计 / 逐规则计数）；
  2. 同场景跑两遍 run_rules，结果必须逐字节一致（确定性）；
  3. 漂移报告（drift_report）：期望与实际任何差异都给出人类可读行，检测器改动后
     要么零漂移，要么由分析师显式刷新 golden（夹具即基线）。

另有夹具脱敏扫描：golden 目录内不得出现身份证/手机号/银行卡长数字（合成场景纪律）。
"""
import json
import re
import unittest
from pathlib import Path

from core import Store
from core.ontology import build_ontology
from core.rules import run_rules

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden"

# 脱敏正则（golden 场景一律合成数据，禁止真实 PII 形态）
_PII_PATTERNS = [
    (re.compile(r"\d{17}[\dXx]"), "身份证号形态"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号形态"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "银行卡/超长数字形态"),
]

# 按列名推断 DuckDB 列类型（其余一律 VARCHAR）
_TYPE_HINTS = {
    "金额": "DOUBLE",
    "次数": "BIGINT",
    "发布日期": "DATE",
    "举报日期": "DATE",
    "中标公示日": "DATE",
}


def load_fixture(name: str) -> dict:
    p = FIXTURE_DIR / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def build_store(fx: dict) -> Store:
    """按夹具建内存库 → 灌行 → 构建语义层。"""
    s = Store(db_path=":memory:")
    for table, spec in fx["tables"].items():
        cols = spec["columns"]
        col_def = ", ".join(f'"{c}" {_TYPE_HINTS.get(c, "VARCHAR")}' for c in cols)
        s.execute(f'CREATE TABLE "{table}" ({col_def})')
        for row in spec["rows"]:
            placeholders = ", ".join(["?"] * len(cols))
            s.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', list(row))
    build_ontology(s.conn)
    return s


def summarize(findings: list[dict]) -> dict:
    """把 run_rules 结果压成可比对摘要（顺序敏感：规则序列即优先级/稳定性锁）。"""
    return {
        "finding_count": len(findings),
        "rules_hit": [f["rule_id"] for f in findings],
        "source_rows_total": sum(len(f.get("source_rows") or []) for f in findings),
        "by_rule": {
            f["rule_id"]: {
                "hits": 1,
                "source_rows": len(f.get("source_rows") or []),
            }
            for f in findings
        },
    }


def drift_report(name: str, actual: dict, expect: dict) -> list[str]:
    """期望 vs 实际的人类可读漂移行；空列表 = 零漂移。"""
    lines = []
    if actual["finding_count"] != expect["finding_count"]:
        lines.append(
            f"[{name}] finding 数量漂移: 期望 {expect['finding_count']} → 实际 {actual['finding_count']}"
        )
    if actual["rules_hit"] != expect["rules_hit"]:
        missing = [r for r in expect["rules_hit"] if r not in actual["rules_hit"]]
        added = [r for r in actual["rules_hit"] if r not in expect["rules_hit"]]
        reorder = (
            not missing and not added and actual["rules_hit"] != expect["rules_hit"]
        )
        if missing:
            lines.append(f"[{name}] 规则消失: {missing}")
        if added:
            lines.append(f"[{name}] 新出现规则（golden 未锁定）: {added}")
        if reorder:
            lines.append(
                f"[{name}] 规则序列变化: {expect['rules_hit']} → {actual['rules_hit']}"
            )
    if actual["source_rows_total"] != expect["source_rows_total"]:
        lines.append(
            f"[{name}] source_rows 合计漂移: 期望 {expect['source_rows_total']} "
            f"→ 实际 {actual['source_rows_total']}"
        )
    for rid, e in (expect.get("by_rule") or {}).items():
        a = actual["by_rule"].get(rid)
        if a is None:
            lines.append(f"[{name}] {rid} 逐规则计数缺失")
        elif a != e:
            lines.append(f"[{name}] {rid} 逐规则计数漂移: 期望 {e} → 实际 {a}")
    for rid in actual["by_rule"]:
        if rid not in (expect.get("by_rule") or {}):
            lines.append(f"[{name}] {rid} 为新增命中（golden 未锁定）")
    return lines


class GoldenFindingTests(unittest.TestCase):

    def _run_fixture(self, name: str) -> tuple[dict, list[dict], dict]:
        fx = load_fixture(name)
        store = build_store(fx)
        try:
            findings = run_rules(store, stage=None)
        finally:
            store.close()
        return fx, findings, summarize(findings)

    def test_01_fixture_suite_complete(self):
        """≥3 个夹具：基线 / 误报陷阱 / 降级场景。"""
        names = sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))
        self.assertIn("baseline", names)
        self.assertIn("false_positive_trap", names)
        self.assertIn("degraded_single_source", names)
        self.assertGreaterEqual(len(names), 3, f"golden 夹具不足 3 个: {names}")

    def test_02_fixtures_desensitized(self):
        """golden 夹具为合成数据：不得含身份证/手机号/银行卡形态。"""
        offenders = []
        for p in sorted(FIXTURE_DIR.glob("*.json")):
            text = p.read_text(encoding="utf-8")
            for pat, label in _PII_PATTERNS:
                m = pat.search(text)
                if m:
                    offenders.append(f"{p.name}: {label} -> {m.group(0)[:20]}")
        self.assertEqual(offenders, [], "golden 夹具检出真实 PII 形态")

    def test_03_baseline_locked(self):
        """基线场景：与 golden 期望零漂移（检测器回归的主锁）。"""
        fx, findings, actual = self._run_fixture("baseline")
        expect = fx["expect"]
        self.assertIsNotNone(expect.get("finding_count"),
                             "baseline golden 未锁定（finding_count 为 null），"
                             "请先跑生成脚本固化期望")
        self.assertTrue(findings, "baseline 必须有命中（否则锁无意义）")
        self.assertTrue(all(f["级别"] == "待核实" for f in findings),
                        "finding 状态必须恒为'待核实'（状态锁）")
        drifts = drift_report("baseline", actual, expect)
        self.assertEqual(drifts, [], "\n".join(drifts) or "零漂移")

    def test_04_false_positive_trap_clean(self):
        """误报陷阱：同城不同日/非关联法人/非整万流转一律零命中。"""
        fx, findings, actual = self._run_fixture("false_positive_trap")
        self.assertEqual(actual["finding_count"], 0,
                         f"误报陷阱不应有命中，实际: {actual['rules_hit']}\n"
                         + json.dumps(actual, ensure_ascii=False))
        drifts = drift_report("false_positive_trap", actual, fx["expect"])
        self.assertEqual(drifts, [], "\n".join(drifts))

    def test_05_degraded_single_source_no_crash(self):
        """降级场景：只有银行流水单源，规则零命中不报错（缺失源跳过而非崩）。"""
        fx, findings, actual = self._run_fixture("degraded_single_source")
        self.assertEqual(actual["finding_count"], 0,
                         f"降级场景不应有命中，实际: {actual['rules_hit']}")
        drifts = drift_report("degraded", actual, fx["expect"])
        self.assertEqual(drifts, [], "\n".join(drifts))

    def test_06_determinism_same_input_same_output(self):
        """确定性锁：同夹具跑两遍，摘要逐字节一致（无随机/无时间依赖）。"""
        fx = load_fixture("baseline")
        summaries = []
        for _ in range(2):
            store = build_store(fx)
            try:
                summaries.append(summarize(run_rules(store, stage=None)))
            finally:
                store.close()
        self.assertEqual(summaries[0], summaries[1],
                         "同输入两次跑结果不一致：违反确定性")

    def test_07_drift_report_detects_changes(self):
        """漂移报告自检：篡改期望必须被 drift_report 抓住。"""
        _, _, actual = self._run_fixture("baseline")
        # 篡改 1：finding 数量
        mutated = json.loads(json.dumps(actual, ensure_ascii=False))
        mutated["finding_count"] += 99
        self.assertTrue(drift_report("mut", actual, mutated))
        # 篡改 2：source_rows 合计
        mutated = json.loads(json.dumps(actual, ensure_ascii=False))
        mutated["source_rows_total"] += 1
        self.assertTrue(any("source_rows" in ln for ln in drift_report("mut", actual, mutated)))
        # 篡改 3：伪造一条规则命中
        mutated = json.loads(json.dumps(actual, ensure_ascii=False))
        mutated["rules_hit"].append("R99")
        mutated["by_rule"]["R99"] = {"hits": 1, "source_rows": 1}
        report = drift_report("mut", actual, mutated)
        self.assertTrue(any("R99" in ln for ln in report), report)
        # 零漂移场景
        self.assertEqual(drift_report("same", actual, json.loads(json.dumps(actual))), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
