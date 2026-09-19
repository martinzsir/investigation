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
from unittest import mock

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


def _add_image_evidence(base: Path, rows: list[dict],
                        *, legacy: bool = False) -> None:
    """向 cases/c1/state.sqlite 追加 image_evidence 表并插行。

    legacy=True 模拟老库（缺 title/detail/severity 三列），
    验证列白名单交集降级路径。
    """
    conn = sqlite3.connect(base / "cases" / "c1" / "state.sqlite")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS image_evidence (
            image_evidence_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            clue_id TEXT NOT NULL DEFAULT '',
            image_uri TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            model_score REAL,
            verifier TEXT NOT NULL DEFAULT '',
            verify_conclusion TEXT NOT NULL DEFAULT '',
            subject_type TEXT NOT NULL DEFAULT '',
            subject_id TEXT NOT NULL DEFAULT '',
            draft_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL)""")
    cols = ["image_evidence_id", "case_id", "clue_id", "image_uri", "model",
            "prompt_version", "model_score", "verifier", "verify_conclusion",
            "subject_type", "subject_id", "draft_id", "created_at"]
    if not legacy:
        for col in ("title", "detail", "severity"):
            conn.execute(f"ALTER TABLE image_evidence ADD COLUMN {col} TEXT")
        cols += ["title", "detail", "severity"]
    for r in rows:
        conn.execute(
            f"INSERT INTO image_evidence ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [r.get(c) for c in cols])
    conn.commit()
    conn.close()


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
                      "**已导入数据源批次**：1 个",
                      "招投标档案.parquet", "up_bid",
                      "| 13 |", "2026-09-13 02:12",
                      "**本线索画布引用数据源**：1 个", "溯源行 2 条",
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


# ----------------------------------------------------------------------
# 附录：引用索引（渲染生成）
# ----------------------------------------------------------------------

class TestCitationIndex(unittest.TestCase):

    def test_extract_refs_ordered_and_dedup(self):
        sections = {
            "overview": "无引用句",
            "facts": "- 甲事[cite:R1]\n- 乙事[cite:R2]\n- 再甲[cite:R1]",
            "inferences": "- 推[cite:R3]",
        }
        self.assertEqual(rr.extract_cite_refs(sections),
                         ["R1", "R2", "R3"])

    def test_deterministic_keys_not_scanned(self):
        # sections 试图以确定性键夹带引用：不扫描（防 LLM 经此注入索引）
        sections = {
            "sufficiency": "[cite:ghost1]",
            "correlation": "[cite:ghost2]",
            "sources": "[cite:ghost3]",
            "facts": "[cite:R1]",
        }
        self.assertEqual(rr.extract_cite_refs(sections), ["R1"])

    def test_rows_numbered_with_explicit_summary(self):
        sections = {
            "facts": "[cite:R2][cite:R1]",
            "citations": [
                {"cite_id": 9, "ref": "R1", "summary": "规则一摘要"},
                {"ref": "R9", "summary": "正文未引用条目"},
            ],
        }
        rows = rr.build_citation_rows(sections)
        # 正文出现序编号；摘要按 ref 合并；孤立显式条目追加末尾
        self.assertEqual([r["ref"] for r in rows], ["R2", "R1", "R9"])
        self.assertEqual([r["cite_id"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[1]["summary"], "规则一摘要")
        self.assertEqual(rows[0]["summary"], "")

    def test_keep_set_excludes_dropped_sections(self):
        # C 类剔除 inferences/pending：其中引用不进索引
        sections = {
            "facts": "[cite:R1]",
            "inferences": "[cite:R2]",
            "pending": "[cite:R3]",
        }
        keep = rr.TYPE_KEEP["C"]
        self.assertEqual(rr.extract_cite_refs(sections, keep), ["R1"])

    def test_appendix_always_rendered_all_types(self):
        evidence = {"case_id": "c1", "data_version": 3}
        for rtype in ("A", "B", "C"):
            md = rr.build_markdown(evidence, {}, rtype)
            self.assertIn("## 附录：引用索引", md, rtype)
            self.assertIn("（本段无内容）", md, rtype)

    def test_appendix_renders_extracted_rows(self):
        evidence = {"case_id": "c1", "data_version": 3}
        sections = {"facts": "- 测试路整数资金[cite:9f5b55e5d111]",
                    "pending": "- 待核实[cite:vi_test]"}
        md = rr.build_markdown(evidence, sections, "A")
        self.assertIn("| 1 | 9f5b55e5d111 |  |", md)
        self.assertIn("| 2 | vi_test |  |", md)
        self.assertNotIn("附录：引用索引\n\n（本段无内容）", md)


class TestClueRefs(unittest.TestCase):
    """P7 collect_clue_refs + 第五段确定性渲染。"""

    def _write_artifact(self, base: Path, clues: list[dict],
                        version: int = 1) -> None:
        art = base / "cases" / "c1" / "artifacts"
        art.mkdir(parents=True, exist_ok=True)
        (art / f"clues_v{version}.json").write_text(
            json.dumps({"version": version, "clues": clues}),
            encoding="utf-8")

    def test_none_when_no_artifact(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            (base / "cases" / "c1").mkdir(parents=True)
            out = ge.collect_clue_refs(base, "c1", "", [])
            self.assertIsNone(out)

    def test_groups_by_kind_and_clue_filter(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            clue = {
                "clue_id": "clue-9", "title": "开标前资金碰撞",
                "evidence_refs": [
                    {"kind": "node", "ref": "obj_person#p1"},
                    {"kind": "edge", "ref": "lnk_transfers#t_001"},
                    {"kind": "time_window", "ref": "lnk_time_window#proj_2",
                     "time_from": "2026-08-01", "time_to": "2026-09-10"},
                    {"kind": "aggregate", "metric": "hit_amount",
                     "value": 4800000},
                    {"kind": "file", "file_uri": "cases/c1/files/f01.png",
                     "verify_status": "待核验"},
                ],
            }
            self._write_artifact(base, [clue])
            out = ge.collect_clue_refs(base, "c1", "", [])
            self.assertIsNotNone(out)
            self.assertEqual(out["artifact_version"], 1)
            self.assertEqual(out["线索数"], 1)
            self.assertEqual(out["节点引用"][0]["type"], "person")
            self.assertEqual(out["节点引用"][0]["key"], "p1")
            self.assertEqual(out["边引用"][0]["edge_pk"], "t_001")
            self.assertEqual(out["时间窗"][0]["time_from"], "2026-08-01")
            self.assertEqual(out["聚合量"][0]["value"], 4800000)
            self.assertEqual(out["书证引用"][0]["verify_status"], "待核验")

            # clue_id 过滤：不匹配 → 空组但块仍存在
            out2 = ge.collect_clue_refs(base, "c1", "clue-other", [])
            self.assertEqual(out2["线索数"], 0)
            self.assertEqual(out2["节点引用"], [])

    def test_bad_artifact_degraded(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            art = base / "cases" / "c1" / "artifacts"
            art.mkdir(parents=True)
            (art / "clues_v1.json").write_text("{不是 JSON", encoding="utf-8")
            degraded: list[str] = []
            out = ge.collect_clue_refs(base, "c1", "", degraded)
            self.assertEqual(out["线索数"], 0)
            self.assertTrue(degraded)

    def test_render_in_correlation(self):
        refs = {
            "artifact_version": 1, "线索数": 1,
            "节点引用": [{"线索标题": "资金碰撞", "type": "person", "key": "p1"}],
            "边引用": [{"线索标题": "资金碰撞", "type": "transfers",
                        "edge_pk": "t_001"}],
            "时间窗": [], "聚合量": [
                {"线索标题": "资金碰撞", "metric": "hit_amount",
                 "value": 4800000}],
            "书证引用": [{"线索标题": "资金碰撞",
                          "file_uri": "files/f01.png",
                          "verify_status": "待核验"}],
        }
        evidence = {"case_id": "c1", "data_version": 1,
                    "确定性块": {"线索证据引用": refs}}
        md = rr.build_markdown(evidence, {}, "A")
        self.assertIn("线索图谱/资金与书证引用", md)
        self.assertIn("person#p1", md)
        self.assertIn("lnk_transfers#t_001", md)
        self.assertIn("hit_amount = 4800000", md)
        self.assertIn("files/f01.png", md)
        self.assertIn("待核验", md)

    def test_render_none_refs_says_not_produced(self):
        evidence = {"case_id": "c1", "data_version": 1,
                    "确定性块": {"线索证据引用": None}}
        md = rr.build_markdown(evidence, {}, "A")
        self.assertIn("案件尚未产出线索产物", md)

    # ---- P8 回流并集：人验图像证据 → 书证引用 ------------------------

    _imrow = {
        "image_evidence_id": "imgev_a1", "case_id": "c1",
        "clue_id": "clue_x", "image_uri": "evidence/mat_01/invoide.png",
        "verify_conclusion": "确认", "verifier": "正兵-张三",
        "created_at": "2026-09-20T10:00:00",
    }

    def test_image_evidence_merged_into_file_refs(self):
        """图像核验发生在产物产出之后：报告侧按线索确定性并入书证引用。"""
        with TemporaryDirectory() as d:
            base = _make_case_root(Path(d))
            clue = {"clue_id": "clue_x", "title": "资金碰撞",
                    "evidence_refs": [
                        {"kind": "node", "ref": "obj_person#p1"}]}
            self._write_artifact(base, [clue])
            _add_image_evidence(base, [self._imrow])
            out = ge.collect_clue_refs(base, "c1", "clue_x", [])
            files = out["书证引用"]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["file_uri"],
                             "evidence/mat_01/invoide.png")
            self.assertEqual(files[0]["verify_status"], "确认")
            self.assertEqual(files[0]["verifier"], "正兵-张三")
            self.assertEqual(files[0]["线索标题"], "资金碰撞")
            self.assertEqual(files[0]["线索"], "clue_x")

    def test_image_evidence_dedup_artifact_wins(self):
        """同 file_uri：镜头产物条目优先，不重复计数。"""
        with TemporaryDirectory() as d:
            base = _make_case_root(Path(d))
            clue = {"clue_id": "clue_x", "title": "资金碰撞",
                    "evidence_refs": [
                        {"kind": "file",
                         "file_uri": "evidence/mat_01/invoide.png",
                         "verify_status": "待核验"}]}
            self._write_artifact(base, [clue])
            _add_image_evidence(base, [self._imrow])
            out = ge.collect_clue_refs(base, "c1", "clue_x", [])
            files = out["书证引用"]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["verify_status"], "待核验")
            self.assertNotIn("verifier", files[0])

    def test_image_evidence_without_artifact_returns_none(self):
        """P7 契约保持：无线索产物仍返回 None（图像证据只走八段块）。"""
        with TemporaryDirectory() as d:
            base = _make_case_root(Path(d))
            _add_image_evidence(base, [self._imrow])
            self.assertIsNone(ge.collect_clue_refs(base, "c1", "", []))

    def test_image_refs_render_in_correlation(self):
        """并集后的书证引用在第五段确定性渲染（核验状态随行）。"""
        with TemporaryDirectory() as d:
            base = _make_case_root(Path(d))
            clue = {"clue_id": "clue_x", "title": "资金碰撞",
                    "evidence_refs": [
                        {"kind": "node", "ref": "obj_person#p1"}]}
            self._write_artifact(base, [clue])
            _add_image_evidence(base, [self._imrow])
            refs = ge.collect_clue_refs(base, "c1", "clue_x", [])
            evidence = {"case_id": "c1", "data_version": 1,
                        "确定性块": {"线索证据引用": refs}}
            md = rr.build_markdown(evidence, {}, "A")
            self.assertIn("- 书证引用：1 处", md)
            self.assertIn(
                "evidence/mat_01/invoide.png（核验状态：确认）", md)


class TestCollectImageEvidence(unittest.TestCase):
    """P8 图像证据采集（state.sqlite image_evidence，列白名单交集）。"""

    _row = {
        "image_evidence_id": "imgev_a1", "case_id": "c1",
        "clue_id": "clue_x", "image_uri": "evidence/mat_01/invoide.png",
        "model": "qwen-vl-max", "prompt_version": "pvlm-3",
        "model_score": 0.87, "title": "发票抬头与中标单位不一致",
        "detail": "发票开具方为 A建材", "severity": "warn",
        "verifier": "正兵-张三", "verify_conclusion": "确认",
        "subject_type": "person", "subject_id": "张卫国",
        "draft_id": "d1", "created_at": "2026-09-20T10:00:00",
    }

    def test_full_columns_and_clue_filter(self):
        with TemporaryDirectory() as td:
            base = _make_case_root(Path(td))
            other = dict(self._row, image_evidence_id="imgev_b2",
                         clue_id="clue_other",
                         created_at="2026-09-20T11:00:00")
            _add_image_evidence(base, [self._row, other])
            rows = ge.collect_image_evidence(base, "c1", "clue_x")
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["image_evidence_id"], "imgev_a1")
            self.assertEqual(r["title"], "发票抬头与中标单位不一致")
            self.assertEqual(r["severity"], "warn")
            self.assertEqual(r["verify_conclusion"], "确认")
            self.assertEqual(r["verifier"], "正兵-张三")

    def test_newest_first_ordering(self):
        with TemporaryDirectory() as td:
            base = _make_case_root(Path(td))
            newer = dict(self._row, image_evidence_id="imgev_new",
                         created_at="2026-09-21T09:00:00")
            _add_image_evidence(base, [self._row, newer])
            rows = ge.collect_image_evidence(base, "c1", "clue_x")
            self.assertEqual([r["image_evidence_id"] for r in rows],
                             ["imgev_new", "imgev_a1"])

    def test_legacy_columns_no_crash(self):
        """老库缺 title/detail/severity：行不含这些键，不炸不降级。"""
        with TemporaryDirectory() as td:
            base = _make_case_root(Path(td))
            _add_image_evidence(base, [self._row], legacy=True)
            rows = ge.collect_image_evidence(base, "c1", "clue_x")
            self.assertEqual(len(rows), 1)
            self.assertNotIn("title", rows[0])
            self.assertNotIn("severity", rows[0])
            self.assertEqual(rows[0]["image_evidence_id"], "imgev_a1")

    def test_no_state_db_returns_empty(self):
        with TemporaryDirectory() as td:
            base = _make_case_root(Path(td), no_state=True)
            self.assertEqual(ge.collect_image_evidence(base, "c1", ""), [])

    def test_missing_table_returns_empty(self):
        # state.sqlite 只有 clue_canvas：image_evidence 表不存在 → []
        with TemporaryDirectory() as td:
            base = _make_case_root(Path(td))
            self.assertEqual(ge.collect_image_evidence(base, "c1", ""), [])

    def test_safe_wrapper_degrades_on_error(self):
        with TemporaryDirectory() as td:
            base = _make_case_root(Path(td))
            degraded: list[str] = []
            with mock.patch.object(ge, "collect_image_evidence",
                                   side_effect=RuntimeError("boom")):
                rows = ge.collect_image_evidence_safe(
                    base, "c1", "", degraded)
            self.assertEqual(rows, [])
            self.assertTrue(
                any("图像证据不可读" in d for d in degraded), degraded)
            # 无 mock 时正常透传（无 image_evidence 表 → []）
            self.assertEqual(
                ge.collect_image_evidence_safe(base, "c1", "", []), [])


class TestRenderImageEvidence(unittest.TestCase):
    """P8 八段确定性注入：图像证据在前，LLM 叙述在后。"""

    _row = {
        "image_evidence_id": "imgev_a1",
        "image_uri": "evidence/mat_01/invoide.png",
        "title": "发票抬头与中标单位不一致",
        "severity": "warn", "verify_conclusion": "确认",
        "verifier": "正兵-张三",
    }

    def _evidence(self, rows: list[dict]) -> dict:
        return {"case_id": "c1", "clue_id": "clue_x", "data_version": 1,
                "确定性块": {"图像证据": rows}}

    def test_injected_before_narrative(self):
        sections = {"evidence": "另有纸质书证两册。"}
        md = rr.build_markdown(self._evidence([self._row]), sections, "A")
        self.assertIn("## 八、书证清单", md)
        self.assertIn("**图像证据（人验）**：1 条", md)
        self.assertIn("| imgev_a1 | invoide.png | 发票抬头与中标单位不一致"
                      " | warn | 确认 | 正兵-张三 |", md)
        # 确定性小节在前、叙述在后
        self.assertLess(md.index("图像证据（人验）"),
                        md.index("另有纸质书证两册"))

    def test_empty_and_missing_block_no_noise(self):
        """空列表/老 evidence 无此块：八段退回纯叙述，不产生噪声。"""
        sections = {"evidence": "纯叙述书证清单。"}
        for block in ({}, {"图像证据": []}):
            ev = {"case_id": "c1", "data_version": 1, "确定性块": block}
            md = rr.build_markdown(ev, sections, "A")
            self.assertNotIn("图像证据（人验）", md)
            self.assertIn("纯叙述书证清单。", md)

    def test_all_empty_renders_placeholder(self):
        md = rr.build_markdown({"case_id": "c1", "data_version": 1}, {}, "A")
        self.assertIn("## 八、书证清单\n\n（本段无内容）", md)

    def test_type_b_and_c_keep_image_evidence(self):
        """八段在 B/C 类保留——移送固证报告恰好需要人验过的图像证据。"""
        for rtype in ("B", "C"):
            md = rr.build_markdown(self._evidence([self._row]), {}, rtype)
            self.assertIn("图像证据（人验）", md, rtype)
            self.assertIn("invoide.png", md, rtype)


if __name__ == "__main__":
    unittest.main()
