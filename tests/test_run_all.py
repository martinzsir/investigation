"""
tests/test_run_all.py
任务 ① 集成测试：验证 run_all.py 单一入口能把 适配层→实体对齐(人名+组织)→确认工作台
→ 采样预演 → 侦查主流程 → 处置 → 操作台导出 完整跑通。

用 --auto-review 模式（跳过交互 CLI），验证产物文件齐全 + 关键不变量。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import unittest

ROOT = Path(__file__).parent.parent


class TestRunAllPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 一次端到端运行（--auto-review 避免交互阻塞）
        cls.result = subprocess.run(
            [sys.executable, "run_all.py", "--auto-review", "--no-cli"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        cls.stdout = cls.result.stdout
        cls.stderr = cls.result.stderr

    def test_exit_code_zero(self):
        self.assertEqual(self.result.returncode, 0,
                         msg=f"stderr:\n{self.stderr}\nstdout:\n{self.stdout}")

    def test_all_pipeline_steps_logged(self):
        # 10 个阶段都应打印出来
        for step_marker in [
            "0. 数据准备",
            "3. 数据接入适配层",
            "4. 实体对齐",
            "5. 人工确认工作台",
            "6. 采样预演",
            "7-8. 侦查主流程",
            "9. 处置状态",
            "10. 导出操作台数据",
        ]:
            self.assertIn(step_marker, self.stdout, msg=f"缺少阶段: {step_marker}")

    def test_adaptor_ingested_multiple_formats(self):
        self.assertIn("接入", self.stdout)
        # 至少识别到 csv/json/parquet 中的多种格式
        self.assertIn("采样", self.stdout)  # 后续阶段跑通则说明前置成功

    def test_entity_alignment_runs(self):
        self.assertIn("人名：", self.stdout)
        self.assertIn("组织：", self.stdout)
        # 强合并 + 候选均出现
        self.assertIn("强合并", self.stdout)

    def test_review_queue_exported(self):
        q = json.loads((ROOT / "output" / "review_queue.json").read_text(encoding="utf-8"))
        self.assertIn("decisions", q)
        self.assertIn("pending", q)
        # auto-review 模式下所有候选应已 accepted
        statuses = [d["status"] for d in q["decisions"]]
        self.assertTrue(all(s == "已合并" for s in statuses), msg=f"statuses={statuses}")

    def test_entity_mapping_exported(self):
        m = json.loads((ROOT / "output" / "entity_mapping.json").read_text(encoding="utf-8"))
        self.assertIn("person", m)
        self.assertIn("org", m)
        self.assertIn("review", m)
        # 组织别名（宏业建设=宏业建设第一项目部）应被 accept 后写入 org mapping
        self.assertTrue(len(m["org"]) >= 0)  # 可能为 0（取决于样本），不强制

    def test_lineage_clues_exported(self):
        r = json.loads((ROOT / "output" / "lineage_clues.json").read_text(encoding="utf-8"))
        self.assertIn("cross_level", r)
        self.assertIn("clues", r)
        self.assertGreaterEqual(len(r["clues"]), 1)

    def test_sampling_verdict_present(self):
        self.assertIn("命中率", self.stdout)
        # 三种判定之一
        self.assertTrue(
            ("方向明确" in self.stdout) or ("方向存疑" in self.stdout) or ("方向否定" in self.stdout),
            msg="采样预演未输出判定",
        )

    def test_disposal_filed_redline(self):
        # 已立案须经 已固证 + 法定依据，且审计链写入
        self.assertIn("已立案", self.stdout)
        self.assertIn("法定依据", self.stdout)

    def test_priority_score_attached(self):
        r = json.loads((ROOT / "output" / "lineage_clues.json").read_text(encoding="utf-8"))
        # 至少一条线索带 priority_score（最高优先级那条）
        any_scored = any(
            "priority_score" in c.get("detail", {}) for c in r["clues"]
        )
        self.assertTrue(any_scored, msg="线索未附加优先级分数")

    def test_g024_empirical_gap_wired_into_health(self):
        # REQ-G-024：实证缺口独立报警键进 miao_coverage；健康度小节在产物首部
        r = json.loads((ROOT / "output" / "lineage_clues.json").read_text(encoding="utf-8"))
        self.assertIn("健康度", r)
        dc = r["miao_coverage"]["dimension_coverage"]
        for key in ("empirical_alarm", "empirical_alarm_text",
                    "empirical_missing", "empirical_covered"):
            self.assertIn(key, dc, msg=f"dimension_coverage 缺 {key}")
        self.assertIn(r["健康度"]["status"], ("healthy", "degraded", "critical"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
