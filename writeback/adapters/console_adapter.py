"""
writeback/adapters/console_adapter.py
REQ-043 真实 Writeback Adapter —— Console 验证适配器。

将 payload 打印到控制台（模拟外部业务系统接收），生成含业务号的回执。
用于在接入真实业务系统前验证 outbox → dispatcher → adapter 全链路。

满足 AC1–AC5：
  AC1: dry_run() 与 send() payload 逐字节一致
  AC2: 幂等键在真实系统生效（同键重复 send 只产生一条记录）
  AC3: 回执含唯一业务号、版本、终态
  AC4: 涉密字段不出受控环境（控制台输出做脱敏）
  AC5: 对账差异可检出（fetch_status 查无此单 → unknown）
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path

from core.writeback import DryRunResult, ExternalStatus, Receipt


# 涉密字段脱敏正则（AC4：控制台不泄露原文）
# 顺序：长模式优先（身份证/银行卡），短模式后行（手机号需排除更长数字串）
_SENSITIVE_PATTERNS = [
    (re.compile(r'\b\d{17}[\dXx]\b'), '******************'),  # 身份证 18 位
    (re.compile(r'\b\d{16,19}\b'), '****************'),       # 银行卡 16-19 位
    (re.compile(r'(?<!\d)(\d{3})\d{4}(\d{4})(?!\d)'), r'\1****\2'),  # 手机号 11 位（独立）
]


def _mask(value: str) -> str:
    """脱敏字符串中的涉密信息。"""
    for pat, repl in _SENSITIVE_PATTERNS:
        value = pat.sub(repl, value)
    return value


def _mask_payload(payload: dict) -> dict:
    """递归脱敏 payload 中所有字符串值（仅用于控制台输出，不影响真实 payload）。"""
    masked = {}
    for k, v in payload.items():
        if isinstance(v, str):
            masked[k] = _mask(v)
        elif isinstance(v, dict):
            masked[k] = _mask_payload(v)
        else:
            masked[k] = v
    return masked


class ConsoleAdapter:
    """控制台输出适配器（REQ-043 验证用）。

    send() 将 payload 打印到 stdout 并返回含业务号的回执。
    幂等：同 idempotency_key 重复 send 返回原回执，不重复打印（AC2）。
    持久化：记录落 JSON 文件，重启后仍可 fetch_status（AC5 对账）。
    """

    PREFIX = "CON"  # 业务号前缀

    def __init__(self, ledger_path: str | Path = "data/console_adapter_ledger.json",
                 *, stream=None):
        self.path = Path(ledger_path)
        self._stream = stream or sys.stdout
        self._records: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self._records = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self._records = []

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._records, ensure_ascii=False, indent=1),
            encoding="utf-8")

    # ---- WritebackAdapter 协议实现 ----

    def dry_run(self, payload: dict) -> DryRunResult:
        """AC1：返回与 send() 将要发送的完全一致的 payload。"""
        return DryRunResult(payload=json.loads(json.dumps(payload, default=str)))

    def send(self, payload: dict, idempotency_key: str) -> Receipt:
        """发送 payload 到控制台（模拟外部系统），返回回执。

        AC2：同 idempotency_key 已受理 → 返回原回执，不重复输出。
        AC3：回执含唯一业务号、终态 confirmed。
        AC4：控制台输出做脱敏，真实 payload 不受影响。
        """
        # 幂等检查（AC2）
        for r in self._records:
            if r["idempotency_key"] == idempotency_key:
                # 已受理，返回原回执，不重复打印
                self._print(f"[幂等命中] {idempotency_key} → {r['business_id']}（不重复建单）")
                return Receipt(ok=True, status_code=200,
                               external_id=r["business_id"])

        # 生成业务号（AC3：唯一业务号）
        seq = len(self._records) + 1
        business_id = f"{self.PREFIX}-{seq:06d}"

        # 控制台输出（AC4：脱敏后输出）
        masked = _mask_payload(json.loads(json.dumps(payload, default=str)))
        self._print(f"{'=' * 60}")
        self._print(f"[CONSOLE ADAPTER] 模拟外部系统接收")
        self._print(f"  业务号:     {business_id}")
        self._print(f"  幂等键:     {idempotency_key}")
        self._print(f"  时间:       {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._print(f"  Payload（已脱敏）:")
        for line in json.dumps(masked, ensure_ascii=False, indent=2).split("\n"):
            self._print(f"    {line}")
        self._print(f"  终态:       confirmed")
        self._print(f"{'=' * 60}")

        # 持久化记录
        record = {
            "idempotency_key": idempotency_key,
            "business_id": business_id,
            "version": 1,
            "state": "confirmed",
            "payload": json.loads(json.dumps(payload, default=str)),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._records.append(record)
        self._flush()

        return Receipt(ok=True, status_code=200, external_id=business_id)

    def fetch_status(self, external_id: str) -> ExternalStatus:
        """AC5：查外部台账状态，用于对账差异检出。"""
        for r in self._records:
            if r["business_id"] == external_id:
                return ExternalStatus(
                    external_id=external_id,
                    state=r.get("state", "confirmed"),
                    raw={"version": r.get("version", 1),
                         "created_at": r.get("created_at", "")})
        # 查无此单（AC5：对账差异可检出）
        return ExternalStatus(external_id=external_id, state="unknown")

    # ---- 辅助 ----

    def _print(self, msg: str) -> None:
        print(msg, file=self._stream, flush=True)

    def record_count(self) -> int:
        return len(self._records)
