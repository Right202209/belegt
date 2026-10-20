# Spec: 02 §3.6（数值与单位清洗）、§3.1（单位代码）；03 §2.2 DEC-STR
from __future__ import annotations

from decimal import Decimal

import pytest

from belegt_io import classify_null, detect_number_style, extract_quantities, normalize_unit, parse_number, to_dec_str


@pytest.mark.parametrize("raw,lang,value,style", [
    ("1.234,5", "de", "1234.5", "de"),
    ("1.234,5", None, "1234.5", "de"),   # 两种分隔符同时出现：无歧义
    ("1,234.5", "en", "1234.5", "en"),
    ("1.234.567,89", None, "1234567.89", "de"),
    ("4.820.000", None, "4820000", "de"),
    ("12,5", "de", "12.5", "de"),
    ("0,35", None, "0.35", "de"),
    ("1'234.5", None, "1234.5", "ch"),
    ("1\u202f234,5", None, "1234.5", "space"),
    ("-3,75", "de", "-3.75", "de"),
])
def test_parse_number_unambiguous(raw, lang, value, style):
    p = parse_number(raw, lang=lang)
    assert p.value == Decimal(value) and p.style == style and not p.needs_confirmation


def test_thousand_separator_ambiguity_requires_confirmation():
    p = parse_number("1.234", lang="de")
    assert p.value == Decimal(1234) and p.needs_confirmation and p.alternatives == [Decimal("1.234")]
    p = parse_number("1.234", lang="de", doc_style="de")  # 同文档交叉验证一致 → 不需确认
    assert p.value == Decimal(1234) and not p.needs_confirmation and "resolved_by_document_style" in p.reasons
    p = parse_number("1,234", lang="de", doc_style="en")  # 语言与文档格式冲突 → 确认
    assert p.value == Decimal(1234) and p.needs_confirmation
    p = parse_number("1,234")
    assert p.needs_confirmation


def test_detect_number_style():
    assert detect_number_style(["Summe 1.234,5 kWh", "Netto 12.000,00 €", "Menge 2.150"]) == "de"
    assert detect_number_style(["Total 1,234.5 kWh", "Net 12,000.00 USD"]) == "en"
    assert detect_number_style(["2025"]) is None


@pytest.mark.parametrize("raw,unit,reason", [
    ("kWh", "kWh", None), ("KWh", "kWh", None), ("kwh", "kWh", None), ("MWh", "MWh", None),
    ("t CO2e", "tCO2e", None), ("tCO2eq", "tCO2e", None), ("t CO2-Äq.", "tCO2e", None), ("t CO₂e", "tCO2e", None),
    ("tCO2", "tCO2", "co2_not_co2e"), ("ton", "t", "ton_ambiguous"), ("Tonnen", "t", None), ("t", "t", None),
    ("m³", "m3", None), ("%", "%", None), ("Massen-%", "%w/w", None), ("ppm", "ppm", None), ("€", "EUR", None),
    ("kgCO2e/kg", "kgCO2e/kg", None), ("tCO2e/t", "tCO2e/t", None), ("MWh/t", "MWh/t", None),
    ("GWh", "GWh", "unit_not_in_dictionary"),
])
def test_normalize_unit(raw, unit, reason):
    code, reasons = normalize_unit(raw)
    assert code == unit
    assert (reason in reasons) if reason else not reasons


def test_extract_quantities_german_document():
    text = "Stromverbrauch 2025: 1.234,5 MWh; Erdgas 2.150 MWh; Emissionen 1.835,2 t CO2e; Betrag 1.234.567,89 €"
    qs = extract_quantities(text, lang="de")
    vals = [(q.value, q.unit) for q in qs]
    assert (Decimal("1234.5"), "MWh") in vals
    assert (Decimal("2150"), "MWh") in vals  # "2.150" 由同文档德式数字交叉验证
    assert (Decimal("1835.2"), "tCO2e") in vals
    assert (Decimal("1234567.89"), "EUR") in vals
    assert all(not q.needs_confirmation for q in qs)
    assert not any(q.value == Decimal(2025) for q in qs)  # 年份不抽


def test_extract_ranges_and_flags():
    q = extract_quantities("Konzentration < 0,1 %", lang="de")[0]
    assert q.is_range and q.value is None and q.comparator == "<" and q.is_estimate and q.range_text == "< 0.1"
    q = extract_quantities("Bereich 0,3–1,0 %", lang="de")[0]
    assert q.is_range and q.range_text == "0.3–1" and q.unit == "%" and q.value is None
    q = extract_quantities("Anteil 0,35 %", lang="de")[0]
    assert q.value == Decimal("0.35") and q.needs_confirmation and "percent_value_le_1" in q.reasons
    q = extract_quantities("Menge 12 tons", lang="en")[0]
    assert q.unit == "t" and q.needs_confirmation and "ton_ambiguous" in q.reasons
    q = extract_quantities("Emission 120 tCO2", lang="en")[0]
    assert q.unit == "tCO2" and q.needs_confirmation
    q = extract_quantities("ca. 500 kWh", lang="de")[0]
    assert q.value == Decimal(500) and q.is_estimate and q.comparator == "~"


@pytest.mark.parametrize("raw,kind", [
    ("0", "zero"), ("0,0", "zero"), (0, "zero"), ("n/a", "not_applicable"), ("nicht zutreffend", "not_applicable"),
    ("entfällt", "not_applicable"), ("k.A.", "no_data"), ("keine Angabe", "no_data"), ("unbekannt", "no_data"),
    ("vertraulich", "confidential"), ("Confidential", "confidential"), ("1.234", None),
])
def test_classify_null(raw, kind):
    assert classify_null(raw) == kind


def test_dec_str():
    assert to_dec_str(Decimal("2027.200")) == "2027.2"
    assert to_dec_str(Decimal("-0")) == "0"
    assert to_dec_str(Decimal("1E+3")) == "1000"
    assert to_dec_str(Decimal("0.000100")) == "0.0001"
    with pytest.raises(ValueError):
        to_dec_str(Decimal("NaN"))
