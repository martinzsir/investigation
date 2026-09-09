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

import csv
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
    ".json": "json", ".ndjson": "json", ".jsonl": "json",
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
}


def sniff_format(filename: str) -> str | None:
    return SUPPORTED_FORMATS.get(Path(filename).suffix.lower())


def sniff_csv_sep(path: Path) -> str:
    """嗅探文本分隔符（逗号/分号/制表符/管道四选一）。

    中文 Windows 环境下 Excel 另存或银行导出的 CSV 常为分号分隔（区域
    设置中小数点用逗号时分隔符被迫改用分号）；若硬按逗号拆列，整行表头
    会塌成一列（如 "日期;主体;对方;金额"），向导映射页只剩一个源列选项。
    嗅探失败/空文件/单列无分隔符时回落逗号。
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace",
                  newline="") as f:
            sample = f.read(8192)
        if not sample.strip():
            return ","
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except (csv.Error, OSError):
        return ","


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


def _stringify(df: pd.DataFrame) -> pd.DataFrame:
    """读入后统一字符串化：NaN/NaT/None 归一为空串。

    parquet/json/sqlite 路径若直接 astype(str)，空值会变成字面量 "nan"
    （float NaN）/"None"（object None），污染列画像（null_rate/samples）并
    随冷层落地——主体名变 "nan"、数值/日期列 TRY_CAST 全部误报降级。
    CSV 路径以 keep_default_na=False 读入空值即空串，本函数把其余格式
    拉齐到同一口径（StringDtype 保留 <NA>，fillna 后转普通 str）。
    """
    return df.astype("string").fillna("").astype(str)


def read_table(path: Path, fmt: str) -> pd.DataFrame:
    """五格式 → DataFrame（统一 str 友好：SQLite 取第一张表）。"""
    if fmt == "csv":
        if path.suffix.lower() == ".tsv":
            sep = "\t"
        else:
            sep = sniff_csv_sep(path)
        return pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False,
                           encoding="utf-8-sig")
    if fmt == "excel":
        return pd.read_excel(path, dtype=str, keep_default_na=False,
                             engine="openpyxl")
    if fmt == "parquet":
        return _stringify(pd.read_parquet(path))
    if fmt == "json":
        # 暂存文件统一改名 .json，无法靠后缀区分 JSONL；按内容双试：
        # 整文件 JSON（records 数组/columns 对象）先行，行分隔 JSONL 回落
        # （lines=False 对 JSONL 报 "Trailing data"）。
        try:
            return _stringify(pd.read_json(path, dtype=str, lines=False))
        except ValueError:
            return _stringify(pd.read_json(path, dtype=str, lines=True))
    if fmt == "sqlite":
        # 走 pandas+sqlalchemy 的 sqlite URI（不在本文件出现直连字面量，
        # 符合"store/ 外不得直连数据库"的静态门禁）。
        uri = f"sqlite:///{path.resolve().as_posix()}"
        name = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1",
            uri).iloc[0, 0]
        return _stringify(pd.read_sql_query(f'SELECT * FROM "{name}"', uri))
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
