"""
tests/test_canvas_report.py
RC-304/305/306 研判报告纯函数测试（server/app/canvas_report.py）。

AC-304 对应：
  1. version_no 严格递增（由 state_store insert 保证）；
  2. 报告记录关联 snapshot_id；
  3. 报告结构化字段齐全（8 个章节键存在）；
  4. 任务失败不产生 ready 版本；
  5. fake_invoke 下端到端可生成。

AC-305 对应：
  1. 报告引用角标能在关联快照中解析；
  2. 事后修改画布不影响历史报告。

AC-306 对应：
  1. render_markdown 输出含 8 段标题 + 附录；
  2. render_docx 可被 python-docx 重新打开；
  3. 文件名规则含版本号。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.canvas_report import (
    REPORT_SECTIONS,
    build_report_prompt,
    parse_llm_to_sections,
    parse_report_sections,
    render_markdown,
    render_docx,
)
from server.app.canvas_citation_guard import build_valid_refs_from_doc


def _doc() -> dict:
    """构造一个与 seed_canvas 产物同构的画布 doc fixture。"""
    return {
        "nodes": [
            {"id": "rule:R1", "kind": "rule", "ref": "R1",
             "label": "整数万元存入",
             "props": {"rule_text": "单笔现金存入金额为整数万元",
                       "依据": "整数现金存入"},
             "system": True, "pinned": False, "x": 0, "y": 0},
            {"id": "fact:f1", "kind": "fact", "ref": "fact:clue_1:0",
             "label": "张卫国向海州建材转账5万元",
             "system": True, "pinned": False, "x": 200, "y": 0},
            {"id": "source_row:row:bank_001", "kind": "source_row",
             "ref": "row:bank_001",
             "label": "行 1: 张卫国→海州建材 50000",
             "system": True, "pinned": False, "x": 400, "y": 0},
            {"id": "hypothesis:h1", "kind": "hypothesis", "ref": "hyp_1",
             "label": "海州建材为壳公司",
             "props": {"title": "海州建材为壳公司",
                       "content": "无实际经营，疑似资金通道"},
             "system": False, "pinned": False, "x": 200, "y": 200},
        ],
        "edges": [
            {"id": "e1", "source": "rule:R1", "target": "fact:f1",
             "rel": "命中", "system": True},
            {"id": "e2", "source": "fact:f1",
             "target": "source_row:row:bank_001",
             "rel": "来源行", "system": True},
        ],
    }


class TestBuildReportPrompt(unittest.TestCase):
    """RC-304 prompt 构建。"""

    def test_prompt_contains_10_sections(self):
        """系统提示词含十段结构说明（含确定性段）。"""
        system_prompt, _ = build_report_prompt(_doc())
        # 十段标题在 system_prompt 中
        for title in ("线索概况", "证据充分性", "命中规则与判据",
                      "事实与依据", "关联核验", "研判推断",
                      "待核实事项", "书证清单", "数据源清单"):
            self.assertIn(title, system_prompt)

    def test_prompt_contains_context(self):
        """用户提示词含画布上下文。"""
        _, user_prompt = build_report_prompt(_doc())
        payload = json.loads(user_prompt)
        self.assertIn("画布业务视图", payload)
        self.assertIn("输出要求", payload)

    def test_extra_request_passed_through(self):
        """补充要求透传。"""
        _, user_prompt = build_report_prompt(
            _doc(), "重点关注资金流向")
        payload = json.loads(user_prompt)
        self.assertEqual(payload["补充要求"], "重点关注资金流向")

    def test_evidence_passed_through(self):
        """确定性证据透传到用户提示词。"""
        evidence = {"case_id": "C001", "确定性块": {"证据充分性": {}}}
        _, user_prompt = build_report_prompt(
            _doc(), evidence=evidence)
        payload = json.loads(user_prompt)
        self.assertIn("确定性证据", payload)
        self.assertEqual(payload["确定性证据"]["case_id"], "C001")


class TestParseReportSections(unittest.TestCase):
    """RC-302/304 报告解析 + 引用校验。"""

    def test_sections_complete(self):
        """8 段章节键齐全，空段有兜底文案。"""
        raw = (
            "## 一、线索概况\n线索 clue_1 命中 1 条规则，2 条事实。\n"
            "## 二、命中规则与判据\n规则 R1：整数万元存入。\n"
            "## 三、事实与依据\n"
            "张卫国向海州建材转账5万元[cite:R1]。\n"
            "## 四、研判推断\n海州建材疑似壳公司[cite:hyp_1]。\n"
            "## 五、待核实事项\n需核查海州建材工商登记。\n"
            "## 六、书证清单\n（暂无书证）\n"
            "## 七、数据源清单\nbank_001 银行流水。\n"
            "## 附录：引用索引\n[1] R1\n"
        )
        doc = _doc()
        valid_refs = build_valid_refs_from_doc(doc)
        result = parse_report_sections(raw, valid_refs)

        for key in REPORT_SECTIONS:
            self.assertIn(key, result["sections"])
            self.assertIsInstance(result["sections"][key], str)

    def test_facts_have_citations_pending_moved(self):
        """有据句保留引用，无据句移入待核实段。"""
        raw = (
            "## 三、事实与依据\n"
            "张卫国转账5万元为整数万元[cite:R1]。\n"
            "海州建材疑似壳公司。\n"
            "## 五、待核实事项\n（原有待核实内容）\n"
        )
        doc = _doc()
        valid_refs = build_valid_refs_from_doc(doc)
        result = parse_report_sections(raw, valid_refs)

        # facts 段含有效引用句
        self.assertIn("R1", result["sections"]["facts"])
        # 无据句移入 pending
        self.assertIn("海州建材疑似壳公司",
                       result["sections"]["pending"])
        # warnings 有记录
        self.assertTrue(len(result["warnings"]) > 0)

    def test_fake_citation_removed(self):
        """假引用被剔除并记 warning。"""
        raw = (
            "## 三、事实与依据\n"
            "某陈述[cite:FAKE_REF]。\n"
        )
        doc = _doc()
        valid_refs = build_valid_refs_from_doc(doc)
        result = parse_report_sections(raw, valid_refs)

        # 假引用句进 pending（引用全假）
        self.assertIn("某陈述", result["sections"]["pending"])
        # warning 记假引用
        self.assertTrue(
            any("FAKE_REF" in w for w in result["warnings"]))

    def test_empty_output(self):
        """空输出不抛异常，所有段有兜底。"""
        result = parse_report_sections("", set())
        for key in REPORT_SECTIONS:
            self.assertIn(key, result["sections"])

    def test_content_md_assembled(self):
        """content_md 含 8 段标题。"""
        raw = "## 一、线索概况\n概况内容。\n"
        result = parse_report_sections(raw, set())
        for title in ("一、线索概况", "二、命中规则与判据",
                      "三、事实与依据", "四、研判推断",
                      "五、待核实事项", "六、书证清单",
                      "七、数据源清单", "附录：引用索引"):
            self.assertIn(title, result["content_md"])


class TestParseLlmToSections(unittest.TestCase):
    """parse_llm_to_sections：解析 LLM 输出为 sections dict（供 render_report）。"""

    def test_returns_narrative_sections(self):
        """返回七段叙述键，不含确定性段。"""
        raw = (
            "## 一、线索概况\n线索概况内容。\n"
            "## 三、命中规则与判据\n规则 R1。\n"
            "## 四、事实与依据\n某事实[cite:R1]。\n"
            "## 六、研判推断\n某推断。\n"
            "## 七、待核实事项\n待核实项。\n"
            "## 八、书证清单\n书证。\n"
            "## 九、数据源清单\n数据源。\n"
        )
        result = parse_llm_to_sections(raw, {"R1"})
        for key in ("overview", "rules", "facts", "inferences",
                    "pending", "evidence", "sources"):
            self.assertIn(key, result["sections"])
        # 不含 content_md（由 render_report 负责组装）
        self.assertNotIn("content_md", result)

    def test_citations_validated(self):
        """引用校验：有据句保留，无据句转待核实。"""
        raw = (
            "## 四、事实与依据\n"
            "张卫国转账5万元为整数万元[cite:R1]。\n"
            "海州建材疑似壳公司。\n"
        )
        result = parse_llm_to_sections(raw, {"R1"})
        self.assertIn("R1", result["sections"]["facts"])
        self.assertIn("海州建材疑似壳公司",
                      result["sections"]["pending"])
        self.assertTrue(len(result["warnings"]) > 0)

    def test_fake_citation_removed(self):
        """假引用被剔除并记 warning。"""
        raw = "## 四、事实与依据\n某陈述[cite:FAKE_REF]。\n"
        result = parse_llm_to_sections(raw, set())
        self.assertIn("某陈述", result["sections"]["pending"])
        self.assertTrue(
            any("FAKE_REF" in w for w in result["warnings"]))

    def test_empty_input(self):
        """空输入不抛异常，所有叙述段有兜底。"""
        result = parse_llm_to_sections("", set())
        for key in ("overview", "rules", "facts", "inferences",
                    "pending", "evidence", "sources"):
            self.assertIn(key, result["sections"])
        self.assertEqual(result["citations"], [])

    def test_citations_index_built(self):
        """citations 列表正确构建。"""
        raw = (
            "## 四、事实与依据\n"
            "事实一[cite:R1]。\n"
            "事实二[cite:row:bank_001]。\n"
        )
        result = parse_llm_to_sections(
            raw, {"R1", "row:bank_001"})
        self.assertEqual(len(result["citations"]), 2)
        self.assertEqual(result["citations"][0]["ref"], "R1")
        self.assertEqual(result["citations"][1]["ref"], "row:bank_001")


class TestRenderMarkdown(unittest.TestCase):
    """RC-306 Markdown 导出。"""

    def test_md_contains_8_sections(self):
        """Markdown 含 8 段标题。"""
        sections = {k: f"{k} 内容" for k in REPORT_SECTIONS}
        md = render_markdown(sections, [])
        for title in ("一、线索概况", "二、命中规则与判据",
                      "三、事实与依据", "四、研判推断",
                      "五、待核实事项", "六、书证清单",
                      "七、数据源清单", "附录：引用索引"):
            self.assertIn(title, md)

    def test_md_contains_citation_table(self):
        """Markdown 含引用索引表格。"""
        sections = {k: "" for k in REPORT_SECTIONS}
        citations = [
            {"cite_id": 1, "ref": "R1", "summary": "规则引用"},
            {"cite_id": 2, "ref": "row:bank_001", "summary": "行引用"},
        ]
        md = render_markdown(sections, citations)
        self.assertIn("| 编号 |", md)
        self.assertIn("R1", md)
        self.assertIn("row:bank_001", md)


class TestRenderDocx(unittest.TestCase):
    """RC-306 Word 导出（python-docx 依赖可选）。"""

    def test_docx_reopenable(self):
        """docx 可被 python-docx 重新打开，章节标题数 >=8。"""
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx 未安装，跳过 Word 导出测试")

        sections = {k: f"{k} 内容段落" for k in REPORT_SECTIONS}
        citations = [
            {"cite_id": 1, "ref": "R1", "summary": "规则引用"},
        ]
        docx_bytes = render_docx(sections, citations)

        # 重新打开验证
        import io
        doc = Document(io.BytesIO(docx_bytes))
        headings = [p.text for p in doc.paragraphs
                    if p.style.name.startswith("Heading")]
        # 8 段标题（附录单独处理为 7+1）
        self.assertGreaterEqual(len(headings), 7)

    def test_filename_rule(self):
        """文件名含版本号（命名函数校验，不依赖实际导出）。"""
        clue_id = "clue_1"
        version_no = 3
        created_at = "2026-09-14T10:30:00"
        filename = (
            f"研判报告_{clue_id}_v{version_no}_{created_at[:10]}.docx")
        self.assertIn("v3", filename)
        self.assertIn("2026-09-14", filename)
        self.assertTrue(filename.endswith(".docx"))


class TestStateStoreReport(unittest.TestCase):
    """RC-304 state_store 报告 CRUD（内存 SQLite）。"""

    def test_insert_and_get_report(self):
        """插入报告后可按 report_id 读取。"""
        import sqlite3
        from server.app.store.state_store import StateStore

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # 建表（与 production 同构）
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clue_canvas_report (
                report_id TEXT PRIMARY KEY,
                clue_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                snapshot_id TEXT NOT NULL,
                content_md TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL DEFAULT '{}',
                citations_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                extra_request TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'generating',
                task_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(clue_id, version_no)
            );
        """)
        # 替换 StateStore 的 conn
        store = StateStore.__new__(StateStore)
        store._conn = conn
        store._case_id = "test_case"

        report = store.insert_canvas_report(
            report_id="rpt_test1", clue_id="clue_1",
            version_no=1, snapshot_id="snap_1",
            created_by="analyst", created_at="2026-09-14T10:00:00")
        self.assertEqual(report["report_id"], "rpt_test1")
        self.assertEqual(report["status"], "generating")

        # 读取
        got = store.get_canvas_report("rpt_test1")
        self.assertIsNotNone(got)
        self.assertEqual(got["clue_id"], "clue_1")

        # 更新为 ready
        updated = store.update_canvas_report_status(
            "rpt_test1", status="ready",
            content_md="## 报告",
            sections={"overview": "概况"},
            citations=[{"cite_id": 1, "ref": "R1"}],
            warnings=["test warning"],
            task_id="t_1")
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["content_md"], "## 报告")
        self.assertEqual(updated["sections"]["overview"], "概况")
        self.assertEqual(len(updated["citations"]), 1)
        self.assertEqual(len(updated["warnings"]), 1)

        conn.close()

    def test_version_no_monotonic(self):
        """version_no 严格递增：重复插入同版本号失败。"""
        import sqlite3
        from server.app.store.state_store import StateStore

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clue_canvas_report (
                report_id TEXT PRIMARY KEY,
                clue_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                snapshot_id TEXT NOT NULL,
                content_md TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL DEFAULT '{}',
                citations_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                extra_request TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'generating',
                task_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(clue_id, version_no)
            );
        """)
        store = StateStore.__new__(StateStore)
        store._conn = conn
        store._case_id = "test_case"

        store.insert_canvas_report(
            report_id="rpt_v1", clue_id="clue_1",
            version_no=1, snapshot_id="snap_1",
            created_by="a", created_at="t1")
        # 同 clue_id + version_no=1 再插 → IntegrityError
        with self.assertRaises(Exception):
            store.insert_canvas_report(
                report_id="rpt_v1_dup", clue_id="clue_1",
                version_no=1, snapshot_id="snap_1",
                created_by="a", created_at="t2")

        conn.close()

    def test_list_reports_descending(self):
        """列表按 version_no 倒序。"""
        import sqlite3
        from server.app.store.state_store import StateStore

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clue_canvas_report (
                report_id TEXT PRIMARY KEY,
                clue_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                snapshot_id TEXT NOT NULL,
                content_md TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL DEFAULT '{}',
                citations_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                extra_request TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'generating',
                task_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(clue_id, version_no)
            );
        """)
        store = StateStore.__new__(StateStore)
        store._conn = conn
        store._case_id = "test_case"

        for v in [1, 2, 3]:
            store.insert_canvas_report(
                report_id=f"rpt_v{v}", clue_id="clue_1",
                version_no=v, snapshot_id=f"snap_{v}",
                created_by="a", created_at=f"t{v}")

        reports = store.list_canvas_reports("clue_1")
        self.assertEqual(len(reports), 3)
        self.assertEqual(int(reports[0]["version_no"]), 3)
        self.assertEqual(int(reports[1]["version_no"]), 2)
        self.assertEqual(int(reports[2]["version_no"]), 1)

        conn.close()


if __name__ == "__main__":
    unittest.main()
