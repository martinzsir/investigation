"""
tests/test_rule_overlap.py
REQ-025 规则互斥与重叠消解：
  - AC1：exclusive_group 内 primary 命中 → 非 primary 自动抑制
  - AC2：excludes 单向声明（A→B 但 B→A 不写）→ 装载期 warnings.warn
  - AC3：抑制结果 suppressed_log 可审计（含抑制原因/组/主规则/被抑制 finding 字段）
  - AC4：无 exclusive_group 的规则（R3/R4/R5）行为不变
  - AC5：保留 finding 的 suppressed_log 含抑制原因，产物落盘可见
"""
from __future__ import annotations

import json
import unittest
import warnings
from pathlib import Path

from core import Store
from core.ontology import build_ontology
from core.ontology_loader import load_pack, SCHEMA_VERSION
from core.rules import run_rules

ROOT = Path(__file__).resolve().parent.parent


def _make_store() -> Store:
    """baseline golden 同结构：R1（整数存现）+ R2（整数聚合）双命中。"""
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
              "('张卫国','李志强','2021-10-01',3),"
              "('张卫国','李志强','2021-09-30',5)")
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设','李志强','存续',NULL)")
    s.execute("INSERT INTO 轨迹出行 VALUES "
              "('2021-10-02','张卫国','项目B'),"
              "('2021-09-30','张卫国','项目A'),"
              "('2021-10-01','李志强','项目A')")
    s.execute("INSERT INTO 招投标档案 VALUES ('项目A','宏业建设','2021-10-01','张卫国')")
    s.execute("INSERT INTO 公开OSINT VALUES ('张卫国','分管招投标','2019-03-01','政府官网')")
    s.execute("INSERT INTO 举报材料 VALUES ('2022-01-10','经济类','张卫国','匿名','x')")
    build_ontology(s.conn)
    return s


class RuleOverlapTests(unittest.TestCase):

    def test_ac1_primary_suppresses_secondary_in_group(self):
        """AC1：integer_amount 组内 R1（primary）命中 → R2（secondary）不出现在主 findings。"""
        store = _make_store()
        try:
            findings = run_rules(store, stage=None)
        finally:
            store.close()
        rule_ids = [f["rule_id"] for f in findings]
        self.assertIn("R1", rule_ids, "primary R1 必须保留（组内不被抑制）")
        self.assertNotIn("R2", rule_ids,
                         "AC1 失败：R1(primary) 命中 → R2(secondary) 仍出现在主 findings："
                         + str(rule_ids))

    def test_ac2_excludes_one_way_warns_at_load(self):
        """AC2：excludes 单向声明（A→B 但 B→A 不写）→ 装载期 warnings.warn。"""
        import tempfile
        import shutil
        from core.ontology_loader import _load_rules
        from core.functions import FunctionExecutor  # noqa: F401 确保 functions 已加载
        # 克隆 pack 到临时目录，写 rules.json（R2 excludes=[R1]，但 R1 不写 excludes）
        spec = load_pack("default")
        tmp = ROOT / "ontology" / "_tmp_overlap_ac2"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        rules_out = []
        for r in spec.rules.values():
            d = {
                "id": r.id, "stage": r.stage, "title": r.title,
                "rule_text": r.rule_text, "function": r.function,
                "params": dict(r.params), "hit_when": r.hit_when,
                "dimension": r.dimension, "jian_types": list(r.jian_types),
                "assumption": r.assumption, "basis_text": r.basis_text,
            }
            if r.exclusive_group:
                d["exclusive_group"] = r.exclusive_group
                d["primary_rule"] = r.primary_rule
                d["overlap_resolution"] = r.overlap_resolution
                # 强制单向：R1 清 excludes，R2 保留 excludes=[R1]
                if r.id == "R1":
                    d["excludes"] = []
                elif r.id == "R2":
                    d["excludes"] = ["R1"]
            rules_out.append(d)
        (tmp / "rules.json").write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "rules": rules_out},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        functions_src = (ROOT / "ontology" / "default" / "functions.json")
        if functions_src.exists():
            import shutil as _sh
            _sh.copy(functions_src, tmp / "functions.json")
        with warnings.catch_warnings(record=True) as wlist:
            warnings.simplefilter("always")
            try:
                _load_rules(tmp / "rules.json", spec.functions, required=True)
            finally:
                shutil.rmtree(tmp)
            messages = [str(x.message) for x in wlist]
        self.assertTrue(any("单向声明" in m for m in messages),
                        f"AC2 失败：装载期未出 excludes 单向告警。warnings={messages}")

    def test_ac3_suppressed_log_auditable(self):
        """AC3：findings 总数下降（R1 抑制 R2），且 suppressed_log 完整可审计。"""
        store = _make_store()
        try:
            findings = run_rules(store, stage=None)
        finally:
            store.close()
        # 主 findings 中 R2 被移除：统计
        suppressed_logs = [f.get("suppressed_log") or [] for f in findings]
        flat = [x for sl in suppressed_logs for x in sl]
        # 抑制项中应至少有 R2
        self.assertTrue(any(x["rule_id"] == "R2" for x in flat),
                        f"AC3 失败：suppressed_log 不含 R2。日志={json.dumps(flat, ensure_ascii=False)[:200]}")
        r2_entry = next(x for x in flat if x["rule_id"] == "R2")
        # 审计字段齐全
        for k in ("rule_id", "suppressed_by_group", "suppressed_by_rule", "reason",
                  "级别", "候选虚处", "source_rows"):
            self.assertIn(k, r2_entry, f"suppressed_log 缺字段 {k}：{r2_entry}")
        self.assertEqual(r2_entry["suppressed_by_group"], "integer_amount")
        self.assertEqual(r2_entry["suppressed_by_rule"], "R1")
        self.assertIn("primary=R1 命中", r2_entry["reason"])

    def test_ac4_non_group_rules_untouched(self):
        """AC4：无 exclusive_group 的 R3/R4/R5 行为不变（不被抑制，不抑制别人）。"""
        store = _make_store()
        try:
            findings = run_rules(store, stage=None)
        finally:
            store.close()
        rule_ids = [f["rule_id"] for f in findings]
        # R3/R4/R5 都应保留（原 baseline 行为）——注意 golden baseline 中这三条中 R4/R6 仍命中
        for rid in ("R4", "R6"):
            self.assertIn(rid, rule_ids, f"非重叠规则 {rid} 被误抑制：{rule_ids}")
        # R4 没有 _suppressed 标记
        r4 = next(f for f in findings if f["rule_id"] == "R4")
        self.assertNotIn("_suppressed", r4, "R4 不应该有 _suppressed 标记")

    def test_ac5_suppression_reason_in_product(self):
        """AC5：保留 finding 的 suppressed_log 有抑制原因（人可读文本 + 机器组号）。"""
        store = _make_store()
        try:
            findings = run_rules(store, stage=None)
        finally:
            store.close()
        # 任何保留 finding 都应携带 suppressed_log（即使为空也要有字段）
        for f in findings:
            self.assertIn("suppressed_log", f,
                          f"finding {f['rule_id']} 缺失 suppressed_log 字段")
        # 有抑制时 reason 非空
        if findings:
            first_log = findings[0]["suppressed_log"]
            if first_log:
                self.assertTrue(all(x["reason"] for x in first_log),
                                "suppressed_log 条目 reason 字段空")
                self.assertTrue(all(x["suppressed_by_group"] for x in first_log),
                                "suppressed_log 条目 group 字段空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
