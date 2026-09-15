"""tests/test_sunzi_report_skill.py

sunzi-report skill 脚本（.trae/skills/sunzi-report/scripts/）纯函数测试：

1. 第九段「数据源清单」确定性采集 gather_evidence.collect_sources
   （ingest meta 登记口径 + 画布节点聚合 + 降级不静默）；
2. 第九段确定性渲染 render_report.render_sources / build_markdown
   （sections 提供 sources 必须被忽略）。

不依赖真实案件库与内核：用 tmp_path 构造 meta.db / state.sqlite / ontology。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / ".trae" / "skills" / "sunzi-report" / "scripts"


def _load_skill(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_sunzi_skill_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ge = _load_skill("gather_evidence")
rr = _load_skill("render_report")


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------

def _make_case_root(base: Path, *, bad_canvas: bool = False,
                    no_state: bool = False) -> Path:
    """构造与生产同构的最小案件根（ontology/meta/cases 三件套）。"""
    pack_dir = base / "ontology" / "default"
    pack_dir.mkdir(parents=True)
    (pack_dir / "objects.json").write_text(
        json.dumps({"schema_version": 2}), encoding="utf-8")

    meta_dir = base / "meta"
    meta_dir.mkdir(parents=True)
    conn = sqlite3.connect(meta_dir / "meta.db")
    conn.execute(
        """CREATE TABLE case_sources (
            case_id TEXT NOT NULL, upload_id TEXT NOT NULL,
            filename TEXT NOT NULL, fmt TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT '',
            table_name TEXT NOT NULL DEFAULT '',
            rows INTEGER NOT NULL DEFAULT 0,
            columns_json TEXT NOT NULL DEFAULT '{}',
            mapping_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'staged',
            parquet_path TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (case_id, upload_id))""")
    rows = [
        # c1：一条 imported（报告依据）+ 一条 staged（只计数）
        ("c1", "up_bid", "招投标档案.parquet", "parquet", "fp1",
         "招投标档案", 13, "{}", "{}", "imported", "", "demo",
         "2026-09-13T02:12:18"),
        ("c1", "up_staged", "人员信息.parquet", "parquet", "fp2",
         "", 22, "{}", "{}", "staged", "", "demo",
         "2026-09-13T03:00:00"),
        # c2：必须被 case_id 过滤掉
        ("c2", "up_other", "其他案件.parquet", "parquet", "fp3",
         "其他", 1, "{}", "{}", "imported", "", "demo",
         "2026-09-13T04:00:00"),
    ]
    conn.executemany(
        "INSERT INTO case_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    case_dir = base / "cases" / "c1"
    case_dir.mkdir(parents=True)
    if not no_state:
        doc = {
            "nodes": [
                # 一行 props.source 显式给 dataset
                {"kind": "source_row", "ref": "招投标档案@local#row/aaa",
                 "label": "招投标档案",
                 "props": {"source": "招投标档案"}},
                # 一行只给 ref 前缀（验证 dataset 兜底解析）
                {"kind": "source_row", "ref": "招投标档案@local#row/bbb",
                 "label": "招投标档案", "props": {}},
                # 文件节点：文件名/批次来源
                {"kind": "source_file", "label": "招投标档案.parquet",
                 "props": {"registered": True, "dataset": "招投标档案",
                           "upload_id": "up_bid"}},
                # 未登记文件节点不计入（registered=false）
                {"kind": "source_file", "label": "野文件.csv",
                 "props": {"registered": False, "dataset": "野数据"}},
            ]
        }
        doc_json = "{bad json" if bad_canvas else json.dumps(
            doc, ensure_ascii=False)
        conn = sqlite3.connect(case_dir / "state.sqlite")
        conn.execute("CREATE TABLE clue_canvas ("
                     "clue_id TEXT, version INTEGER, doc_json TEXT)")
        conn.executemany(
            "INSERT INTO clue_canvas VALUES (?,?,?)",
            [("clue_x", 1, "{}"),
             ("clue_x", 2, doc_json),
             ("clue_other", 9, "{}")])
        conn.commit()
        conn.close()
    return base


# ----------------------------------------------------------------------
# 采集
# ----------------------------------------------------------------------

class TestCollectSources(unittest.TestCase):

    def test_registered_and_clue_sources(self):
        with TemporaryDirectory() as td:
            root = _make_case_root(Path(td))
            degraded: list[str] = []
            block = ge.collect_sources(
                root, "c1", "clue_x", "default", 3, degraded)

            self.assertEqual(degraded, [])
            self.assertEqual(block["案件库"], "cases/c1/v3.duckdb")
            self.assertEqual(block["数据版本"], 3)
            self.assertEqual(block["本体包"], "ontology/default")
            self.assertEqual(block["本体版本"], 2)

            regs = block["登记数据源"]
            self.assertEqual(len(regs), 1)  # staged 不入清单，c2 被过滤
            r = regs[0]
            self.assertEqual(r["upload_id"], "up_bid")
            self.assertEqual(r["filename"], "招投标档案.parquet")
            self.assertEqual(r["table_name"], "招投标档案")
            self.assertEqual(r["rows"], 13)
            self.assertEqual(block["未导入批次计数"], 1)

            clue = block["本线索数据源"]
            self.assertEqual(len(clue), 1)
            cs = clue[0]
            self.assertEqual(cs["dataset"], "招投标档案")
            self.assertEqual(cs["filename"], "招投标档案.parquet")
            self.assertEqual(cs["upload_id"], "up_bid")
            self.assertEqual(cs["溯源行数"], 2)  # 两个 source_row 节点

    def test_no_clue_id_skips_canvas(self):
        with TemporaryDirectory() as td:
            root = _make_case_root(Path(td))
            degraded: list[str] = []
            block = ge.collect_sources(
                root, "c1", "", "default", 3, degraded)
            self.assertEqual(block["本线索数据源"], [])
            self.assertEqual(degraded, [])

    def test_missing_state_db_not_degraded(self):
        """无画布功能的案件（state.sqlite 不存在）：不降级、线索源为空。"""
        with TemporaryDirectory() as td:
            root = _make_case_root(Path(td), no_state=True)
            degraded: list[str] = []
            block = ge.collect_sources(
                root, "c1", "clue_x", "default", 1, degraded)
            self.assertEqual(block["本线索数据源"], [])
            self.assertEqual(degraded, [])

    def test_missing_meta_db_degraded(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "cases" / "c1").mkdir(parents=True)
            degraded: list[str] = []
            block = ge.collect_sources(
                root, "c1", "clue_x", "default", None, degraded)
            self.assertEqual(block["登记数据源"], [])
            self.assertTrue(
                any("数据源登记库不存在" in d for d in degraded), degraded)

    def test_bad_canvas_json_degraded(self):
        with TemporaryDirectory() as td:
            root = _make_case_root(Path(td), bad_canvas=True)
            degraded: list[str] = []
            block = ge.collect_sources(
                root, "c1", "clue_x", "default", 3, degraded)
            # 案件级登记仍可用
            self.assertEqual(len(block["登记数据源"]), 1)
            self.assertEqual(block["本线索数据源"], [])
            self.assertTrue(
                any("本线索数据源聚合不可用" in d for d in degraded), degraded)

    def test_missing_ontology_version_is_none(self):
        with TemporaryDirectory() as td:
            root = _make_case_root(Path(td))
            (root / "ontology" / "default" / "objects.json").unlink()
            degraded: list[str] = []
            block = ge.collect_sources(
                root, "c1", "", "default", 3, degraded)
            self.assertIsNone(block["本体版本"])
            self.assertEqual(degraded, [])


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------

def _full_block() -> dict:
    return {
        "案件库": "cases/c1/v3.duckdb",
        "数据版本": 3,
        "本体包": "ontology/default",
        "本体版本": 2,
        "登记数据源": [
            {"upload_id": "up_bid", "filename": "招投标档案.parquet",
             "fmt": "parquet", "table_name": "招投标档案", "rows": 13,
             "created_at": "2026-09-13T02:12:18"},
        ],
        "未导入批次计数": 1,
        "本线索数据源": [
            {"dataset": "招投标档案", "filename": "招投标档案.parquet",
             "upload_id": "up_bid", "溯源行数": 2},
        ],
    }


class TestRenderSources(unittest.TestCase):

    def test_deterministic_set_contains_sources(self):
        self.assertIn("sources", rr.DETERMINISTIC)

    def test_full_block_rendered(self):
        md = rr.render_sources({"数据源清单": _full_block()})
        for token in ("cases/c1/v3.duckdb", "数据版本 v3",
                      "ontology/default", "schema_version=2",
                      "已导入数据源批次：1 个",
                      "招投标档案.parquet", "up_bid",
                      "| 13 |", "2026-09-13 02:12",
                      "本线索画布引用数据源：1 个", "溯源行 2 条",
                      "另有 1 个批次已上传但未导入"):
            self.assertIn(token, md, token)

    def test_empty_registry(self):
        block = _full_block()
        block["登记数据源"] = []
        block["未导入批次计数"] = 0
        block["本线索数据源"] = []
        md = rr.render_sources({"数据源清单": block})
        self.assertIn("（无已导入批次登记）", md)
        self.assertNotIn("本线索画布引用数据源", md)

    def test_block_missing_is_unknown_not_empty(self):
        # 老 evidence / 采集异常：缺失 ≠ 无数据源（防误读）
        md = rr.render_sources({})
        self.assertIn("未知（采集端未提供登记）", md)

    def test_build_markdown_ignores_llm_sources(self):
        evidence = {"case_id": "c1", "clue_id": "clue_x",
                    "data_version": 3, "确定性块": {"数据源清单": _full_block()}}
        sections = {"sources": "LLM 自己拼凑的数据源清单"}
        md = rr.build_markdown(evidence, sections, "A")
        self.assertIn("## 九、数据源清单", md)
        self.assertIn("招投标档案.parquet", md)
        self.assertNotIn("LLM 自己拼凑的数据源清单", md)
        # 第九节确定性渲染，不走空段占位
        self.assertNotIn("## 九、数据源清单\n\n（本段无内容）", md)

    def test_type_b_and_c_keep_sources(self):
        evidence = {"case_id": "c1", "data_version": 3,
                    "确定性块": {"数据源清单": _full_block()}}
        for rtype in ("B", "C"):
            md = rr.build_markdown(evidence, {}, rtype)
            self.assertIn("## 九、数据源清单", md, rtype)
            self.assertIn("up_bid", md, rtype)


if __name__ == "__main__":
    unittest.main()
