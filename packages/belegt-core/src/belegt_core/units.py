# Spec: 03 §2.2（单位）、02 §3.1/§3.2（单位表与换算表）、03 §2.3 R-UNIT
"""单位量纲与确定性线性换算。

优先使用 ``belegt_dictionary.load_dictionary().convert``（可导入时）；否则使用本模块内置的最小单位维度表
（02 §3.1 的子集）与换算表（02 §3.2，全部为精确有理数系数）。
"""
from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
from typing import Any, Protocol


class UnitError(ValueError):
    pass


# 内置最小单位维度表：unit -> (dimension, factor_to_base as Fraction)
_BUILTIN: dict[str, tuple[str, Fraction]] = {
    "t": ("mass", Fraction(1000)),
    "kg": ("mass", Fraction(1)),
    "tCO2e": ("mass_co2e", Fraction(1000)),
    "kgCO2e": ("mass_co2e", Fraction(1)),
    "kWh": ("energy", Fraction(1, 1000)),
    "MWh": ("energy", Fraction(1)),
    "GJ": ("energy", Fraction(1, 36) * 10),  # 1 GJ = 1/3.6 MWh
    "m3": ("volume", Fraction(1)),
    "L": ("volume", Fraction(1, 1000)),
    "m2": ("area", Fraction(1)),
    "ha": ("area", Fraction(10000)),
    "%": ("ratio_percent", Fraction(1)),
    "%w/w": ("mass_fraction", Fraction(1)),
    "ppm": ("mass_fraction", Fraction(1, 10000)),
    "1": ("count", Fraction(1)),
    "h": ("time", Fraction(1)),
    "EUR": ("currency_eur", Fraction(1)),
    "tCO2e/t": ("intensity_co2e_per_mass", Fraction(1)),
    "kgCO2e/kg": ("intensity_co2e_per_mass", Fraction(1)),
    "kgCO2e/kWh": ("intensity_co2e_per_energy_kwh", Fraction(1)),
    "MWh/t": ("energy_per_mass", Fraction(1)),
    "tCO2e/EUR": ("intensity_co2e_per_eur", Fraction(1)),
    "tCO2e/MWh": ("factor_co2e_per_energy", Fraction(1)),
    "tCO2e/GJ": ("factor_co2e_per_gj", Fraction(1)),
    "kgCO2e/L": ("factor_co2e_per_volume", Fraction(1)),
    "kg/MWh": ("factor_mass_per_energy", Fraction(1)),
    "EUR/tCO2e": ("price_eur_per_co2e", Fraction(1)),
}


class UnitSystem(Protocol):
    def compatible(self, a: str, b: str) -> bool: ...
    def convert(self, value: Decimal, from_unit: str, to_unit: str) -> Decimal: ...
    def known(self, unit: str) -> bool: ...


class BuiltinUnits:
    source = "builtin"

    def known(self, unit: str) -> bool:
        return unit in _BUILTIN

    def dimension(self, unit: str) -> str:
        if unit not in _BUILTIN:
            raise UnitError(f"unknown unit {unit!r}")
        return _BUILTIN[unit][0]

    def compatible(self, a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            return self.dimension(a) == self.dimension(b)
        except UnitError:
            return False

    def convert(self, value: Decimal, from_unit: str, to_unit: str) -> Decimal:
        if from_unit == to_unit:
            return Decimal(value)
        if not self.compatible(from_unit, to_unit):
            raise UnitError(f"cannot convert {from_unit} -> {to_unit}")
        f = _BUILTIN[from_unit][1] / _BUILTIN[to_unit][1]
        with localcontext() as ctx:
            ctx.prec = 34
            return Decimal(value) * Decimal(f.numerator) / Decimal(f.denominator)


class DictionaryUnits:
    """适配 ``belegt_dictionary`` 的 ``Dictionary.convert``；未知单位回落到内置表。"""

    source = "belegt_dictionary"

    def __init__(self, dictionary: Any) -> None:
        self._d = dictionary
        self._fallback = BuiltinUnits()

    def known(self, unit: str) -> bool:
        units = getattr(self._d, "units", None)
        try:
            if units is not None and unit in units:
                return True
        except TypeError:  # pragma: no cover
            pass
        return self._fallback.known(unit)

    def convert(self, value: Decimal, from_unit: str, to_unit: str) -> Decimal:
        if from_unit == to_unit:
            return Decimal(value)
        try:
            return Decimal(self._d.convert(Decimal(value), from_unit, to_unit))
        except Exception:
            return self._fallback.convert(value, from_unit, to_unit)

    def compatible(self, a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            self.convert(Decimal(1), a, b)
            return True
        except Exception:
            return False


@lru_cache(maxsize=1)
def default_units() -> UnitSystem:
    try:  # 优先字典包
        import belegt_dictionary  # type: ignore

        return DictionaryUnits(belegt_dictionary.load_dictionary())
    except Exception:
        return BuiltinUnits()
