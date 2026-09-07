"""server.app.store —— 服务端存储后端（W-003，M1）。

M1 单文件 backend.py 收口；M2 按需拆分 factory.py / backend_case.py /
backend_cross.py 并新增 state_store.py（backend_api.md 第五部分结构）。
"""
from server.app.store.backend import (
    CaseStore,
    CrossCaseStore,
    StoreBackend,
    StoreFactory,
    UnsupportedOperation,
)

__all__ = [
    "StoreBackend",
    "CaseStore",
    "CrossCaseStore",
    "StoreFactory",
    "UnsupportedOperation",
]
