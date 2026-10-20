# Spec: 02 §3.1–§3.2 单位换算（Decimal、精确有理换算）；05 §1.3、TV-09、TV-10
from decimal import Decimal
from fractions import Fraction

import pytest

from belegt_dictionary import UnitError, parse_factor, to_dec_str


@pytest.mark.parametrize("value,f,t,expected", [
    ("1000", "kg", "t", "1"),
    ("1234.5", "kgCO2e", "tCO2e", "1.2345"),
    ("1234567", "kWh", "MWh", "1234.567"),
    ("4444.4412", "GJ", "MWh", "1234.567"),           # TV-09: exact ÷3.6, back to the original value
    ("3600000", "GJ", "MWh", "1000000"),               # TV-09: not 1000000.8 (×0.277778)
    ("1234.567", "MWh", "GJ", "4444.4412"),
    ("2.5", "MWh", "kWh", "2500"),
    ("1", "kWh", "GJ", "0.0036"),                      # composed path kWh→MWh→GJ, exact
    ("2500", "L", "m3", "2.5"),
    ("4", "ha", "m2", "40000"),
    ("1000", "ppm", "%w/w", "0.1"),                    # 1000 ppm = 0.1 %w/w
    ("0.85", "kgCO2e/kg", "tCO2e/t", "0.85"),
    ("12.5", "t", "kg", "12500"),
    ("0", "GJ", "MWh", "0"),
])
def test_exact_conversions(shipped_dict, value, f, t, expected):
    out = shipped_dict.convert(Decimal(value), f, t)
    assert isinstance(out, Decimal)
    assert out == Decimal(expected)
    assert to_dec_str(out) == expected


def test_gj_round_trip_is_lossless(shipped_dict):
    for v in ("1", "7.2", "123456.789", "0.000036"):
        mwh = shipped_dict.convert(v, "GJ", "MWh")
        back = shipped_dict.convert(mwh, "MWh", "GJ")
        assert back == Decimal(v) or abs(back - Decimal(v)) < Decimal("1e-28")


def test_factor_is_rational(shipped_dict):
    assert shipped_dict.units.factor("GJ", "MWh") == Fraction(5, 18)
    assert shipped_dict.units.factor("MWh", "GJ") == Fraction(18, 5)
    assert shipped_dict.units.factor("ppm", "%w/w") == Fraction(1, 10000)


def test_non_terminating_uses_34_digits(shipped_dict):
    out = shipped_dict.convert("1", "GJ", "MWh")
    assert str(out).startswith("0.2777777777")
    assert len(str(out).replace("0.", "")) == 34


def test_cross_dimension_rejected(shipped_dict):
    with pytest.raises(UnitError, match="calculation"):
        shipped_dict.convert("1", "MWh", "tCO2e")
    with pytest.raises(UnitError):
        shipped_dict.convert("1", "t", "tCO2e")  # tCO2 vs tCO2e: different dimension (02 §3.6)


def test_unknown_unit_and_float_rejected(shipped_dict):
    with pytest.raises(UnitError, match="unknown unit"):
        shipped_dict.convert("1", "ton", "t")
    with pytest.raises(UnitError, match="float"):
        shipped_dict.convert(1.5, "kg", "t")


def test_no_path_same_dimension(shipped_dict):
    with pytest.raises(UnitError, match="no conversion path"):
        shipped_dict.convert("1", "kgCO2e/kWh", "tCO2e/MWh")


def test_export_only_percent(shipped_dict):
    with pytest.raises(UnitError):
        shipped_dict.convert("35", "%", "1")
    assert shipped_dict.convert("35", "%", "1", allow_export_only=True) == Decimal("0.35")


def test_round_half_even_on_export(shipped_dict):
    # TV-10 style: quantize only at export, banker's rounding
    assert str(shipped_dict.convert("2012.5", "kWh", "MWh", round_digits=3)) == "2.012"
    assert str(shipped_dict.convert("2013.5", "kWh", "MWh", round_digits=3)) == "2.014"
    assert str(shipped_dict.convert("-2012.5", "kWh", "MWh", round_digits=3)) == "-2.012"
    assert str(shipped_dict.convert("1", "GJ", "MWh", round_digits=6)) == "0.277778"


def test_to_canonical(shipped_dict):
    assert shipped_dict.to_canonical("dp:env.ghg.scope1_total", "1500", "kgCO2e") == Decimal("1.5")
    assert shipped_dict.to_canonical("dp:energy.consumption.natural_gas", "1234567", "kWh") == Decimal("1234.567")
    with pytest.raises(ValueError, match="allowed_units"):
        shipped_dict.to_canonical("dp:env.ghg.scope1_total", "1", "t")


@pytest.mark.parametrize("text,frac", [("1/3.6", Fraction(5, 18)), ("0.001", Fraction(1, 1000)), ("10000", Fraction(10000)),
                                       ("3.6", Fraction(18, 5))])
def test_parse_factor(text, frac):
    assert parse_factor(text) == frac


def test_parse_factor_rejects_float_and_zero():
    with pytest.raises(UnitError):
        parse_factor(0.001)
    with pytest.raises(UnitError):
        parse_factor("0")


def test_dec_str():
    assert to_dec_str(Decimal("1E+3")) == "1000"
    assert to_dec_str(Decimal("1.2300")) == "1.23"
    assert to_dec_str(Decimal("-0")) == "0"


def test_ucum_codes(shipped_dict):
    assert shipped_dict.units.ucum("tCO2e") == "t{CO2e}"
    assert shipped_dict.units.ucum("kWh") == "kW.h"
    assert shipped_dict.units.ucum("ha") == "har"
