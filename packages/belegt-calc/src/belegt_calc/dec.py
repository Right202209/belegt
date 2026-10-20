# Spec: 05 §1.2（十进制精度与舍入）、03 §2.2（DEC-STR）、03 §4.5-2（规范化）
"""十进制工具。

规则（05 §1.2）：
- 上下文 ``prec=34``、``ROUND_HALF_EVEN``；捕获 InvalidOperation / DivisionByZero / Overflow。
- 加、减、乘必须精确；只有除法允许在 34 位精度处截断。
- 同一计算内不做中间舍入；结果只在写出时舍入一次；breakdown 按 10 位小数量化。

实现：中间量用 :class:`Rat`（有理数，分子分母均为整数）承载。加、减、乘在 ``Rat`` 上精确完成；
若一个"有限小数"中间量需要超过 34 位有效数字（即 ``decimal`` 中会触发 ``Inexact``），报
``CALC_PRECISION_OVERFLOW``。所有除法在 ``Rat`` 中保留为分母，到量化输出时才执行**唯一一次**
``Decimal`` 除法（prec=34、HALF_EVEN），再量化到目标位数。这正是 05 §1.2 的语义：
把每个输出量的除法安排在最后一步，避免"截断后的值再参与乘法"导致的 Inexact。
"""
from __future__ import annotations

import re
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    localcontext,
)
from fractions import Fraction
from typing import Union

from .errors import CALC_DEC_INVALID, CALC_PRECISION_OVERFLOW, CalcError

PREC = 34
MAX_SIG_DIGITS = 18          # DEC-STR 最多 18 位有效数字（03 §2.2）
BREAKDOWN_DIGITS = 10        # breakdown 量化位数（05 §1.2）
DEC_STR_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_MAX_BITS = 4000             # 防御：有理数分子/分母位长上限


