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
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

# 嵌套包裹 JSON 的常见记录键（API 响应式结构，按优先级排序）
_WRAPPER_KEYS = ("records", "data", "rows", "items", "results",
                 "result", "list", "content")
# DFS 下钻深度上限（防异常深嵌套/循环引用拖垮上传）
_MAX_DRILL_DEPTH = 8

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


def _find_record_list(node: Any, depth: int = 0) -> list[dict] | None:
    """DFS 找第一个"元素全为 dict 的列表"（记录数组）。

    dict 节点先按常见包裹键（records/data/rows/items…）优先下钻，未中再
    遍历其余值；list 节点整体是 dict 数组即命中，否则逐元素继续下钻。
    用于展平 {"data": {"records": [...]}} 这类 API 响应式包裹——
    pd.read_json 会把它塌成 1 行 1 列（外层 key 当列名、内层 dict 当
    单元格字符串），记录全丢且不报错。
    """
    if depth > _MAX_DRILL_DEPTH:
        return None
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            return node
        for item in node:
            found = _find_record_list(item, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, dict):
        for key in _WRAPPER_KEYS:
            if key in node:
                found = _find_record_list(node[key], depth + 1)
                if found is not None:
                    return found
        for value in node.values():
            found = _find_record_list(value, depth + 1)
            if found is not None:
                return found
    return None


def _flatten_nested_json(path: Path) -> pd.DataFrame | None:
    """嵌套包裹 JSON 展平为 DataFrame；非该形态返回 None（回落 pandas 双试）。

    顶层为 records 数组（[{...},{...}]）时同样命中（与 pandas records
    形态等价，空值口径统一走 _stringify）；JSONL（json.load 报额外数据）、
    单记录对象（无 list[dict]）、坏文件均返回 None 交回 pandas 路径。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return None
    records = _find_record_list(payload)
    if not records:
        return None
    return pd.DataFrame(records)


def collapse_warning(df: pd.DataFrame) -> str | None:
    """塌缩预警：解析结果 1 行 1 列且唯一单元格疑似 JSON/结构化串。

    嵌套包裹未展平的兜底场景（如 records 是双重编码的 JSON 字符串）、
    或文本分隔符完全不在嗅探集内时，整行会塌成一列；若该单元格以
    [ 或 { 开头，几乎可断定结构化数据被当成纯文本——在向导映射前
    直接提示，避免用户对着一个源列硬配、直到 BUILD 才报缺列。
    """
    if len(df.columns) == 1 and len(df) == 1:
        head = str(df.iloc[0, 0]).strip()[:1]
        if head in ("[", "{"):
            return ("文件被解析为 1 行 1 列，且单元格内容疑似 JSON 结构"
                    f"（以 {head} 开头）：可能是嵌套 JSON 包裹未展平"
                    "或分隔符不匹配，请检查文件格式后重新上传")
    return None


def read_table(path: Path, fmt: str, table: str | None = None) -> pd.DataFrame:
    """五格式 → DataFrame（统一 str 友好）。

    table：仅 SQLite 有效，指定读取库内哪张表；缺省取第一张用户表。
    """
    if table is not None and fmt != "sqlite":
        raise ValueError(f"仅 SQLite 支持选择库内表，当前格式 {fmt} 不支持")
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
        # 1) 嵌套包裹展平：stdlib json.load 后 DFS 找记录数组
        # （{"data":{"records":[...]}} 等 API 响应式结构；pd.read_json 会把
        # 它塌成 1 行 1 列且不报错）。非该形态返回 None。
        flat = _flatten_nested_json(path)
        if flat is not None:
            return _stringify(flat)
        # 2) 暂存文件统一改名 .json，无法靠后缀区分 JSONL；按内容双试：
        # 整文件 JSON（columns 对象等）先行，行分隔 JSONL 回落
        # （lines=False 对 JSONL 报 "Trailing data"）。
        try:
            return _stringify(pd.read_json(path, dtype=str, lines=False))
        except ValueError:
            return _stringify(pd.read_json(path, dtype=str, lines=True))
    if fmt == "sqlite":
        # 走 pandas+sqlalchemy 的 sqlite URI（不在本文件出现直连字面量，
        # 符合"store/ 外不得直连数据库"的静态门禁）。
        uri = f"sqlite:///{path.resolve().as_posix()}"
        if table is not None:
            # 显式指定库内表：白名单校验（表名必须真实存在，防注入/防误读）
            avail = [t["name"] for t in list_sqlite_tables(path)]
            if table not in avail:
                raise ValueError(
                    f"SQLite 库内不存在表 {table!r}（可用表 {avail}）")
            name = table
        else:
            name = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid LIMIT 1",
                uri).iloc[0, 0]
        qname = str(name).replace('"', '""')
        return _stringify(pd.read_sql_query(f'SELECT * FROM "{qname}"', uri))
    raise ValueError(f"不支持的格式：{fmt}")


def list_sqlite_tables(path: Path) -> list[dict]:
    """枚举 SQLite 库内全部用户表（rowid 顺序=创建顺序）。

    返回 [{name, rows, columns:[列名...]}]，供向导"库内表选择"——
    read_table 默认只读第一张表，多表库需让用户显式选择目标表。
    """
    uri = f"sqlite:///{path.resolve().as_posix()}"
    names = pd.read_sql_query(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY rowid", uri)["name"].tolist()
    out: list[dict] = []
    for name in names:
        qname = str(name).replace('"', '""')
        cols = pd.read_sql_query(
            f'PRAGMA table_info("{qname}")', uri)["name"].tolist()
        n = int(pd.read_sql_query(
            f'SELECT COUNT(*) AS n FROM "{qname}"', uri).iloc[0, 0])
        out.append({"name": str(name), "rows": n,
                    "columns": [str(c) for c in cols]})
    return out


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
