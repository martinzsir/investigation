"""
server/app/store/backend_cross.py
CrossCaseStore：跨案件只读 ATTACH 后端（W-026/027，M5）。

backend_api.md 跨案件约束：
  - ATTACH 一律 READ_ONLY（被 ATTACH 库不能就地写，写者构建新版本不受影响）
  - 禁 DDL/DML（SQL 首词白名单 + READ_ONLY 双保险）
  - 强制 max_rows 上限与超时
  - 查询本身进审计链（案件列表 + SQL 原文 + reason）—— 由 router 层落 ops_events
  - ATTACH 连接按 case 组合缓存复用（frozenset 为键）

grep 门禁（tests/test_store_backend.py）：server/app/ 除 store/ 外不得直连 DuckDB。
ATTACH 在此模块内完成，router 层只消费 CrossCaseStore.query()。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import duckdb

from server.app.store.backend import StoreBackend, UnsupportedOperation

# SQL 首词白名单：仅允许只读查询；ATTACH 后的库均 READ_ONLY，
# 白名单是双保险（防止对主内存连接写临时表）。
_ALLOWED_SQL_PREFIXES = ("select", "with", "pragma")

# ATTACH 连接缓存上限（LRU 淘汰），防止案件组合爆炸耗尽连接资源
_CACHE_MAX = 16

# 类级缓存：frozenset(case_ids) -> (conn, attached_paths)
_conn_cache: "OrderedDict[frozenset[str], tuple[duckdb.DuckDBPyConnection, list[Path]]]" = \
    OrderedDict()
_cache_lock = threading.Lock()


class CrossCaseStore(StoreBackend):
    """跨案件只读 ATTACH 后端。

    authorized_cases 必须已通过全有或全无鉴权（router 层保证，W-027）。
    本类不做权限校验——只负责 ATTACH + 只读查询。
    """

    case_id = "*cross-case*"

    def __init__(self, authorized_cases: list[str], *,
                 version_paths: dict[str, Path]):
        """
        Args:
            authorized_cases: 已鉴权的案件 ID 列表（顺序无关）
            version_paths: case_id -> 当前版本文件路径（由 router 经 StoreFactory 解析）
        """
        self._authorized = list(authorized_cases)
        self._version_paths = dict(version_paths)
        self._key = frozenset(self._authorized)
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._borrowed = False  # 是否从缓存借用

    @property
    def version(self) -> int:
        return 0

    @property
    def authorized_cases(self) -> list[str]:
        return list(self._authorized)

    def _attach_or_cache(self) -> duckdb.DuckDBPyConnection:
        """按 frozenset(case_ids) 复用 ATTACH 连接；未命中则新建并 ATTACH。"""
        global _conn_cache
        with _cache_lock:
            cached = _conn_cache.get(self._key)
            if cached is not None:
                conn, _ = cached
                _conn_cache.move_to_end(self._key)  # LRU 续期
                self._borrowed = True
                self._conn = conn
                return conn
        # 缓存未命中：新建主连接并 ATTACH（锁外执行 ATTACH，避免持锁阻塞）
        conn = duckdb.connect(":memory:")
        for cid in self._authorized:
            path = self._version_paths.get(cid)
            if path is None or not Path(path).exists():
                # ATTACH 失败不应部分成功——清理后抛
                conn.close()
                raise FileNotFoundError(
                    f"跨案件 ATTACH 失败：案件 {cid} 版本文件不存在")
            alias = f"case_{cid}"
            conn.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")
        with _cache_lock:
            # 二次检查：可能并发已建
            existing = _conn_cache.get(self._key)
            if existing is not None:
                conn.close()
                conn, _ = existing
                _conn_cache.move_to_end(self._key)
            else:
                if len(_conn_cache) >= _CACHE_MAX:
                    _, (old_conn, _) = _conn_cache.popitem(last=False)
                    try:
                        old_conn.close()
                    except Exception:
                        pass
                _conn_cache[self._key] = (conn, list(self._version_paths.values()))
        self._borrowed = True
        self._conn = conn
        return conn

    @property
    def read_conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._attach_or_cache()
        return self._conn

    @property
    def write_conn(self) -> duckdb.DuckDBPyConnection:
        raise UnsupportedOperation(
            "跨案件后端永远只读（READ_ONLY ATTACH，禁 DDL/DML）")

    @staticmethod
    def _assert_readonly_sql(sql: str) -> None:
        """SQL 首词白名单校验：禁 DDL/DML（W-026 AC-4）。"""
        stripped = sql.lstrip()
        if not stripped:
            raise UnsupportedOperation("空 SQL")
        first = stripped.split(None, 1)[0].lower()
        if first not in _ALLOWED_SQL_PREFIXES:
            raise UnsupportedOperation(
                f"跨案件查询仅允许只读 SQL（首词须为 SELECT/WITH/PRAGMA），"
                f"收到 {first!r}")

    def query(self, sql: str, params: tuple | list = (), *,
              max_rows: int = 1000, timeout_ms: int = 30000) -> list[dict]:
        """只读查询：首词白名单 + max_rows 强制 LIMIT + 超时。

        Args:
            max_rows: 结果行数上限（强制追加 LIMIT，W-026 AC-5）
            timeout_ms: 查询超时毫秒（PRAGMA timeout 作用于锁等待，
                        DuckDB 无全局 query timeout，此处设锁等待超时）
        """
        self._assert_readonly_sql(sql)
        conn = self.read_conn
        # 超时：DuckDB 的 timeout pragma 控制锁等待毫秒
        try:
            conn.execute(f"PRAGMA timeout={int(timeout_ms)}")
        except Exception:
            pass  # 部分版本不支持，降级忽略
        # 强制 max_rows：若 SQL 已含 LIMIT 则不重复追加（简化：直接追加，
        # DuckDB 允许多个 LIMIT 取最小者——实际取最后一个，故此处用子查询包裹更安全）
        wrapped = f"SELECT * FROM ({sql}) AS _cc LIMIT {int(max_rows)}"
        cur = conn.execute(wrapped, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        """归还连接到缓存（不关闭——缓存复用）；仅非借用连接关闭。"""
        self._conn = None
        # 借用的连接留在缓存中，不关闭（AC-7 连接复用）
        # 非借用路径当前不存在（所有连接都经缓存），保留语义以备扩展


def evict_cache() -> None:
    """测试辅助：清空 ATTACH 连接缓存。"""
    global _conn_cache
    with _cache_lock:
        for conn, _ in _conn_cache.values():
            try:
                conn.close()
            except Exception:
                pass
        _conn_cache.clear()
