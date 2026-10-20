# Spec: 08 §6.5（签名请求清单 JWS、提交包签名）、08 §6.6（Webhook）、03 §4.5（规范化 JSON 精神）
"""规范化 JSON、HS256 紧凑 JWS（仅标准库）与 Webhook HMAC 签名。

- 请求清单与提交包用 JWS（RFC 7515）HS256，租户与实例之间共享密钥；
  可替换为非对称算法（EdDSA）而不改调用方。
- Webhook：`Belegt-Signature: t=<unix>,v1=<hex(HMAC-SHA256(secret, f"{t}.{body}"))>`，容差默认 300 s。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

WEBHOOK_HEADER = "Belegt-Signature"
DEFAULT_TOLERANCE_S = 300


class SignatureError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def canonical_json(obj: Any) -> bytes:
    """排序键、无空白、UTF-8、不转义非 ASCII。数字应已是 DEC-STR 字符串。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_prefixed(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------- JWS HS256
def jws_sign(payload: Any, secret: bytes, kid: str | None = None) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    if kid:
        header["kid"] = kid
    h = b64url(canonical_json(header))
    p = b64url(canonical_json(payload))
    sig = hmac.new(secret, f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"


def jws_verify(token: str, secret: bytes) -> Any:
    try:
        h, p, s = token.split(".")
        header = json.loads(b64url_decode(h))
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("SIG_MALFORMED", str(exc)) from exc
    if header.get("alg") != "HS256":
        raise SignatureError("SIG_ALG_UNSUPPORTED", str(header.get("alg")))
    expected = hmac.new(secret, f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, b64url_decode(s)):
        raise SignatureError("SIG_INVALID", "signature mismatch")
    return json.loads(b64url_decode(p))


def jws_sign_detached(payload: Any, secret: bytes, kid: str | None = None) -> str:
    """RFC 7515 附录 F 的分离载荷：`<header>..<sig>`。"""
    h, _p, s = jws_sign(payload, secret, kid).split(".")
    return f"{h}..{s}"


def jws_verify_detached(signature: str, payload: Any, secret: bytes) -> None:
    try:
        h, empty, s = signature.split(".")
    except ValueError as exc:
        raise SignatureError("SIG_MALFORMED", "detached jws expected") from exc
    if empty:
        raise SignatureError("SIG_MALFORMED", "payload must be detached")
    jws_verify(f"{h}.{b64url(canonical_json(payload))}.{s}", secret)


# ---------------------------------------------------------------- Webhook HMAC
def webhook_signature(secret: bytes, body: bytes, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else int(timestamp)
    mac = hmac.new(secret, f"{t}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_webhook(secret: bytes, body: bytes, header: str | None, *, now: float | None = None,
                   tolerance_s: int = DEFAULT_TOLERANCE_S) -> int:
    """校验 `Belegt-Signature`；成功返回时间戳，失败抛 SignatureError。"""
    if not header:
        raise SignatureError("SIG_MISSING", WEBHOOK_HEADER)
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            parts.setdefault(k, []).append(v)
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise SignatureError("SIG_MALFORMED", "timestamp") from exc
    now = time.time() if now is None else now
    if abs(now - t) > tolerance_s:
        raise SignatureError("SIG_EXPIRED", "timestamp outside tolerance")
    expected = hmac.new(secret, f"{t}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise SignatureError("SIG_INVALID", "signature mismatch")
    return t
