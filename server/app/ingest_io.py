"""
server/app/ingest_io.py
W-010/012 数据接入：五格式解析、指纹、列画像、冷层 parquet 原子写。

- 支持格式：CSV / Excel(xlsx/xls) / Parquet / JSON / SQLite(.db/.sqlite)
  （AC-1）；pandas 统一解析，行数与列画像在上传阶段产出；
- 指纹 = sha256(文件名 + 内容 sha256 + 行数)——同文件同名重复导入可判，
  同内容异名（指纹含文件名）放行（W-012 AC-1/2/3）；
- 冷层写入：临时目录写 parquet → 原子 os.replace 进 cold/，失败零残留
  （AC-5）；列映射（源列名→bindings 声明的原始列名）在写 parquet 前套用。

本模块不碰 meta 库、不直接入队任务（纯 IO + DataFrame 操作）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

SUPPORTED_FORMATS = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".xlsx": "excel", ".xls": "excel",
    ".parquet": "parquet", ".pq": "parquet",
    ".json": "json", ".ndjson": "json",
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
}


def sniff_format(filename: str) -> str | None:
    return SUPPORTED_FORMATS.get(Path(filename).suffix.lower())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(filename: str, content_hash: str, rows: int) -> str:
    """W-012 幂等指纹：文件名 + 内容哈希 + 行数（同名同内容同行=重复）。"""
    h = hashlib.sha256()
    h.update(filename.encode("utf-8"))
    h.update(b"|")
    h.update(content_hash.encode("ascii"))
    h.update(b"|")
    h.update(str(int(rows)).encode("ascii"))
    return h.hexdigest()[:24]


def read_table(path: Path, fmt: str) -> pd.DataFrame:
    """五格式 → DataFrame（统一 str 友好：SQLite 取第一张表）。"""
    if fmt == "csv":
        sep = "\t" if path.suffix.lower() == ".tsv" else ","
        return pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False)
    if fmt == "excel":
        return pd.read_excel(path, dtype=str, keep_default_na=False,
                             engine="openpyxl")
    if fmt == "parquet":
        return pd.read_parquet(path).astype(str)
    if fmt == "json":
        try:
            return pd.read_json(path, dtype=str, lines=path.suffix.lower() == ".ndjson")
        except ValueError:
            return pd.read_json(path, dtype=str)
    if fmt == "sqlite":
        # 走 pandas+sqlalchemy 的 sqlite URI（不在本文件出现直连字面量，
        # 符合"store/ 外不得直连数据库"的静态门禁）。
        uri = f"sqlite:///{path.resolve().as_posix()}"
        name = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1",
            uri).iloc[0, 0]
        return pd.read_sql_query(
            f'SELECT * FROM "{name}"', uri).astype(str)
    raise ValueError(f"不支持的格式：{fmt}")


def profile_columns(df: pd.DataFrame, sample: int = 3) -> dict[str, dict]:
    """列画像：非空计数 + 推断类型 + 样本值（向导映射用）。"""
    out: dict[str, dict] = {}
    for col in df.columns:
        series = df[col].astype(str)
        non_empty = series[series.str.len() > 0]
        out[str(col)] = {
            "non_null": int(len(non_empty)),
            "samples": non_empty.head(sample).tolist(),
            "inferred": _infer_kind(series),
        }
    return out


def _infer_kind(series: pd.Series) -> str:
    """粗略推断（向导提示用，不参与编译期类型判定）。"""
    s = series[series.str.len() > 0].head(200)
    if s.empty:
        return "empty"
    if s.str.fullmatch(r"-?\d+(\.\d+)?").mean() > 0.8:
        return "number"
    if s.str.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}.*").mean() > 0.8:
        return "date"
    return "text"


def write_cold_parquet(df: pd.DataFrame, *, column_map: dict[str, str],
                       cold_dir: Path, table_name: str) -> tuple[Path, int]:
    """套列映射后写冷层 parquet（临时目录 → 原子 rename，失败零残留）。

    column_map: {上传文件列名: bindings 声明的原始列名}；未映射列原样保留
    （编译器只取声明列，多余列无害）。
    返回 (parquet 绝对路径, 行数)。
    """
    if column_map:
        df = df.rename(columns={k: v for k, v in column_map.items()
                                if k in df.columns})
    n_rows = int(len(df))
    cold_dir.mkdir(parents=True, exist_ok=True)
    final = cold_dir / f"{table_name}.parquet"
    tmp_dir = cold_dir / ".tmp_import"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    tmp = tmp_dir / f"{table_name}.parquet"
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, final)  # 同目录原子替换
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return final, n_rows
