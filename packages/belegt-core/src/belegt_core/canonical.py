# Spec: 03 §1.4-6（content_hash）、§2.2（DEC-STR）、§4.5（规范化细则）
"""规范化与内容哈希。

``canonicalize(obj)`` = RFC 8785 JCS，并先做 03 §4.5 规范化：
  - 字符串（含键名）NFC 规范化；
  - 字典中值为 ``null`` 的字段删除（列表中的 null 保留）；空对象 ``{}`` 保留；
  - 数字按 ECMAScript Number→String 规则输出（``72.0 → 72``），整数原样。
DEC-STR 字段的数值规范化（``"4820.0" → "4820"``）在写入规范化阶段按字段完成，见 ``normalize_dec``。

``content_hash(body)`` = ``sha256(JCS(body 去掉 content_hash、created_at))``。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

DEC_STR_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_SIGNIFICANT_DIGITS = 18
GENESIS_HASH = "sha256:" + "0" * 64


# ---------------------------------------------------------------- DEC-STR
def is_dec_str(s: Any) -> bool:
    if not isinstance(s, str) or not DEC_STR_RE.match(s) or len(s) > 40:
        return False
    return significant_digits(s) <= MAX_SIGNIFICANT_DIGITS


def significant_digits(s: str) -> int:
    n = normalize_dec(s)
    if not isinstance(n, str):
        return 10**6
    digits = n.lstrip("-").replace(".", "").lstrip("0")
    if "." not in n:
        digits = digits.rstrip("0")
    return max(len(digits), 1)


def normalize_dec(value: Any) -> Any:
    """把数字型 DEC-STR 规范化（去前导零、末尾无意义零、``-0 → 0``）。

    接受 ``Decimal``/``int``/可解析字符串；不可解析或含指数/NaN 的值原样返回（交给 schema 拒绝）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        s = value.strip()
        if not re.match(r"^-?[0-9]+(\.[0-9]+)?$", s):
            return value
        try:
            d = Decimal(s)
        except InvalidOperation:  # pragma: no cover
            return value
    else:
        return value
    if not d.is_finite():
        return value
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("bool is not a number")
    if isinstance(value, (int, str)):
        return Decimal(str(value))
    raise ValueError(f"not a DEC-STR: {value!r}")


def dec_str(value: Any) -> str:
    """Decimal/int/str → 规范 DEC-STR。"""
    out = normalize_dec(value if not isinstance(value, float) else repr(value))
    if not isinstance(out, str) or not DEC_STR_RE.match(out):
        raise ValueError(f"not representable as DEC-STR: {value!r}")
    return out


# ---------------------------------------------------------------- JCS
def _es_number(x: float | int) -> str:
    if isinstance(x, bool):  # pragma: no cover - guarded by caller
        raise TypeError
    if isinstance(x, int):
        return str(x)
    if math.isnan(x) or math.isinf(x):
        raise ValueError("NaN/Infinity not allowed in JCS")
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _es_number(-x)
    r = repr(x)  # 最短往返表示
    if "e" in r or "E" in r:
        mant, exp = r.lower().split("e")
        exp_i = int(exp)
    else:
        mant, exp_i = r, 0
    if "." in mant:
        ip, fp = mant.split(".")
    else:
        ip, fp = mant, ""
    digits = (ip + fp).lstrip("0")
    # n：小数点位置（value = 0.d1d2... × 10^n）
    lead_zeros = len(ip + fp) - len((ip + fp).lstrip("0"))
    n = len(ip) + exp_i - lead_zeros
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    sign = "+" if e >= 0 else "-"
    if k == 1:
        return f"{digits}e{sign}{abs(e)}"
    return f"{digits[0]}.{digits[1:]}e{sign}{abs(e)}"


def _es_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _prepare(o: Any) -> Any:
    """NFC + 删除 null 字段 + Decimal → DEC-STR。"""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if v is None:
                continue
            out[unicodedata.normalize("NFC", str(k))] = _prepare(v)
        return out
    if isinstance(o, (list, tuple)):
        return [_prepare(v) for v in o]
    if isinstance(o, str):
        return unicodedata.normalize("NFC", o)
    if isinstance(o, Decimal):
        return dec_str(o)
    return o


def _serialize(o: Any, out: list[str]) -> None:
    if o is None:
        out.append("null")
    elif o is True:
        out.append("true")
    elif o is False:
        out.append("false")
    elif isinstance(o, (int, float)):
        out.append(_es_number(o))
    elif isinstance(o, str):
        out.append(_es_string(o))
    elif isinstance(o, list):
        out.append("[")
        for i, v in enumerate(o):
            if i:
                out.append(",")
            _serialize(v, out)
        out.append("]")
    elif isinstance(o, dict):
        out.append("{")
        keys = sorted(o.keys(), key=lambda k: k.encode("utf-16-be"))
        for i, k in enumerate(keys):
            if i:
                out.append(",")
            out.append(_es_string(k))
            out.append(":")
            _serialize(o[k], out)
        out.append("}")
    else:
        raise TypeError(f"not JSON serializable: {type(o).__name__}")


def canonicalize(obj: Any, *, strip_nulls: bool = True) -> str:
    """RFC 8785 JCS 字符串（含 03 §4.5 规范化）。"""
    prepared = _prepare(obj) if strip_nulls else obj
    out: list[str] = []
    _serialize(prepared, out)
    return "".join(out)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_tag(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + sha256_hex(data)


def hash_obj(obj: Any) -> str:
    """``sha256:`` + hex(SHA-256(UTF-8(JCS(obj))))。"""
    return sha256_tag(canonicalize(obj))


CONTENT_HASH_EXCLUDE = ("content_hash", "created_at")


def content_hash(body: dict[str, Any]) -> str:
    return hash_obj({k: v for k, v in body.items() if k not in CONTENT_HASH_EXCLUDE})


def nfc_deep(o: Any) -> Any:
    """仅做 NFC（保留 null），用于写入规范化。"""
    if isinstance(o, dict):
        return {unicodedata.normalize("NFC", k): nfc_deep(v) for k, v in o.items()}
    if isinstance(o, list):
        return [nfc_deep(v) for v in o]
    if isinstance(o, str):
        return unicodedata.normalize("NFC", o)
    return o
