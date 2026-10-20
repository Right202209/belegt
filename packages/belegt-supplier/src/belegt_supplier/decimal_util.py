# Spec: 03 §2.2（DEC-STR）
"""DEC-STR：十进制字符串，禁止科学计数法与 NaN，最多 18 位有效数字。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

DEC_STR_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
MAX_SIG_DIGITS = 18


def is_dec_str(value: object) -> bool:
    if not isinstance(value, str) or not DEC_STR_RE.match(value):
        return False
    digits = value.lstrip("-").replace(".", "").lstrip("0")
    return len(digits) <= MAX_SIG_DIGITS


def to_decimal(value: str) -> Decimal:
    if not is_dec_str(value):
        raise ValueError(f"not a DEC-STR: {value!r}")
    return Decimal(value)


def dec_str(value: Decimal) -> str:
    """Decimal → DEC-STR（不使用科学计数法）。"""
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    if s in ("-0", ""):
        s = "0"
    return s


def normalize_user_number(raw: str, locale: str | None = None) -> str | None:
    """把供应商输入（可能带千分位、逗号小数）规范化为 DEC-STR；无法确定时返回 None。

    规则：去空白与 NBSP；仅含一个逗号且无点 → 逗号视为小数点（de/tr 习惯）；
    同时含点和逗号 → 最后出现者为小数点，另一者视为千分位。
    """
    if raw is None:
        return None
    s = str(raw).strip().replace("\u00a0", "").replace(" ", "").replace("'", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(",") == 1:
        s = s.replace(",", ".")
    elif s.count(",") > 1:
        s = s.replace(",", "")
    if s.startswith("+"):
        s = s[1:]
    if re.fullmatch(r"-?\.[0-9]+", s):
        s = s.replace(".", "0.", 1)
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite() or "e" in s.lower():
        return None
    out = dec_str(d)
    return out if is_dec_str(out) else None
