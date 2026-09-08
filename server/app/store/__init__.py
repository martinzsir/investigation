"""server.app.store —— 服务端存储后端（W-003，M1）。

M1 单文件 backend.py 收口；M5 拆分 backend_cross.py（跨案件 ATTACH）。
"""
from server.app.store.backend import (
    CaseStore,
    StoreBackend,
    StoreFactory,
    UnsupportedOperation,
)
from server.app.store.backend_cross import CrossCaseStore

__all__ = [
    "StoreBackend",
    "CaseStore",
    "CrossCaseStore",
    "StoreFactory",
    "UnsupportedOperation",
]
