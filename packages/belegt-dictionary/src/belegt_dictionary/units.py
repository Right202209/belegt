# Spec: 02 §3.1 (单位代码), §3.2 (换算表、精确有理换算、舍入); 05 §1.2 (Decimal prec=34, ROUND_HALF_EVEN), TV-09
"""Exact unit conversion.

Conversion factors are stored as rational strings (``"1/3.6"``, ``"0.001"``, ``"10000"``) and
handled as :class:`fractions.Fraction`; a path is composed exactly and applied to a
:class:`decimal.Decimal` with one multiplication and one division, so ``GJ→MWh`` is an exact
``÷3.6`` (TV-09: 4444.4412 GJ → 1234.567 MWh). Only parameter-free linear conversions exist
here; anything needing calorific values, densities, factors or FX rates is a calculation.
"""
from __future__ import annotations

from collections import deque
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from fractions import Fraction
from typing import Iterable

from .models import Unit

DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)


class UnitError(ValueError):
    """Unknown unit, cross-dimension conversion or missing conversion path."""


def parse_factor(text: str | int | float) -> Fraction:
    """Parse ``"a"``, ``"a.b"`` or ``"a/b"`` (each side a decimal literal) into an exact Fraction."""
    if isinstance(text, float):
        raise UnitError("factor must be a string or int, not float (exactness)")
    s = str(text).strip()
    if "/" in s:
        num, den = s.split("/", 1)
        f = Fraction(Decimal(num.strip())) / Fraction(Decimal(den.strip()))
    else:
        f = Fraction(Decimal(s))
    if f == 0:
        raise UnitError("factor must be non-zero")
    return f


class UnitRegistry:
    """Units table (units/units.yaml) plus conversions (units/conversions.yaml)."""

    def __init__(self, units: Iterable[Unit], conversions: Iterable[dict]):
        self.units: dict[str, Unit] = {u.code: u for u in units}
        # adjacency: from -> {to: (Fraction, export_only)}
        self._edges: dict[str, dict[str, tuple[Fraction, bool]]] = {}
        self.conversions: list[dict] = []
        for c in conversions:
            f, t = c["from"], c["to"]
            fac = parse_factor(c["factor"])
            exp = bool(c.get("export_only", False))
            self.conversions.append(dict(c))
            self._edges.setdefault(f, {})[t] = (fac, exp)
            # exact inverse; an explicitly listed reverse entry (e.g. MWh→GJ ×3.6) takes precedence
            self._edges.setdefault(t, {}).setdefault(f, (1 / fac, exp))

    # ------------------------------------------------------------------ queries
    def __contains__(self, code: str) -> bool:
        return code in self.units

    def dimension(self, code: str) -> str:
        try:
            return self.units[code].dimension
        except KeyError as e:
            raise UnitError(f"unknown unit {code!r}") from e

    def same_dimension(self, a: str, b: str) -> bool:
        return self.dimension(a) == self.dimension(b)

    def factor(self, from_unit: str, to_unit: str, *, allow_export_only: bool = False) -> Fraction:
        """Exact multiplicative factor from ``from_unit`` to ``to_unit`` (BFS over the conversion graph)."""
        for u in (from_unit, to_unit):
            if u not in self.units:
                raise UnitError(f"unknown unit {u!r}")
        if from_unit == to_unit:
            return Fraction(1)
        if not self.same_dimension(from_unit, to_unit):
            raise UnitError(f"cross-dimension conversion {from_unit} ({self.dimension(from_unit)}) → "
                            f"{to_unit} ({self.dimension(to_unit)}) is a calculation, not a unit conversion")
        seen = {from_unit}
        q: deque[tuple[str, Fraction]] = deque([(from_unit, Fraction(1))])
        while q:
            cur, acc = q.popleft()
            for nxt, (fac, exp) in self._edges.get(cur, {}).items():
                if exp and not allow_export_only:
                    continue
                if nxt in seen:
                    continue
                nacc = acc * fac
                if nxt == to_unit:
                    return nacc
                seen.add(nxt)
                q.append((nxt, nacc))
        raise UnitError(f"no conversion path {from_unit} → {to_unit}")

    def has_path(self, from_unit: str, to_unit: str, *, allow_export_only: bool = False) -> bool:
        try:
            self.factor(from_unit, to_unit, allow_export_only=allow_export_only)
            return True
        except UnitError:
            return False

    def convert(self, value: Decimal | str | int, from_unit: str, to_unit: str, *,
                round_digits: int | None = None, allow_export_only: bool = False) -> Decimal:
        """Convert exactly; optional ``round_digits`` quantizes with ROUND_HALF_EVEN (export only, 02 §3.2)."""
        if isinstance(value, float):
            raise UnitError("value must be Decimal/str/int, not float")
        try:
            v = Decimal(value) if not isinstance(value, Decimal) else value
        except InvalidOperation as e:
            raise UnitError(f"invalid decimal {value!r}") from e
        if not v.is_finite():
            raise UnitError("value must be finite")
        fac = self.factor(from_unit, to_unit, allow_export_only=allow_export_only)
        ctx = DECIMAL_CONTEXT
        out = ctx.divide(ctx.multiply(v, Decimal(fac.numerator)), Decimal(fac.denominator))
        if round_digits is not None:
            out = out.quantize(Decimal(1).scaleb(-round_digits), rounding=ROUND_HALF_EVEN, context=ctx)
        return normalize_decimal(out) if round_digits is None else out

    def ucum(self, code: str) -> str | None:
        return self.units[code].ucum if code in self.units else None


def normalize_decimal(d: Decimal) -> Decimal:
    """Strip trailing zeros without switching to exponent notation for integers (1E+3 → 1000)."""
    n = d.normalize(DECIMAL_CONTEXT)
    if n == n.to_integral_value():
        return n.quantize(Decimal(1), context=DECIMAL_CONTEXT)
    return n


def to_dec_str(d: Decimal) -> str:
    """DEC-STR serialization (03 §2.2): plain decimal, no exponent."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s
