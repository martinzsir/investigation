"""
server/app/security.py
Web 认证基元（S1）：PBKDF2 口令哈希、会话 token 生成、Bearer 提取。

纯标准库；不引第三方鉴权框架。operator 永远取自会话（meta_users/
meta_sessions），请求体里的 operator 字段一律忽略。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (salt, hash)；salt 缺省随机生成。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), _PBKDF2_ITERATIONS)
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, dk = hash_password(password, salt)
    return hmac.compare_digest(dk, expected_hash)


def new_token() -> str:
    """会话 token（secrets，URL-safe）。"""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    """已认证主体（来自会话，不来自请求体）。"""

    operator: str
    role: str
    clearance: int
    tenant_id: str
    token: str


def bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 头提取 Bearer token；其他形式一律 None（S1 无旁路）。"""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()
