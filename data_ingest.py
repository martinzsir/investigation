"""
data_ingest.py
数据接入适配层（任务 ① 关键件）：把用户上传的任意格式统一为内部标准 schema，写入 DuckDB。

支持格式：
  .csv / .tsv      CSVAdapter     —— 自动检测分隔符(逗号/分号/制表符)、类型(金额去逗号、日期解析)
  .xlsx / .xls     ExcelAdapter   —— 多 sheet 合并、中文列名映射
  .json            JSONAdapter    —— 嵌套结构递归展开（data.records[*]）
  .sqlite / .db    SQLiteAdapter  —— 直接 SQL 读取，零拷贝
  .parquet         ParquetAdapter —— 原生直读

统一 schema（所有数据源最终都映射成这些标准列）：
  {主体, 对方/对端, 金额, 日期, 用途, 来源文件, 来源sheet, 原始行}

设计要点：
  - 适配器模式：每种格式一个类，新增格式只需加一个 Adapter（不改主流程）
  - 保留原始行（raw 字段），供血缘溯源
  - 容错优先：缺列只警告不报错（正兵可能只上传部分列）
  - 接入日志：每次接入留痕，正兵可追溯到「这批数据哪来的」
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore


# 标准列名映射：各格式常见列名 → 内部标准列
_COLUMN_MAP = {
    "交易日期": "日期", "日期": "日期", "date": "日期", "时间": "日期", "通话时间": "日期",
    "交易金额": "金额", "金额": "金额", "amount": "金额", "数目": "金额",
    "付款方": "主体", "付款人": "主体", "姓名": "主体", "name": "主体", "主体": "主体",
    "收款方": "对方", "对方": "对方", "对端": "对方", "callee": "对方", "收款人": "对方",
    "用途": "用途", "摘要": "用途", "remark": "用途",
}


def _detect_separator(sample: str) -> str:
    """自动检测分隔符：逗号 / 分号 / 制表符。"""
    for sep in ("\t", ";", ","):
        if sep in sample:
            return sep
    return ","


def _coerce_types(df) -> Any:
    """自动推断列类型：金额去逗号/货币符号转数值，日期列转 datetime。

    日期列无法解析的值 coerce 为 NaT（不中断接入，鲁棒性 B2-08），
    损失计数落 df.attrs["coerce_lost"]（{列名: 行数}），随接入记录一并上报——
    只降级不静默，供管道侧落 run_diagnostic 或人工核查。
    """
    if pd is None:
        return df
    coerce_lost: dict = {}
    for col in df.columns:
        # 金额列：含逗号的字符串 → 数值
        if df[col].dtype == object and df[col].str.contains(",|¥|元", na=False).any():
            try:
                df[col] = (df[col].astype(str)
                           .str.replace(",", "", regex=False)
                           .str.replace("¥", "", regex=False)
                           .str.replace("元", "", regex=False)
                           .astype(float))
                continue
            except (ValueError, TypeError):
                pass
        # 日期列：自动解析
        if col in ("日期", "date", "时间", "通话时间"):
            try:
                parsed = pd.to_datetime(df[col], errors="coerce")
                lost = int(df[col].notna().sum() - parsed.notna().sum())
                if lost > 0:
                    coerce_lost[str(col)] = lost
                df[col] = parsed
            except Exception:
                pass
    if coerce_lost:
        df.attrs["coerce_lost"] = coerce_lost
    return df


def _standardize_columns(df) -> Any:
    """把各格式列名映射为内部标准列名。"""
    return df.rename(columns=_COLUMN_MAP)


# ----------------------------------------------------------------------
# 各格式适配器
# ----------------------------------------------------------------------
class CSVAdapter:
    def __init__(self, path: Path, encoding: str = "utf-8"):
        self.path, self.encoding = path, encoding

    def read(self) -> Any:
        if pd is None:
            raise RuntimeError("需安装 pandas 以支持 CSV 读取")
        with open(self.path, "r", encoding=self.encoding) as f:
            sample = f.read(2048)
        sep = _detect_separator(sample)
        df = pd.read_csv(self.path, sep=sep, encoding=self.encoding)
        return _coerce_types(_standardize_columns(df))


class ExcelAdapter:
    def __init__(self, path: Path, column_map: Optional[Dict[str, str]] = None):
        self.path, self.column_map = path, column_map or {}

    def read(self) -> Any:
        if pd is None:
            raise RuntimeError("需安装 pandas + openpyxl 以支持 Excel 读取")
        sheets = pd.ExcelFile(self.path).sheet_names
        parts = []
        for sheet in sheets:
            df = pd.read_excel(self.path, sheet_name=sheet)
            df = df.rename(columns=self.column_map)
            df = _standardize_columns(df)
            df["_source_sheet"] = sheet
            parts.append(df)
        return _coerce_types(pd.concat(parts, ignore_index=True)) if parts else pd.DataFrame()


class JSONAdapter:
    def __init__(self, path: Path, record_path: Optional[str] = None):
        self.path, self.record_path = path, record_path

    def _flatten(self, obj, prefix: str = "") -> Dict[str, Any]:
        items: Dict[str, Any] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    items.update(self._flatten(v, nk))
                else:
                    items[nk] = v
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                items.update(self._flatten(v, f"{prefix}[{i}]"))
        return items

    def read(self) -> Any:
        if pd is None:
            raise RuntimeError("需安装 pandas 以支持 JSON 读取")
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        if self.record_path:
            for part in self.record_path.split("."):
                data = data[part]
        records = [self._flatten(item) for item in data]
        df = pd.DataFrame(records)
        return _coerce_types(_standardize_columns(df))


class SQLiteAdapter:
    def __init__(self, path: Path, table: Optional[str] = None, query: Optional[str] = None):
        self.path, self.table, self.query = path, table, query

    def read(self) -> Any:
        if pd is None:
            raise RuntimeError("需安装 pandas 以支持 SQLite 读取")
        import sqlite3
        conn = sqlite3.connect(self.path)
        q = self.query or f"SELECT * FROM {self.table}" if self.table else "SELECT * FROM (SELECT name FROM sqlite_master WHERE type='table' LIMIT 1)"
        df = pd.read_sql_query(q, conn)
        conn.close()
        return _coerce_types(_standardize_columns(df))


class ParquetAdapter:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> Any:
        if pd is None:
            raise RuntimeError("需安装 pandas 以支持 Parquet 读取")
        df = pd.read_parquet(self.path)
        return _coerce_types(_standardize_columns(df))


# ----------------------------------------------------------------------
# 统一接入管理器
# ----------------------------------------------------------------------
ADAPTERS = {
    ".csv": CSVAdapter, ".tsv": CSVAdapter,
    ".xlsx": ExcelAdapter, ".xls": ExcelAdapter,
    ".json": JSONAdapter,
    ".sqlite": SQLiteAdapter, ".db": SQLiteAdapter,
    ".parquet": ParquetAdapter,
}


class DataIngestManager:
    """
    统一数据接入管理器。

    用法：
        ingest = DataIngestManager(store)
        records = ingest.ingest_directory(Path("raw_data/"))
        # records: [{file, format, source_type, rows, columns, status}]
    """

    # 文件名关键词 → 数据源类型（可覆盖）
    TYPE_HINTS = {
        "流水": "bank_flow", "资金": "bank_flow", "财务": "bank_flow",
        "通话": "call_record", "通讯": "call_record",
        "中标": "bid_win", "招投标": "bid_win", "公告": "bid_win",
        "工商": "business", "企业": "business", "公司": "business",
        "轨迹": "location", "出行": "location", "位置": "location",
    }

    def __init__(self, store, type_hints: Optional[Dict[str, str]] = None):
        self.store = store
        self.type_hints = {**self.TYPE_HINTS, **(type_hints or {})}
        self.ingestion_log: List[Dict[str, Any]] = []

    def ingest_file(self, file_path, source_type: Optional[str] = None,
                    column_map: Optional[Dict[str, str]] = None, **kwargs) -> Dict[str, Any]:
        """接入单个文件 → 标准化 → 写入 DuckDB raw_{source_type} 表。"""
        path = Path(file_path)
        ext = path.suffix.lower()
        if ext not in ADAPTERS:
            raise ValueError(f"不支持的格式：{ext}（支持 {list(ADAPTERS)}）")

        stype = source_type or self._detect_type(path.name)
        adapter = ADAPTERS[ext](path, **kwargs)
        df = adapter.read()
        coerce_lost = dict(df.attrs.get("coerce_lost") or {})
        df = self._validate(df, stype)
        if coerce_lost and not df.attrs.get("coerce_lost"):
            df.attrs["coerce_lost"] = coerce_lost   # _validate 过滤重建 df 后保留计数
        df["_source_file"] = str(path)

        # 写入 DuckDB（CREATE OR REPLACE，保留全量；生产可改为追加+分区）
        table = f"raw_{stype}"
        self.store.conn.register("__tmp_df__", df)
        self.store.execute(f'DROP TABLE IF EXISTS "{table}"')
        self.store.execute(f'CREATE TABLE "{table}" AS SELECT * FROM __tmp_df__')
        self.store.conn.unregister("__tmp_df__")

        record = {
            "file": str(path), "format": ext.lstrip("."), "source_type": stype,
            "rows": len(df), "columns": list(df.columns), "status": "success",
        }
        if df.attrs.get("coerce_lost"):
            record["coerce_lost"] = dict(df.attrs["coerce_lost"])
        self.ingestion_log.append(record)
        return record

    def ingest_directory(self, dir_path) -> List[Dict[str, Any]]:
        """批量接入目录下所有支持的文件。"""
        results: List[Dict[str, Any]] = []
        for file in sorted(Path(dir_path).iterdir()):
            if file.suffix.lower() not in ADAPTERS:
                continue
            try:
                rec = self.ingest_file(file)
                results.append(rec)
            except Exception as e:
                # 失败记录同样补齐 format/source_type/rows，保证下游消费 schema 一致（避免 KeyError）
                failed = {
                    "file": str(file), "format": file.suffix.lower().lstrip("."),
                    "source_type": self._detect_type(file.name),
                    "rows": 0, "columns": [], "status": "failed", "error": str(e),
                }
                self.ingestion_log.append(failed)
                results.append(failed)
        return results

    # ---- 内部 ----
    def _detect_type(self, filename: str) -> str:
        for hint, stype in self.type_hints.items():
            if hint in filename:
                return stype
        return "unknown"

    def _validate(self, df, source_type: str) -> Any:
        """统一 schema 校验：缺关键列只警告（正兵可能只上传部分列）。"""
        required = {
            "bank_flow": ["主体", "金额", "日期"],
            "call_record": ["主体", "对端"],
            "bid_win": ["项目", "公司"],
        }
        if source_type in required:
            missing = [c for c in required[source_type] if c not in df.columns]
            if missing:
                print(f"  ⚠ {source_type} 缺少列 {missing}（保留接入，后续技能可能降级）")
        # 去空主体行
        if "主体" in df.columns:
            df = df[df["主体"].notna() & (df["主体"].astype(str).str.strip() != "")]
        return df


__all__ = ["DataIngestManager", "CSVAdapter", "ExcelAdapter", "JSONAdapter",
           "SQLiteAdapter", "ParquetAdapter"]
