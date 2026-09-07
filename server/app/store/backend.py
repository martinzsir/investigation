"""
server/app/store/backend.py
StoreBackend 抽象 + CaseStore + CrossCaseStore 骨架 + StoreFactory（W-003）。

设计（M1_plan 阶段 A，最小改动原则）：
  - 纯新建文件，不改 core/store.py；服务端全程消费裸 duckdb 连接
    （build_ontology(conn) / OntologyReadGateway(conn, ...)），不构造 core.store.Store；
  - 版本指针不在本模块落库：case_version 表属 meta 元数据层（阶段 B/D），
    工厂经注入的 meta 句柄（鸭子类型 current_version(case_id)->int）解析指针，
    保持 store 层不依赖 meta 实现；
  - 读连接以 duckdb read_only=True 打开：写尝试由 DuckDB 自身拒绝；
    写连接指向新版本文件（cases/{cid}/v{N}.duckdb），与旧版读者零冲突
    （不同文件 = 不同锁域，原子切换靠 meta 指针 UPDATE，见阶段 D）；
  - CrossCaseStore 仅骨架：跨案件 ATTACH 留 M5，read/write 均抛 UnsupportedOperation。

grep 门禁（tests/test_store_backend.py 硬断言）：
  - server/ 树内不得出现 core Store 的无参实例化；
  - server/app/ 除 store/ 模块外不得出现直接 duckdb 连接调用。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Protocol

import duckdb


class UnsupportedOperation(RuntimeError):
    """该 Store 后端不支持请求的操作（如 read 模式写、跨案件 ATTACH 未实现）。"""


class _MetaPointer(Protocol):
    """版本指针提供者（meta 元数据层鸭子类型，阶段 D 接入）。"""

    def current_version(self, case_id: str) -> int: ...


class StoreBackend(ABC):
    """存储后端抽象（W-003）。

    五要素：case_id 标识、version 版本号、read_conn 只读连接、
    write_conn 写连接、query 只读取数。所有连接由工厂统一登记，
    close() 释放并递减读者引用计数。
    """

    case_id: str

    @property
    @abstractmethod
    def version(self) -> int:
        """当前打开的版本号（local 后端为 0）。"""

    @property
    @abstractmethod
    def read_conn(self) -> duckdb.DuckDBPyConnection:
        """只读连接（DuckDB read_only=True；写尝试由引擎拒绝）。"""

    @property
    @abstractmethod
    def write_conn(self) -> duckdb.DuckDBPyConnection:
        """写连接（仅 write 模式/支持写的后端可得，否则 UnsupportedOperation）。"""

    @abstractmethod
    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        """只读查询，返回字典列表。"""

    @abstractmethod
    def close(self) -> None:
        """释放连接并通知工厂递减引用计数（幂等）。"""

    def __enter__(self) -> "StoreBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class CaseStore(StoreBackend):
    """单案件版本化 DuckDB 后端。

    mode="read"：以 read_only=True 打开 cases/{cid}/v{N}.duckdb，
                 write_conn 抛 UnsupportedOperation；
    mode="write"：读写打开目标版本文件（不存在则创建，Worker 构建新版本用），
                 read_conn/write_conn 同一连接（构建期需要边读边写）。
    """

    def __init__(self, path: Path, *, case_id: str, version: int,
                 mode: str, on_close: Callable[["CaseStore"], None] | None = None):
        if mode not in ("read", "write"):
            raise ValueError(f"mode 必须是 read/write，收到 {mode!r}")
        self._path = Path(path)
        self.case_id = case_id
        self._version = version
        self._mode = mode
        self._on_close = on_close
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._closed = False

    @property
    def version(self) -> int:
        return self._version

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            if self._mode == "read":
                if not self._path.exists():
                    raise FileNotFoundError(
                        f"案件版本文件不存在：{self._path}（case={self.case_id} "
                        f"v{self._version}）")
                self._conn = duckdb.connect(str(self._path), read_only=True)
            else:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = duckdb.connect(str(self._path), read_only=False)
        return self._conn

    @property
    def read_conn(self) -> duckdb.DuckDBPyConnection:
        return self._open()

    @property
    def write_conn(self) -> duckdb.DuckDBPyConnection:
        if self._mode != "write":
            raise UnsupportedOperation(
                f"CaseStore({self.case_id}, v{self._version}, mode=read) 不可写："
                f"写操作必须构建新版本文件（W-007 版本化文件）")
        return self._open()

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        cur = self.read_conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        if self._on_close is not None:
            self._on_close(self)


class CrossCaseStore(StoreBackend):
    """跨案件只读 ATTACH 后端（M1 骨架，M5 实现）。

    backend_api.md 跨案件约束：READ_ONLY ATTACH + max_rows + 超时 +
    禁 DDL/DML + 全有或全无授权。M1 不开放，任何连接获取均抛
    UnsupportedOperation，防止误用。
    """

    case_id = "*cross-case*"

    def __init__(self, authorized_cases: list[str]):
        self._authorized = list(authorized_cases)

    @property
    def version(self) -> int:
        return 0

    @property
    def authorized_cases(self) -> list[str]:
        return list(self._authorized)

    @property
    def read_conn(self) -> duckdb.DuckDBPyConnection:
        raise UnsupportedOperation(
            "跨案件只读 ATTACH 未实现（M5）：M1 仅登记骨架，"
            f"已授权案件 {self._authorized}")

    @property
    def write_conn(self) -> duckdb.DuckDBPyConnection:
        raise UnsupportedOperation(
            "跨案件后端永远只读（READ_ONLY ATTACH，禁 DDL/DML）")

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        raise UnsupportedOperation("跨案件 ATTACH 未实现（M5）")

    def close(self) -> None:
        return None


class StoreFactory:
    """存储后端工厂（服务端开库唯一入口）。

    - for_case：案件版本化文件；mode="read" 且未显式给 version 时，
      经 meta 指针（current_version）解析当前生效版本；
    - for_local：兼容既有单案件布局 data/investigation.duckdb（本地/自测用）；
    - for_cross_case：M1 骨架（M5）。
    读者引用计数按 (case_id, version) 登记，为 M2 旧版本物理清理提供依据。
    """

    def __init__(self, cases_root: str | Path | None = None,
                 meta: _MetaPointer | None = None):
        self.cases_root = Path(cases_root) if cases_root else Path("cases")
        self._meta = meta
        self._lock = threading.Lock()
        self._readers: dict[tuple[str, int], int] = {}

    # ---- 版本指针 ----
    def current_version(self, case_id: str) -> int:
        if self._meta is None:
            raise UnsupportedOperation(
                "StoreFactory 未注入 meta 句柄，无法解析版本指针；"
                "请显式传 version 或在阶段 D 接入 MetaRepo")
        return int(self._meta.current_version(case_id))

    def case_dir(self, case_id: str) -> Path:
        return self.cases_root / case_id

    def version_path(self, case_id: str, version: int) -> Path:
        return self.case_dir(case_id) / f"v{version}.duckdb"

    # ---- 开库 ----
    def for_case(self, case_id: str, *, mode: str = "read",
                 version: int | None = None) -> CaseStore:
        if mode not in ("read", "write"):
            raise ValueError(f"mode 必须是 read/write，收到 {mode!r}")
        if version is None:
            if mode != "read":
                raise ValueError("write 模式必须显式指定目标版本号")
            version = self.current_version(case_id)
        path = self.version_path(case_id, version)
        store = CaseStore(path, case_id=case_id, version=version, mode=mode,
                          on_close=self._on_close)
        if mode == "read":
            with self._lock:
                key = (case_id, version)
                self._readers[key] = self._readers.get(key, 0) + 1
        return store

    def for_local(self, *, mode: str = "read", root: str | Path = "data",
                  db_path: str = "investigation.duckdb") -> CaseStore:
        """兼容既有单案件布局（data/investigation.duckdb）。"""
        path = Path(root) / db_path
        return CaseStore(path, case_id="local", version=0, mode=mode,
                         on_close=None)

    def for_cross_case(self, authorized_cases: list[str]) -> CrossCaseStore:
        return CrossCaseStore(authorized_cases)

    # ---- 引用计数 ----
    def reader_count(self, case_id: str, version: int) -> int:
        with self._lock:
            return self._readers.get((case_id, version), 0)

    def _on_close(self, store: CaseStore) -> None:
        if store.mode != "read":
            return
        with self._lock:
            key = (store.case_id, store.version)
            n = self._readers.get(key, 0)
            if n > 1:
                self._readers[key] = n - 1
            else:
                self._readers.pop(key, None)
