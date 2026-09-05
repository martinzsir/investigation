"""
scripts/profile_table.py
REQ-P M6（REQ-P-035①）：新表接入前画像 CLI。

    python -m scripts.profile_table --input data/samples/xxx.xlsx [--sheet 名]
                                    [--pack default] [--db investigation.duckdb]
                                    [--no-drafts]

流程（全部只读外部表 + 只读语义层，不写任何库表）：
  1. raw 模式读外部表（pandas dtype=str，**禁 data_ingest 适配器的隐式类型转换**——
     风险 7：金额/日期被 coerce 后值类型识别失效；同一 CSV 经任何路径画像结果一致）；
  2. build_table_profile：列画像（空值率/值类型分布/混装/落点）+ 候选关联
     （外部列 distinct ∩ obj_* 可连接属性，overlap ≥ draft_overlap_min_ratio）；
  3. DraftAssembler 组装 objects/links/bindings 草案 → output/drafts/<table>/；
  4. recommend_steps 给出 ETL 步骤序列；
  5. 产物 output/profiles/<table>_profile.{json,md}。

红线：治理工具读原始外部表是本职（只读画像），与"检测器/图库/MCP 不准直读 Parquet"
不冲突（该线约束取证取数路径）。草案只写 output/drafts/，绝不写 ontology/。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from core.ontology_profile import build_table_profile
from core.draft_assembler import DraftAssembler, recommend_steps

ROOT = Path(__file__).resolve().parent.parent
OUT_PROFILES = ROOT / "output" / "profiles"
OUT_DRAFTS = ROOT / "output" / "drafts"


def _raw_read(path: Path, sheet: str | None = None):
    """raw 模式读取：全字符串、不映射列名、不做类型转换（风险 7）。

    返回 (columns, rows)；rows 为 list[list[str|None]]，空单元格为 None。
    """
    import pandas as pd
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        sep = "\t" if ext == ".tsv" else None
        df = pd.read_csv(path, dtype=str, keep_default_na=False,
                         sep=sep, engine="python") if sep else \
             pd.read_csv(path, dtype=str, keep_default_na=False, engine="python")
    elif ext in (".xlsx", ".xls"):
        import openpyxl  # noqa: F401  存在性检查
        xls = pd.ExcelFile(path)
        sheets = [sheet] if sheet else xls.sheet_names
        parts = [pd.read_excel(path, sheet_name=s, dtype=str) for s in sheets]
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    elif ext in (".sqlite", ".db"):
        import sqlite3
        con = sqlite3.connect(str(path))
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            t = sheet or (tables[0] if tables else None)
            if not t:
                raise RuntimeError(f"{path.name} 中无表")
            df = pd.read_sql_query(f'SELECT * FROM "{t}"', con)
        finally:
            con.close()
    elif ext == ".parquet":
        df = pd.read_parquet(path)
    elif ext == ".json":
        df = pd.read_json(path, dtype=str)
    else:
        raise RuntimeError(f"不支持的文件类型：{ext}（支持 csv/tsv/xlsx/sqlite/parquet/json）")

    columns = [str(c) for c in df.columns]
    rows = []
    for rec in df.itertuples(index=False, name=None):
        out = []
        for v in rec:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                out.append(None)
            else:
                s = str(v)
                out.append(s if s.strip() != "" else None)
        rows.append(out)
    return columns, rows


def render_markdown(profile, steps, draft_files) -> str:
    L = ["# 新表接入画像：%s" % profile.table_name, "",
         "> 红线：只观察不写回。全部结论均为【待核实】候选；草案为自动组装，",
         "> 人工审核 + loader 校验两道闸后才生效。", "",
         f"- 行数：**{profile.row_count}**；列数：{len(profile.columns)}", ""]

    L += ["## 列画像", "",
          "| 列 | 空值率 | 基数 | 值类型分布 | 混装 | 落点建议 | 样例 |",
          "|---|---:|---:|---|---|---|---|"]
    for c in profile.columns:
        L.append(f"| {c.name} | {c.null_rate:.0%} | {c.distinct} "
                 f"| {c.type_dist} | {'⚠ 是' if c.mixed else '—'} "
                 f"| {c.landing_suggestions or '—'} | "
                 f"{', '.join(str(s) for s in c.samples[:3])} |")
    L.append("")

    L += ["## 候选关联（外部列 → 语义对象，overlap ≥ 阈值）", ""]
    if profile.candidates:
        L += ["| 外部列 | 目标对象.属性 | overlap | 方向 |",
              "|---|---|---:|---|"]
        for c in profile.candidates:
            L.append(f"| {c.col} | {c.target_obj}.{c.target_prop} "
                     f"| {c.overlap_ratio:.0%} | {c.direction} |")
    else:
        L.append("无（无已物化语义层，或无列达到 overlap 阈值）。")
    L.append("")

    L += ["## 建议 ETL 步骤（recommend_steps，只输出清单不自动执行）", ""]
    for i, s in enumerate(steps, 1):
        L.append(f"{i}. **{s['step']}**")
        L.append(f"   - why：{s['why']}")
        L.append(f"   - how：{s['how']}")
        L.append(f"   - done_when：{s['done_when']}")
    L.append("")

    L += ["## 草案产物（待人工审核）", ""]
    if draft_files:
        for f in draft_files:
            L.append(f"- `{f.relative_to(ROOT) if ROOT in f.parents or f.is_relative_to(ROOT) else f}`")
    else:
        L.append("- 未生成（--no-drafts）")
    L += ["", "---", "_全部为【待核实】候选，不构成办案指导。_", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="新表接入前画像（raw 只读 + 草案组装）")
    ap.add_argument("--input", required=True, help="外部表文件（csv/tsv/xlsx/sqlite/parquet/json）")
    ap.add_argument("--sheet", default=None, help="Excel sheet 名 / SQLite 表名（默认首个/全部）")
    ap.add_argument("--pack", default="default", help="ontology 案件包名")
    ap.add_argument("--db", default=None, help="语义层 DuckDB（默认 investigation.duckdb）")
    ap.add_argument("--no-drafts", action="store_true", help="只画像不生成草案")
    args = ap.parse_args(argv)

    path = Path(args.input)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        print(f"输入文件不存在：{path}")
        return 2
    columns, rows = _raw_read(path, args.sheet)
    print(f"raw 读取：{path.name} {len(rows)} 行 × {len(columns)} 列（未做任何类型转换）")

    # 候选关联需要语义层（可选；未构建则 candidates=[]）
    gateway = None
    try:
        from core import Store
        from core.gateway import OntologyReadGateway
        store = Store(root=str(ROOT), db_path=args.db) if args.db else Store(root=str(ROOT))
        gw = OntologyReadGateway(store.conn)
        if gw.materialization_state() != "UNBUILT":
            gateway = gw
        else:
            print("语义层未构建：候选关联跳过（先跑 run_all / build_ontology）")
    except Exception as e:
        print(f"语义层不可用，候选关联跳过：{e}")

    profile = build_table_profile(path.stem, columns, rows,
                                  gateway=gateway, pack=args.pack)
    steps = recommend_steps(profile)

    draft_files = []
    if not args.no_drafts:
        draft_files = DraftAssembler(profile, pack=args.pack).write_drafts(OUT_DRAFTS)

    OUT_PROFILES.mkdir(parents=True, exist_ok=True)
    (OUT_PROFILES / f"{path.stem}_profile.json").write_text(
        json.dumps({"profile": profile.to_dict(),
                    "steps": steps,
                    "drafts": [str(f.relative_to(ROOT)) for f in draft_files]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_PROFILES / f"{path.stem}_profile.md").write_text(
        render_markdown(profile, steps, draft_files), encoding="utf-8")

    mixed = [c.name for c in profile.columns if c.mixed]
    print(f"画像完成：混装列 {mixed or '无'}；候选关联 {len(profile.candidates)} 条；"
          f"建议步骤 {len(steps)} 步")
    print(f"产物：{OUT_PROFILES.relative_to(ROOT)}/{path.stem}_profile.{{json,md}}")
    if draft_files:
        print(f"草案：output/drafts/{path.stem}/（{len(draft_files)} 件，待人工审核）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