def calc_context() -> Context:
    """05 §1.2 规定的十进制上下文。"""
    return Context(
        prec=PREC,
        rounding=ROUND_HALF_EVEN,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def sig_digits(d: Decimal) -> int:
    """有效数字位数（去掉前导与末尾零）。0 记 1 位。"""
    if d == 0:
        return 1
    t = d.normalize(Context(prec=1000)).as_tuple()
    return len(t.digits)


def parse_dec(value: object, *, what: str = "value", max_sig: int | None = MAX_SIG_DIGITS) -> Decimal:
    """解析 DEC-STR，失败报 ``CALC_DEC_INVALID``。只接受字符串（禁止 float/科学计数法/NaN）。"""
    if isinstance(value, bool) or not isinstance(value, str):
        raise CalcError(CALC_DEC_INVALID, f"{what}: DEC-STR must be a string, got {type(value).__name__} {value!r}")
    if not DEC_STR_RE.match(value):
        raise CalcError(CALC_DEC_INVALID, f"{what}: {value!r} is not a DEC-STR")
    d = Decimal(value)
    if max_sig is not None and sig_digits(d) > max_sig:
        raise CalcError(CALC_DEC_INVALID, f"{what}: {value!r} has more than {max_sig} significant digits")
    return d


def is_dec_str(value: object) -> bool:
    return isinstance(value, str) and bool(DEC_STR_RE.match(value))


def norm(d: Decimal) -> str:
    """规范化写出：去末尾无意义零，``-0 → 0``，不用科学计数法（05 §1.2、03 §4.5-2）。"""
    if not d.is_finite():
        raise CalcError(CALC_DEC_INVALID, f"non-finite decimal {d}")
    if d == 0:
        return "0"
    with localcontext(Context(prec=1000)):
        s = format(d.normalize(), "f")
    return s


def norm_dec_str(s: str, *, what: str = "value") -> str:
    """把 DEC-STR 规范化（``"4820.0" → "4820"``、``"2.10" → "2.1"``、``"-0" → "0"``）。"""
    return norm(parse_dec(s, what=what, max_sig=None))


def _is_terminating(den: int) -> bool:
    while den % 2 == 0:
        den //= 2
    while den % 5 == 0:
        den //= 5
    return den == 1


Number = Union["Rat", Decimal, int, str]


class Rat:
    """精确有理数中间量（不可变）。"""

    __slots__ = ("_f",)

    def __init__(self, value: Union[Fraction, int] = 0) -> None:
        f = Fraction(value)
        if f.numerator.bit_length() > _MAX_BITS or f.denominator.bit_length() > _MAX_BITS:
            raise CalcError(CALC_PRECISION_OVERFLOW, "intermediate value exceeds supported size")
        if _is_terminating(f.denominator) and f.numerator != 0:
            # 有限小数：检查有效数字是否超过 34（等价于 decimal 中加/减/乘触发 Inexact）
            digits = _terminating_sig_digits(f)
            if digits > PREC:
                raise CalcError(
                    CALC_PRECISION_OVERFLOW,
                    f"exact arithmetic result needs {digits} significant digits (> {PREC})",
                )
        self._f = f

    # --- 构造 ---
    @classmethod
    def of(cls, v: Number) -> "Rat":
        if isinstance(v, Rat):
            return v
        if isinstance(v, bool):
            raise TypeError("bool is not a number")
        if isinstance(v, int):
            return cls(v)
        if isinstance(v, Decimal):
            if not v.is_finite():
                raise CalcError(CALC_DEC_INVALID, f"non-finite decimal {v}")
            return cls(Fraction(v))
        if isinstance(v, str):
            return cls(Fraction(parse_dec(v, max_sig=None)))
        if isinstance(v, Fraction):
            return cls(v)
        raise TypeError(f"cannot convert {type(v).__name__} to Rat")

    @property
    def fraction(self) -> Fraction:
        return self._f

    # --- 运算（精确） ---
    def __add__(self, o: Number) -> "Rat":
        return Rat(self._f + Rat.of(o)._f)

    __radd__ = __add__

    def __sub__(self, o: Number) -> "Rat":
        return Rat(self._f - Rat.of(o)._f)

    def __rsub__(self, o: Number) -> "Rat":
        return Rat(Rat.of(o)._f - self._f)

    def __mul__(self, o: Number) -> "Rat":
        return Rat(self._f * Rat.of(o)._f)

    __rmul__ = __mul__

    def __truediv__(self, o: Number) -> "Rat":
        d = Rat.of(o)._f
        if d == 0:
            raise CalcError(CALC_DEC_INVALID, "division by zero")
        return Rat(self._f / d)

    def __rtruediv__(self, o: Number) -> "Rat":
        return Rat.of(o) / self

    def __neg__(self) -> "Rat":
        return Rat(-self._f)

    def __abs__(self) -> "Rat":
        return Rat(abs(self._f))

    # --- 比较 ---
    def _cmp(self, o: Number) -> Fraction:
        return self._f - Rat.of(o)._f

    def __eq__(self, o: object) -> bool:  # type: ignore[override]
        try:
            return self._cmp(o) == 0  # type: ignore[arg-type]
        except TypeError:
            return NotImplemented  # type: ignore[return-value]

    def __hash__(self) -> int:
        return hash(self._f)

    def __lt__(self, o: Number) -> bool:
        return self._cmp(o) < 0

    def __le__(self, o: Number) -> bool:
        return self._cmp(o) <= 0

    def __gt__(self, o: Number) -> bool:
        return self._cmp(o) > 0

    def __ge__(self, o: Number) -> bool:
        return self._cmp(o) >= 0

    def __bool__(self) -> bool:
        return self._f != 0

    def __repr__(self) -> str:
        return f"Rat({self._f})"

    # --- 输出 ---
    def to_decimal(self) -> Decimal:
        """执行唯一一次十进制除法（prec=34，HALF_EVEN；05 §1.2 只允许除法截断）。"""
        try:
            with localcontext(calc_context()):
                return Decimal(self._f.numerator) / Decimal(self._f.denominator)
        except (InvalidOperation, Overflow, DivisionByZero) as e:  # pragma: no cover - 防御
            raise CalcError(CALC_PRECISION_OVERFLOW, f"decimal division failed: {e!r}") from e


def _terminating_sig_digits(f: Fraction) -> int:
    """有限小数 f（分母只含因子 2、5）的有效数字位数。"""
    d = f.denominator
    a = b = 0
    while d % 2 == 0:
        d //= 2
        a += 1
    while d % 5 == 0:
        d //= 5
        b += 1
    k = max(a, b)
    n = abs(f.numerator) * (10**k) // f.denominator
    s = str(n).rstrip("0") or "0"
    return len(s)


def quantize(value: Number, digits: int) -> Decimal:
    """量化到 ``digits`` 位小数，ROUND_HALF_EVEN；超出 34 位精度报 ``CALC_PRECISION_OVERFLOW``。"""
    if not isinstance(digits, int) or digits < 0 or digits > 34:
        raise CalcError(CALC_DEC_INVALID, f"invalid rounding digits {digits!r}")
    d = Rat.of(value).to_decimal() if not isinstance(value, Decimal) else value
    try:
        with localcontext(calc_context()):
            q = d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
    except InvalidOperation as e:
        raise CalcError(CALC_PRECISION_OVERFLOW, f"cannot quantize {d} to {digits} digits within prec {PREC}") from e
    return q


def round_str(value: Number, digits: int) -> str:
    """量化并规范化写出（结果值用）。"""
    return norm(quantize(value, digits))


def breakdown_str(value: Number) -> str:
    """breakdown 值：10 位小数 HALF_EVEN 量化后规范化（05 §1.2）。"""
    return norm(quantize(value, BREAKDOWN_DIGITS))


ZERO = Rat(0)
ONE = Rat(1)
