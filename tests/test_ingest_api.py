"""
tests/test_ingest_api.py
M3 阶段 E：W-010 数据源注册与列映射向导 + W-012 导入任务幂等。

AC 对应：
  AC-1 五格式上传（CSV/Excel/Parquet/JSON/SQLite）→ 暂存+指纹+列画像；
  W-012 AC-1 同指纹（同名同内容同行数）导入拦截 409；
  AC-2 同内容异名放行；AC-3 同名异内容放行；AC-4 判定阶段无任务行；
  AC-5 失败零残留（映射非法不落冷层）；
  AC-6 清洗声明随映射落 bindings（过 loader 校验）；
  端到端：导入→冷层 parquet→链式 BUILD→obj_transaction 物化。
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool

# 银行流水声明列：主体/对方/金额/日期（bindings transaction）
FLOW_COLS = ["主体", "对方", "金额", "日期"]


def flow_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "付款方": [f"张某{i}" for i in range(n)],
        "收款方": [f"李某{i}" for i in range(n)],
        "金额（元）": [str(10000 * (i + 1)) for i in range(n)],
        "交易日期": [f"2026-03-{28 + i:02d}" for i in range(n)],
    })


FLOW_MAP = {"付款方": "主体", "收款方": "对方",
            "金额（元）": "金额", "交易日期": "日期"}


class IngestApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.app = create_app(self.ctx)
        self.client = TestClient(self.app)
        salt, h = hash_password("pw-pro")
        self.repo.create_user(User(operator="王检察官", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        salt2, h2 = hash_password("pw-sol")
        self.repo.create_user(User(operator="李侦查员", password_hash=h2,
                                   salt=salt2, role="正兵", clearance=1,
                                   tenant_id="t1"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _upload(self, filename: str, content: bytes,
                headers=None) -> dict:
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=headers or self.auth_h,
            files={"file": (filename, content)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def _import(self, upload_id: str, target: str = "银行流水",
                column_map=None, clean=None, headers=None):
        body = {"target_table": target,
                "column_map": column_map or FLOW_MAP}
        if clean:
            body["clean"] = clean
        return self.client.post(
            f"/api/v1/cases/c1/sources/{upload_id}/import",
            headers=headers or self.auth_h, json=body)

    def _drain(self):
        self.pool.run_until_drained(max_idle_rounds=30)

    # ------------------------------------------------------------------
    # AC-1：五格式上传
    # ------------------------------------------------------------------
    def test_upload_five_formats(self):
        df = flow_df()
        # CSV
        up = self._upload("流水.csv", df.to_csv(index=False).encode("utf-8"))
        self.assertEqual(up["format"], "csv")
        self.assertEqual(up["rows"], 3)
        self.assertIn("银行流水", up["declared_tables"])
        self.assertEqual(set(up["columns"]),
                         {"付款方", "收款方", "金额（元）", "交易日期"})
        # Excel
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        up = self._upload("流水.xlsx", buf.getvalue())
        self.assertEqual(up["format"], "excel")
        self.assertEqual(up["rows"], 3)
        # Parquet
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        up = self._upload("流水.parquet", buf.getvalue())
        self.assertEqual(up["format"], "parquet")
        # JSON
        up = self._upload("流水.json",
                          df.to_json(orient="records", force_ascii=False)
                          .encode("utf-8"))
        self.assertEqual(up["format"], "json")
        # SQLite
        tmpdb = Path(tempfile.mkdtemp()) / "s.db"
        con = sqlite3.connect(str(tmpdb))
        df.to_sql("银行流水表", con, index=False)
        con.commit()
        con.close()
        up = self._upload("流水.db", tmpdb.read_bytes())
        self.assertEqual(up["format"], "sqlite")
        self.assertEqual(up["rows"], 3)

    def test_upload_unsupported_format_400(self):
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("木马.exe", b"MZ\x90\x00")})
        self.assertEqual(r.status_code, 400)

    def test_upload_semicolon_csv_split_columns(self):
        """分号分隔 CSV（中文 Excel/银行导出常见）：表头不得塌成一列。

        回归：read_table 曾硬编码 sep=','，分号分隔文件的整行表头
        "日期;主体;对方;金额" 被当成单个列名，向导映射页只剩一个源列选项，
        四个声明列全部退化为 60% 模糊匹配。
        """
        content = "日期;主体;对方;金额\n" + "\n".join(
            f"2026-03-{28 + i:02d};张某{i};李某{i};{10000 * (i + 1)}"
            for i in range(3)) + "\n"
        up = self._upload("流水分号.csv", content.encode("utf-8"))
        self.assertEqual(up["rows"], 3)
        self.assertEqual(set(up["columns"]),
                         {"日期", "主体", "对方", "金额"})
        # analyze：四列精确命中，不再退化为整列模糊 60%
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up['upload_id']}/analyze",
            headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 200, r.text)
        sug = r.json()["data"]["suggestion"]
        self.assertEqual(sug["target_table"], "银行流水")
        match_types = {m["target_prop"]: m["match_type"]
                       for m in sug["matches"]}
        for col in FLOW_COLS:
            self.assertEqual(match_types.get(col), "exact", col)
        self.assertEqual(sug["missing_required"], [])

    def test_upload_parquet_nulls_not_literal_nan(self):
        """Parquet 空值不得字符串化为字面量 "nan"（与 CSV 空串口径一致）。

        回归：read_parquet(...).astype(str) 把 null/None/NaT 全部变成
        "nan" 文本，污染列画像并随冷层落地（主体名变 "nan"、数值列
        TRY_CAST 误报）。
        """
        df = pd.DataFrame({
            "付款方": ["张某0", None, "张某2"],
            "收款方": ["李某0", "李某1", None],
            "金额（元）": ["10000", None, "30000"],
            "交易日期": ["2026-03-28", "2026-03-29", None],
        })
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        up = self._upload("流水.parquet", buf.getvalue())
        self.assertEqual(up["rows"], 3)
        # 列画像：样本不得出现字面量 "nan"/"None"
        for col, prof in up["columns"].items():
            self.assertNotIn("nan", prof["samples"], col)
            self.assertNotIn("None", prof["samples"], col)
        # 端到端导入冷层：空值落为空（null/空串），不是 "nan"
        r = self._import(up["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        cold = pd.read_parquet(
            self.factory.case_dir("c1") / "cold" / "银行流水.parquet")
        lit = cold.fillna("").astype(str)
        self.assertFalse((lit == "nan").any().any(), "冷层出现字面量 nan")
        self.assertFalse((lit == "None").any().any(), "冷层出现字面量 None")
        # 非空值完好
        self.assertIn("张某0", set(cold["主体"].dropna()))
        self.assertIn("李某1", set(cold["对方"].dropna()))

    def test_upload_json_nulls_jsonl_content_and_suffix(self):
        """JSON 三形态：records 含 null 归一空串 / .json 内容实为 JSONL /
        .jsonl 后缀放行。"""
        # 1) records 数组 + null
        payload = json.dumps([
            {"付款方": "张某0", "收款方": "李某0",
             "金额（元）": "10000", "交易日期": "2026-03-28"},
            {"付款方": None, "收款方": "李某1",
             "金额（元）": None, "交易日期": "2026-03-29"},
        ], ensure_ascii=False)
        up = self._upload("流水.json", payload.encode("utf-8"))
        self.assertEqual(up["format"], "json")
        self.assertEqual(up["rows"], 2)
        for col, prof in up["columns"].items():
            self.assertNotIn("nan", prof["samples"], col)
        # 2) .json 后缀但内容是行分隔 JSONL（暂存统一改名 .json，靠内容双试）
        jsonl = (
            '{"付款方":"张某0","收款方":"李某0","金额（元）":"10000",'
            '"交易日期":"2026-03-28"}\n'
            '{"付款方":"张某1","收款方":"李某1","金额（元）":"20000",'
            '"交易日期":"2026-03-29"}\n'
        )
        up2 = self._upload("流水行.json", jsonl.encode("utf-8"))
        self.assertEqual(up2["rows"], 2)
        self.assertEqual(set(up2["columns"]),
                         {"付款方", "收款方", "金额（元）", "交易日期"})
        # 3) .jsonl 后缀直接放行
        up3 = self._upload("流水.jsonl", jsonl.encode("utf-8"))
        self.assertEqual(up3["format"], "json")
        self.assertEqual(up3["rows"], 2)

    def test_upload_nested_json_flattened(self):
        """嵌套包裹 JSON（{"data":{"records":[...]}}，API 响应式结构）须展平。

        回归：pd.read_json 把外层 key 当列名、内层 dict 当单元格字符串，
        通话记录.json 的 2 条记录塌成 1 行 1 列——向导无法映射，BUILD
        必填列缺失硬失败。展平后应为 2 行 3 列且无塌缩预警。
        """
        payload = json.dumps({
            "data": {"records": [
                {"caller": "张卫国", "callee": "李志强", "count": 12},
                {"caller": "张卫国", "callee": "王五", "count": 3},
            ]}}, ensure_ascii=False)
        up = self._upload("通话记录.json", payload.encode("utf-8"))
        self.assertEqual(up["format"], "json")
        self.assertEqual(up["rows"], 2)
        self.assertEqual(set(up["columns"]),
                         {"caller", "callee", "count"})
        self.assertFalse(up.get("warning"))
        # 记录间缺键补齐为空串（不产 "nan"）
        self.assertEqual(up["columns"]["caller"]["samples"],
                         ["张卫国", "张卫国"])

    def test_upload_nested_json_other_wrapper_keys(self):
        """其他常见包裹键（result/items）+ 更深层级同样展平；顶层数组直通。"""
        payload = json.dumps({"result": {"items": [
            {"主体": "张某0", "对方": "李某0", "金额": "100", "日期": "2026-03-28"},
        ]}}, ensure_ascii=False)
        up = self._upload("流水.json", payload.encode("utf-8"))
        self.assertEqual(up["rows"], 1)
        self.assertEqual(set(up["columns"]),
                         {"主体", "对方", "金额", "日期"})
        # 顶层 records 数组（无包裹）行为不变
        up2 = self._upload("流水2.json", json.dumps([
            {"主体": "张某0", "对方": "李某0", "金额": "100", "日期": "2026-03-28"},
        ], ensure_ascii=False).encode("utf-8"))
        self.assertEqual(up2["rows"], 1)
        self.assertEqual(set(up2["columns"]),
                         {"主体", "对方", "金额", "日期"})

    def test_upload_collapse_warning_double_encoded(self):
        """records 是双重编码 JSON 字符串（无法展平）→ 1x1 塌缩预警。

        预警把"结构化数据被当成纯文本"暴露在向导页之前，避免用户对着
        一个源列硬配、直到 BUILD 才报必填列缺失。
        """
        inner = json.dumps([{"caller": "张卫国", "callee": "李志强"}],
                           ensure_ascii=False)
        payload = json.dumps({"data": {"records": inner}}, ensure_ascii=False)
        up = self._upload("通话坏.json", payload.encode("utf-8"))
        self.assertEqual(up["rows"], 1)
        self.assertEqual(list(up["columns"]), ["data"])
        self.assertTrue(up["warning"])
        self.assertIn("1 行 1 列", up["warning"])

    # ------------------------------------------------------------------
    # 端到端：导入→冷层→BUILD→语义表
    # ------------------------------------------------------------------
    def test_import_e2e_cold_and_build(self):
        up = self._upload("流水.csv",
                          flow_df(4).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        tasks = self.repo.list_tasks(case_id="c1")
        by_type = {t.task_type: t for t in tasks}
        self.assertEqual(by_type["IMPORT"].status, TASK_SUCCEEDED,
                         f"{by_type['IMPORT'].error_code} "
                         f"{by_type['IMPORT'].error_message}")
        self.assertEqual(by_type["BUILD"].status, TASK_SUCCEEDED,
                         f"{by_type['BUILD'].error_code} "
                         f"{by_type['BUILD'].error_message}")
        # 冷层 parquet 存在（原子落盘）
        cold = self.factory.case_dir("c1") / "cold" / "银行流水.parquet"
        self.assertTrue(cold.exists())
        # 注册表置 imported
        src = self.repo.get_source("c1", up["upload_id"])
        self.assertEqual(src["status"], "imported")
        # BUILD 产物：obj_transaction 物化为 4 行
        self.assertEqual(self.repo.current_version("c1"), 1)
        store = self.factory.for_case("c1", mode="read")
        try:
            n = store.read_conn.execute(
                "SELECT COUNT(*) FROM obj_transaction").fetchone()[0]
            self.assertEqual(n, 4)
        finally:
            store.close()

    def test_import_collapsed_json_hard_fails(self):
        """worker 落库前塌缩硬闸：1x1 结构化串直接失败，不落冷层、不入队 BUILD。

        回归事故（2026-09-10）：常驻 worker 未随 ingest_io 展平修复重启，旧代码
        把 {"data":{"records":[...]}} 读成 1 行 1 列 data 并照常落冷层，
        用户直到链式 BUILD 才看到晦涩的 BinderException（缺列）。双重编码 JSON
        字符串（records 本身是串）在新版解析下同样塌缩——硬闸须快速失败并给
        可操作中文诊断。
        """
        inner = json.dumps([
            {"caller": "张卫国", "callee": "李志强", "count": 12},
            {"caller": "张卫国", "callee": "王五", "count": 3},
        ], ensure_ascii=False)
        payload = json.dumps({"data": {"records": inner}}, ensure_ascii=False)
        up = self._upload("通话记录.json", payload.encode("utf-8"))
        self.assertEqual(up["rows"], 1)  # upload 阶段：1x1 + 软预警
        self.assertIn("1 行 1 列", up.get("warning") or "")

        # 映射目标合法（主体在银行流水声明列内），通过映射校验后须撞硬闸
        r = self._import(up["upload_id"], target="银行流水",
                         column_map={"data": "主体"})
        self.assertEqual(r.status_code, 200, r.text)  # 导入任务已受理
        self._drain()

        tasks = self.repo.list_tasks(case_id="c1")
        imports = [t for t in tasks if t.task_type == "IMPORT"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0].status, TASK_FAILED)
        self.assertEqual(imports[0].error_code, "SUSPECT_COLLAPSE")
        self.assertIn("1 行 1 列", imports[0].error_message)
        # 零残留：冷层无 parquet，且未链式入队 BUILD
        self.assertFalse(
            (self.factory.case_dir("c1") / "cold" / "银行流水.parquet").exists())
        self.assertFalse(any(t.task_type == "BUILD" for t in tasks))
        self.assertNotEqual(
            self.repo.get_source("c1", up["upload_id"])["status"], "imported")

    def test_import_nested_json_flattened_then_build(self):
        """正面回归：嵌套包裹 JSON 经导入任务落冷层（2 行）并链式 BUILD 成功。

        与硬闸测试成对——证明展平链路在 worker 侧真实生效，不只是 upload 列画像。
        """
        payload = json.dumps({"data": {"records": [
            {"号码": "张卫国", "对端号码": "李志强", "日期": "2026-03-28", "次数": "12"},
            {"号码": "张卫国", "对端号码": "王五", "日期": "2026-03-29", "次数": "3"},
        ]}}, ensure_ascii=False)
        up = self._upload("通话记录.json", payload.encode("utf-8"))
        self.assertEqual(up["rows"], 2)
        r = self._import(up["upload_id"], target="通话记录",
                         column_map={"号码": "主体", "对端号码": "对端",
                                     "次数": "次数"})
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        tasks = self.repo.list_tasks(case_id="c1")
        by_type = {t.task_type: t for t in tasks}
        self.assertEqual(by_type["IMPORT"].status, TASK_SUCCEEDED,
                         f"{by_type['IMPORT'].error_code} "
                         f"{by_type['IMPORT'].error_message}")
        self.assertEqual(by_type["BUILD"].status, TASK_SUCCEEDED,
                         f"{by_type['BUILD'].error_code} "
                         f"{by_type['BUILD'].error_message}")
        cold = self.factory.case_dir("c1") / "cold" / "通话记录.parquet"
        self.assertTrue(cold.exists())
        got = pd.read_parquet(cold)
        self.assertEqual(len(got), 2)
        self.assertIn("主体", got.columns)
        self.assertIn("对端", got.columns)

    def test_retry_failed_import_runs_full_chain(self):
        """FAILED IMPORT 经 retry 端点重新入队（同 upload_id、源状态回 queued），
        重试走完全部 worker 链路：导入成功 + 链式 BUILD 成功。旧任务留痕。"""
        payload = json.dumps({"data": {"records": [
            {"号码": "张卫国", "对端号码": "李志强", "日期": "2026-03-28", "次数": "12"},
            {"号码": "张卫国", "对端号码": "王五", "日期": "2026-03-29", "次数": "3"},
        ]}}, ensure_ascii=False)
        up = self._upload("通话记录.json", payload.encode("utf-8"))
        uid = up["upload_id"]
        r = self._import(uid, target="通话记录",
                         column_map={"号码": "主体", "对端号码": "对端",
                                     "次数": "次数"})
        self.assertEqual(r.status_code, 200, r.text)
        old_id = r.json()["data"]["id"]
        # 模拟 worker 执行前的瞬时失败（源已在 queued）
        self.repo.fail_task(old_id, error_code="WORKER_DIED",
                            error_message="worker 进程被杀死")
        self.assertEqual(self.repo.get_source("c1", uid)["status"], "queued")

        r = self.client.post(f"/api/v1/tasks/{old_id}/retry",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        new_task = r.json()["data"]
        self.assertNotEqual(new_task["id"], old_id)
        self.assertEqual(new_task["task_type"], "IMPORT")
        self.assertEqual(new_task["status"], "PENDING")
        self.assertEqual(new_task["params"]["upload_id"], uid)

        self._drain()
        tasks = self.repo.list_tasks(case_id="c1")
        by_type = {}
        for t in tasks:
            if t.status == TASK_SUCCEEDED:
                by_type[t.task_type] = t
        self.assertEqual(by_type["IMPORT"].id, new_task["id"])
        self.assertEqual(by_type["BUILD"].status, TASK_SUCCEEDED,
                         f"{by_type.get('BUILD') and by_type['BUILD'].error_message}")
        # 旧任务保留 FAILED 留痕；源最终 imported；重试已成功任务 → 409
        self.assertEqual(self.repo.get_task(old_id).status, TASK_FAILED)
        self.assertEqual(self.repo.get_source("c1", uid)["status"], "imported")
        r = self.client.post(f"/api/v1/tasks/{new_task['id']}/retry",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 409, r.text)

    def test_retry_failed_import_missing_staging_conflict(self):
        """IMPORT 重试时 uploads 暂存原件已丢失 → 409，且不产生新任务。"""
        payload = json.dumps({"data": {"records": [
            {"号码": "张卫国", "对端号码": "李志强", "次数": "12"},
        ]}}, ensure_ascii=False)
        up = self._upload("通话记录.json", payload.encode("utf-8"))
        uid = up["upload_id"]
        r = self._import(uid, target="通话记录",
                         column_map={"号码": "主体", "对端号码": "对端",
                                     "次数": "次数"})
        self.assertEqual(r.status_code, 200, r.text)
        old_id = r.json()["data"]["id"]
        self.repo.fail_task(old_id, error_code="X", error_message="boom")
        # 暂存件被清理
        staged = self.factory.case_dir("c1") / "uploads" / f"{uid}.json"
        staged.unlink()

        r = self.client.post(f"/api/v1/tasks/{old_id}/retry",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("重新上传", r.json()["error"]["message"])
        # 没有新任务入队，源状态未被改动
        tasks = self.repo.list_tasks(case_id="c1")
        self.assertEqual(len([t for t in tasks if t.task_type == "IMPORT"]), 1)

    # ------------------------------------------------------------------
    # 方案 C：SQLite 多表枚举与按表读取；方案 A：org 可选列降级
    # ------------------------------------------------------------------
    def _sqlite_bytes(self, tables: dict) -> bytes:
        """造含多张表的 sqlite 文件字节（{表名: DataFrame}）。"""
        tmpdb = Path(tempfile.mkdtemp()) / "s.db"
        con = sqlite3.connect(str(tmpdb))
        for name, df in tables.items():
            df.to_sql(name, con, index=False)
        con.commit()
        con.close()
        return tmpdb.read_bytes()

    def test_upload_sqlite_lists_tables_and_analyze_named(self):
        """upload 枚举库内全部表；analyze 按 sqlite_table 取表；
        非法表名 / 非 sqlite 带 sqlite_table → 400。"""
        companies = pd.DataFrame({
            "name": ["宏业建设有限公司", "宏图贸易有限公司"],
            "rep": ["李志强", "王秀兰"]})
        contacts = pd.DataFrame({"phone": ["13800000000"],
                                 "owner": ["李志强"]})
        up = self._upload("工商注册.sqlite",
                          self._sqlite_bytes({"companies": companies,
                                              "contacts": contacts}))
        self.assertEqual(up["format"], "sqlite")
        tables = {t["name"]: t for t in up["sqlite_tables"]}
        self.assertEqual(set(tables), {"companies", "contacts"})
        self.assertEqual(tables["companies"]["rows"], 2)
        self.assertEqual(tables["companies"]["columns"], ["name", "rep"])
        self.assertEqual(tables["contacts"]["columns"], ["phone", "owner"])

        def _analyze(body):
            return self.client.post(
                f"/api/v1/cases/c1/sources/{up['upload_id']}/analyze",
                headers=self.auth_h, json=body)

        # 默认（首表 companies）
        r = _analyze({})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual({c["name"] for c in r.json()["data"]["columns"]},
                         {"name", "rep"})
        # 指定 contacts
        r = _analyze({"sqlite_table": "contacts"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual({c["name"] for c in r.json()["data"]["columns"]},
                         {"phone", "owner"})
        # 不存在的表 → 400
        r = _analyze({"sqlite_table": "nope"})
        self.assertEqual(r.status_code, 400)
        # 非 sqlite 带 sqlite_table → 400
        up_csv = self._upload("流水.csv",
                              flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up_csv['upload_id']}/analyze",
            headers=self.auth_h, json={"sqlite_table": "x"})
        self.assertEqual(r.status_code, 400)

    def test_import_sqlite_org_optional_columns_e2e(self):
        """精简工商 sqlite（仅 name/rep）→ 工商信息表：org 绑定
        法人/状态/关联 optional_columns，缺列降级类型化 NULL，BUILD 不硬失败；
        sqlite_table 透传 worker 按表读取。"""
        companies = pd.DataFrame({
            "name": ["宏业建设有限公司", "宏图贸易有限公司"],
            "rep": ["李志强", "王秀兰"]})
        up = self._upload("工商注册.sqlite",
                          self._sqlite_bytes({"companies": companies}))
        self.assertEqual([t["name"] for t in up["sqlite_tables"]],
                         ["companies"])
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up['upload_id']}/import",
            headers=self.auth_h,
            json={"target_table": "工商信息",
                  "column_map": {"name": "主体", "rep": "法人"},
                  "sqlite_table": "companies"})
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        by_type = {t.task_type: t for t in self.repo.list_tasks(case_id="c1")}
        self.assertEqual(by_type["IMPORT"].status, TASK_SUCCEEDED,
                         f"{by_type['IMPORT'].error_code} "
                         f"{by_type['IMPORT'].error_message}")
        self.assertEqual(by_type["BUILD"].status, TASK_SUCCEEDED,
                         f"{by_type['BUILD'].error_code} "
                         f"{by_type['BUILD'].error_message}")
        # 冷层按中文目标表名落盘
        cold = self.factory.case_dir("c1") / "cold" / "工商信息.parquet"
        self.assertTrue(cold.exists())
        df = pd.read_parquet(cold)
        self.assertEqual(set(df["主体"]),
                         {"宏业建设有限公司", "宏图贸易有限公司"})
        self.assertIn("李志强", set(df["法人"]))
        # 语义层 obj_org：法人有值，状态/关联类型化 NULL
        store = self.factory.for_case("c1", mode="read")
        try:
            rows = store.read_conn.execute(
                "SELECT legal_rep, status, relation FROM obj_org "
                "ORDER BY raw_name").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "李志强")
            self.assertIsNone(rows[0][1])
            self.assertIsNone(rows[0][2])
        finally:
            store.close()

    # ------------------------------------------------------------------
    # W-012 幂等
    # ------------------------------------------------------------------
    def test_duplicate_fingerprint_conflict_no_task(self):
        content = flow_df(2).to_csv(index=False).encode("utf-8")
        up1 = self._upload("流水.csv", content)
        up2 = self._upload("流水.csv", content)  # 同名同内容
        self.assertEqual(up1["fingerprint"], up2["fingerprint"])
        r1 = self._import(up1["upload_id"])
        self.assertEqual(r1.status_code, 200)
        # AC-1：第二个导入 409
        r2 = self._import(up2["upload_id"])
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["error"]["code"], "CONFLICT")
        # AC-4：判定阶段无任务行——仅 1 个 IMPORT
        imports = [t for t in self.repo.list_tasks(case_id="c1")
                   if t.task_type == "IMPORT"]
        self.assertEqual(len(imports), 1)

    def test_same_content_different_name_allowed(self):
        df = flow_df(2)
        up1 = self._upload("一月流水.csv",
                           df.to_csv(index=False).encode("utf-8"))
        up2 = self._upload("二月流水.csv",
                           df.to_csv(index=False).encode("utf-8"))
        self.assertNotEqual(up1["fingerprint"], up2["fingerprint"])  # AC-2
        r1 = self._import(up1["upload_id"])
        r2 = self._import(up2["upload_id"])
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_same_name_different_content_allowed(self):
        up1 = self._upload("流水.csv",
                           flow_df(2).to_csv(index=False).encode("utf-8"))
        up2 = self._upload("流水.csv",
                           flow_df(5).to_csv(index=False).encode("utf-8"))
        self.assertNotEqual(up1["fingerprint"], up2["fingerprint"])  # AC-3
        self.assertEqual(self._import(up1["upload_id"]).status_code, 200)
        self.assertEqual(self._import(up2["upload_id"]).status_code, 200)

    # ------------------------------------------------------------------
    # 映射校验 + AC-5 失败零残留
    # ------------------------------------------------------------------
    def test_unknown_target_table_rejected_at_api(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"], target="不存在的表")
        self.assertEqual(r.status_code, 400)
        # 无任务行
        self.assertEqual([t for t in self.repo.list_tasks(case_id="c1")], [])

    def test_unknown_column_rejected_at_api(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"],
                         column_map={"付款方": "不存在列"})
        self.assertEqual(r.status_code, 400)

    def test_unparseable_file_rejected_no_residue(self):
        # AC-5 前置：不可解析文件上传即 400，暂存件清理、无注册行、无冷层
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("坏数据.json", b"not a json {{{")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        self.assertEqual(self.repo.list_sources("c1"), [])
        self.assertFalse((self.factory.case_dir("c1") / "cold").exists())
        uploads = self.factory.case_dir("c1") / "uploads"
        if uploads.exists():
            self.assertEqual(list(uploads.iterdir()), [])

    # ------------------------------------------------------------------
    # AC-6：清洗声明落 bindings
    # ------------------------------------------------------------------
    def test_clean_declaration_persists_to_bindings(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"], clean=["strip"])
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        bp = (self.svc.snapshot_dir("c1", "default") / "bindings.json")
        data = json.loads(bp.read_text(encoding="utf-8"))
        tb = next(b for b in data["object_bindings"]
                  if (b.get("source") or {}).get("table") == "银行流水")
        self.assertIn("strip", tb.get("clean") or [])

    # ------------------------------------------------------------------
    # 权限 / 注册表
    # ------------------------------------------------------------------
    def test_soldier_upload_ok_import_forbidden(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"),
                          headers=self.auth_s)
        self.assertEqual(up["rows"], 2)  # 正兵可上传
        r = self._import(up["upload_id"], headers=self.auth_s)
        self.assertEqual(r.status_code, 403)

    def test_list_sources(self):
        self._upload("流水.csv",
                     flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self.client.get("/api/v1/cases/c1/sources", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["status"], "staged")


if __name__ == "__main__":
    unittest.main()
