"""
core/features.py
L1 特征落盘与版本化（REQ-015）。

旧实现：L1 是 Store 上的进程内 dict，运行结束即失，跨会话无记忆，
采样预演的实验结论无法复用。

新实现：feat_l1 不可变表（DuckDB），内存 dict 仅作加速缓存——
  - feature_set_hash = sha1(entity_id | time_window | feature_name | 规范化 inputs)，
    相同输入永远同 hash（AC2），写入后重启仍可按 hash 读回（AC1）；
  - 每条特征记录计算时的数据版本（ontology build_id）；源版本推进后
    mark_stale() 把旧版本特征置 stale=true（AC3）；
  - get_or_compute()：缓存 miss 自动回源重算并落盘（AC5）；
  - 评分/规则输入经 feature_ref() 携带 feature_set_hash 引用（AC4 签名断言），
    检测器接入在后续批次（REQ-027 阈值自适应消费）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

_DDL = """
CREATE TABLE IF NOT EXISTS feat_l1 (
    feature_set_hash VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    time_window VARCHAR NOT NULL,
    feature_name VARCHAR NOT NULL,
    payload VARCHAR NOT NULL,
    inputs_hash VARCHAR NOT NULL,
    data_version VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    stale BOOLEAN NOT NULL DEFAULT false
)
"""


@dataclass(frozen=True)
class FeatureRecord:
    feature_set_hash: str
    entity_id: str
    time_window: str
    feature_name: str
    payload: Any
    inputs_hash: str
    data_version: str
    created_at: str
    stale: bool

    def to_dict(self) -> dict:
        return {
            "feature_set_hash": self.feature_set_hash,
            "entity_id": self.entity_id,
            "time_window": self.time_window,
            "feature_name": self.feature_name,
            "payload": self.payload,
            "inputs_hash": self.inputs_hash,
            "data_version": self.data_version,
            "created_at": self.created_at,
            "stale": self.stale,
        }


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def feature_set_hash(entity_id: str, time_window: str,
                     feature_name: str, inputs: Any) -> str:
    """确定性特征集哈希：同输入同 hash（AC2）。"""
    basis = _canonical({
        "entity_id": entity_id,
        "time_window": time_window,
        "feature_name": feature_name,
        "inputs": inputs,
    })
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def feature_ref(record_or_hash) -> dict:
    """评分/规则输入引用特征时的签名包装（AC4）：
    规则/评分入参携带 {"feature_set_hash": ...}，保证可溯源到特征版本。"""
    h = record_or_hash.feature_set_hash if isinstance(
        record_or_hash, FeatureRecord) else record_or_hash
    return {"feature_set_hash": h}


class FeatureStore:
    """feat_l1 持久化特征层（+ 进程内热缓存）。"""

    def __init__(self, conn):
        self._conn = conn
        conn.execute(_DDL)
        self._cache: dict[str, FeatureRecord] = {}

    # ---- 写 ----
    def put(self, *, entity_id: str, time_window: str, feature_name: str,
            payload: Any, inputs: Any, data_version: str) -> FeatureRecord:
        """put-if-absent：同 feature_set_hash 已存在则原样返回（不可变，
        保留首次计算的版本与时间）；否则写入并返回新记录。"""
        h = feature_set_hash(entity_id, time_window, feature_name, inputs)
        existing = self.get(h)
        if existing is not None:
            return existing
        inputs_hash = hashlib.sha256(_canonical(inputs).encode("utf-8")).hexdigest()
        rec = FeatureRecord(
            feature_set_hash=h, entity_id=entity_id, time_window=time_window,
            feature_name=feature_name, payload=payload, inputs_hash=inputs_hash,
            data_version=data_version,
            created_at=datetime.now().isoformat(timespec="seconds"),
            stale=False)
        self._conn.execute(
            """INSERT OR IGNORE INTO feat_l1
               (feature_set_hash, entity_id, time_window, feature_name, payload,
                inputs_hash, data_version, created_at, stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, false)""",
            [h, entity_id, time_window, feature_name,
             json.dumps(payload, ensure_ascii=False, default=str),
             inputs_hash, data_version, rec.created_at])
        self._cache[h] = rec
        return rec

    # ---- 读 ----
    def get(self, feature_set_hash: str) -> FeatureRecord | None:
        if feature_set_hash in self._cache:
            return self._cache[feature_set_hash]
        row = self._conn.execute(
            "SELECT feature_set_hash, entity_id, time_window, feature_name, payload, "
            "inputs_hash, data_version, created_at, stale "
            "FROM feat_l1 WHERE feature_set_hash=?",
            [feature_set_hash]).fetchone()
        if not row:
            return None
        rec = FeatureRecord(
            feature_set_hash=row[0], entity_id=row[1], time_window=row[2],
            feature_name=row[3], payload=json.loads(row[4]), inputs_hash=row[5],
            data_version=row[6], created_at=row[7], stale=bool(row[8]))
        self._cache[rec.feature_set_hash] = rec
        return rec

    def get_or_compute(self, *, entity_id: str, time_window: str,
                       feature_name: str, inputs: Any, data_version: str,
                       compute_fn: Callable[[], Any]) -> FeatureRecord:
        """缓存 miss 自动回源重算并落盘（AC5）；命中不重算。"""
        h = feature_set_hash(entity_id, time_window, feature_name, inputs)
        existing = self.get(h)
        if existing is not None:
            return existing
        payload = compute_fn()
        return self.put(entity_id=entity_id, time_window=time_window,
                        feature_name=feature_name, payload=payload,
                        inputs=inputs, data_version=data_version)

    # ---- 版本化 ----
    def mark_stale(self, current_data_version: str) -> int:
        """数据版本推进后，把非当前版本特征置 stale=true，返回受影响行数（AC3）。"""
        before = self.count_stale()
        self._conn.execute(
            "UPDATE feat_l1 SET stale=true WHERE data_version <> ? AND stale=false",
            [current_data_version])
        self._cache.clear()
        return self.count_stale() - before

    def count_stale(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM feat_l1 WHERE stale=true").fetchone()[0]

    def stale_hashes(self) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT feature_set_hash FROM feat_l1 WHERE stale=true ORDER BY feature_set_hash"
        ).fetchall()]

    def all_for_entity(self, entity_id: str) -> list[FeatureRecord]:
        rows = self._conn.execute(
            "SELECT feature_set_hash, entity_id, time_window, feature_name, payload, "
            "inputs_hash, data_version, created_at, stale "
            "FROM feat_l1 WHERE entity_id=? ORDER BY feature_set_hash",
            [entity_id]).fetchall()
        return [FeatureRecord(
            feature_set_hash=r[0], entity_id=r[1], time_window=r[2],
            feature_name=r[3], payload=json.loads(r[4]), inputs_hash=r[5],
            data_version=r[6], created_at=r[7], stale=bool(r[8])) for r in rows]
