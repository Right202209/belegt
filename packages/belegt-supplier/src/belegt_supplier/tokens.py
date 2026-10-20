# Spec: 08 §7.1（token 256 位、只存哈希、时效、一个 token 只对应一个 SupplierRequest、补发即旧 token 失效）、07 §4（`slk_` 前缀）、08 §3.2（portal_link 只在渲染时注入）
"""魔法链接 token。

格式：`slk_<body>.<sig>`
- body = base64url(K ‖ exp)，K = HMAC-SHA256(instance_secret, "slk-key|<sreq_id>|<generation>|<exp>")（32 字节），
  exp = 4 字节过期时间（Unix 秒，大端）
- sig  = base64url(HMAC-SHA256(instance_secret, "slk1." + body)[:16])

设计取舍：K 由实例密钥派生而非存储的随机数——256 位、在不知密钥时不可预测；
这样实例在每次渲染催办邮件时都能**重新得出同一链接**，而数据库里只存 `portal_token_hash`
（03 §3.10：明文不入库）。补发时 generation+1，旧 token 的哈希被吊销。
签名用于在查库前拒绝篡改/伪造（防枚举、统一 404）。真正授权以数据库记录为准。
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .signing import b64url, b64url_decode

PREFIX = "slk_"
DEFAULT_GRACE_DAYS = 14   # due_date + 14 天
MAX_TTL_DAYS = 90
READONLY_DAYS_AFTER_CLOSE = 7


@dataclass(frozen=True)
class IssuedToken:
    token: str
    token_hash: str
    expires_at: datetime


def token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def default_expiry(issued_at: datetime, due_date: date, ttl_days: int | None = None) -> datetime:
    """默认 due_date + 14 天，最长 90 天（08 §7.1）。"""
    cap = issued_at + timedelta(days=MAX_TTL_DAYS)
    if ttl_days is not None:
        return min(issued_at + timedelta(days=ttl_days), cap)
    due_end = datetime.combine(due_date + timedelta(days=DEFAULT_GRACE_DAYS), time(23, 59, 59), tzinfo=timezone.utc)
    return min(due_end, cap)


def _sig(secret: bytes, body: str) -> str:
    return b64url(hmac.new(secret, b"slk1." + body.encode("ascii"), hashlib.sha256).digest()[:16])


def derive(secret: bytes, sreq_id: str, generation: int, expires_at: datetime) -> IssuedToken:
    exp = int(expires_at.timestamp())
    key = hmac.new(secret, f"slk-key|{sreq_id}|{generation}|{exp}".encode("utf-8"), hashlib.sha256).digest()
    body = b64url(key + struct.pack(">I", exp))
    tok = f"{PREFIX}{body}.{_sig(secret, body)}"
    return IssuedToken(tok, token_hash(tok), datetime.fromtimestamp(exp, tz=timezone.utc))


def verify_signature(secret: bytes, token: str) -> datetime | None:
    """校验格式与签名；成功返回内嵌过期时间，失败返回 None（调用方统一 404）。"""
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 200:
        return None
    try:
        body, sig = token[len(PREFIX):].split(".", 1)
        if not hmac.compare_digest(_sig(secret, body), sig):
            return None
        raw = b64url_decode(body)
        if len(raw) != 36:
            return None
        (exp,) = struct.unpack(">I", raw[32:])
    except Exception:  # noqa: BLE001
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)
