# Spec: 08 §5.1–§5.2 PLAUS-001…027（每条规则：正例命中、反例不命中、严重度与 flag 映射）
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

import pytest

from belegt_supplier.plausibility import (
    ALL_RULE_IDS, CLIENT_SIDE_RULES, RULES, BBoxGeoOracle, PlausContext, PlausValue, run_plausibility,
)
from belegt_supplier.plausibility import geo

P = "p1"
SD, SI, ROUTE, SEC = "dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect", "dp:prod.cbam.production_route", "dp:prod.cbam.specific_electricity_consumption"
INST, CN, CPP = "dp:prod.cbam.installation_id", "dp:prod.identity.cn_code", "dp:prod.cbam.carbon_price_paid"
GEO, PDATE, CTRY, QTY = "dp:supply.eudr.geolocation", "dp:supply.eudr.production_date", "dp:supply.eudr.country_of_production", "dp:supply.eudr.commodity_quantity"
DREF, DVER = "dp:supply.eudr.dds_reference", "dp:supply.eudr.dds_verification_number"
PCF, PCFX, PCFU, PDS, PREF = ("dp:prod.pcf.cradle_to_gate", "dp:prod.pcf.cradle_to_gate.excluding_biogenic",
                             "dp:prod.pcf.declared_unit", "dp:prod.pcf.primary_data_share", "dp:prod.pcf.reference_period")


def V(dp, value, unit=None, pid=P, ps=None, pe=None, raw=None):
    return PlausValue(dp, value, unit, pid, ps, pe, None, raw)


def ctx(**kw):
    base = dict(submission_date=date(2027, 2, 1), request_period_start=date(2026, 1, 1), request_period_end=date(2026, 12, 31),
                requested_cn_codes={P: "72083900"}, cbam_defaults={"72083900": D("2.00")}, geo_oracle=BBoxGeoOracle())
    base.update(kw)
    return PlausContext(**base)


def pt(lon, lat, props=None):
    return json.dumps({"type": "Feature", "properties": props or {}, "geometry": {"type": "Point", "coordinates": [lon, lat]}})


def square(lon, lat, d):
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return json.dumps({"type": "Polygon", "coordinates": [[[round(x, 6), round(y, 6)] for x, y in ring]]})


BR_POLY_10HA = square(-55.123456, -12.123456, 0.002900)     # ≈ 10 ha
BR_POLY_TINY = square(-55.123456, -12.123456, 0.000050)     # ≈ 0.003 ha
BOWTIE = json.dumps({"type": "Polygon", "coordinates": [[[-55.100000, -12.100000], [-55.090000, -12.090000],
                                                          [-55.090000, -12.100000], [-55.100000, -12.090000],
                                                          [-55.100000, -12.100000]]]})

# (rule_id, values, ctx-overrides, expected_severity) —— 正例
POSITIVE = [
    ("PLAUS-001", [V(SD, "0.50", "tCO2e/t")], {}, "warn"),
    ("PLAUS-002", [V(SD, "3.20", "tCO2e/t")], {}, "warn"),
    ("PLAUS-003", [V(SD, "1850", "tCO2e/t")], {}, "block"),
    ("PLAUS-004", [V(SD, "1.05", "tCO2e/t"), V(ROUTE, "bf_bof")], {}, "warn"),
    ("PLAUS-005", [V(SD, "1.10", "tCO2e/t"), V(ROUTE, "eaf")], {}, "warn"),
    ("PLAUS-006", [V(SI, "0.40", "tCO2e/t"), V(SEC, "0.45", "MWh/t")], {"grid_factors": {"TR": D("0.47")}, "installation_country": {P: "TR"}}, "warn"),
    ("PLAUS-007", [V(SEC, "0.20", "MWh/t"), V(ROUTE, "eaf")], {}, "warn"),
    ("PLAUS-008", [V(SD, "-1.2", "tCO2e/t")], {}, "block"),
    ("PLAUS-009", [V(SD, "2.90", "tCO2e/t")], {"previous_values": {(SD, P): D("1.90")}}, "warn"),
    ("PLAUS-010", [V(SD, "1.90", "tCO2e/t")], {"other_channel_values": {(SD, P): [D("1.95")]}}, "warn"),
    ("PLAUS-011", [V(INST, "inst#1")], {}, "warn"),
    ("PLAUS-012", [V(CN, "7208390")], {}, "block"),
    ("PLAUS-013", [V(CPP, "14040", "EUR")], {"origin_country": {P: "TR"}}, "warn"),
    ("PLAUS-014", [V(SD, "1.90", "tCO2e/t")], {}, "info"),
    ("PLAUS-015", [V(GEO, pt(-55.12, -12.12))], {}, "block"),
    ("PLAUS-016", [V(GEO, pt(-30.123456, -15.123456))], {}, "block"),
    ("PLAUS-017", [V(GEO, pt(-55.123456, -12.123456)), V(CTRY, "CI")], {}, "block"),
    ("PLAUS-018", [V(GEO, pt(-55.123456, -12.123456, {"area_ha": 6.5}))], {}, "block"),
    ("PLAUS-019", [V(GEO, BR_POLY_TINY)], {}, "warn"),
    ("PLAUS-020", [V(PDATE, "2027-05-01")], {}, "block"),
    ("PLAUS-021", [V(DREF, "26DEABCDEFGH1234")], {}, "block"),
    ("PLAUS-022", [V(PCF, "2.10", "kgCO2e/kg")], {}, "block"),
    ("PLAUS-023", [V(PCF, "2.50", "kgCO2e/kg"), V(PCFX, "2.00", "kgCO2e/kg")], {}, "warn"),
    ("PLAUS-024", [V(PDS, "120", "%")], {}, "block"),
    ("PLAUS-025", [V(PREF, "2021-01-01/2021-12-31")], {"request_period_start": date(2026, 1, 1)}, "warn"),
    ("PLAUS-026", [V(SD, "1.90", "tCO2e/t", ps=date(2026, 1, 1), pe=date(2026, 6, 30))], {}, "warn"),
    ("PLAUS-027", [V(SD, "1.90", "tCO2e/t"), V(SI, "1.90", "tCO2e/t")], {}, "info"),
]

# 反例：同一规则不命中
NEGATIVE = [
    ("PLAUS-001", [V(SD, "1.85", "tCO2e/t")], {}),
    ("PLAUS-002", [V(SD, "2.50", "tCO2e/t")], {}),
    ("PLAUS-003", [V(SD, "1850", "kgCO2e/t")], {}),       # 选了 kg 单位 → 换算后 1.85 正常
    ("PLAUS-004", [V(SD, "1.95", "tCO2e/t"), V(ROUTE, "bf_bof")], {}),
    ("PLAUS-005", [V(SD, "0.40", "tCO2e/t"), V(ROUTE, "eaf")], {}),
    ("PLAUS-006", [V(SI, "0.21", "tCO2e/t"), V(SEC, "0.45", "MWh/t")], {"grid_factors": {"TR": D("0.47")}, "installation_country": {P: "TR"}}),
    ("PLAUS-007", [V(SEC, "0.45", "MWh/t"), V(ROUTE, "eaf")], {}),
    ("PLAUS-008", [V(SD, "1.85", "tCO2e/t"), V(CPP, "0", "EUR")], {}),  # carbon_price_paid 允许 0
    ("PLAUS-009", [V(SD, "2.00", "tCO2e/t")], {"previous_values": {(SD, P): D("1.90")}}),
    ("PLAUS-010", [V(SD, "1.90", "tCO2e/t")], {"other_channel_values": {(SD, P): [D("1.905")]}}),
    ("PLAUS-011", [V(INST, "TR-INST-000123")], {"previous_installation_ids": {P: "TR-INST-000123"}}),
    ("PLAUS-012", [V(CN, "72083900")], {}),
    ("PLAUS-013", [V(CPP, "14040", "EUR")], {"origin_country": {P: "CN"}}),
    ("PLAUS-014", [V(SD, "1.90", "tCO2e/t")], {"has_verification_report": True}),
    ("PLAUS-015", [V(GEO, pt(-55.123456, -12.123456))], {}),
    ("PLAUS-016", [V(GEO, pt(-55.123456, -12.123456))], {}),
    ("PLAUS-017", [V(GEO, pt(-55.123456, -12.123456)), V(CTRY, "BR")], {}),
    ("PLAUS-018", [V(GEO, pt(-55.123456, -12.123456, {"area_ha": 3.2})), V(GEO, BR_POLY_10HA)], {}),
    ("PLAUS-019", [V(GEO, BR_POLY_10HA)], {}),
    ("PLAUS-020", [V(PDATE, "2026-05-01/2026-06-30")], {}),
    ("PLAUS-021", [V(DREF, "26DEABCDEFGH1234"), V(DVER, "AB12CD34")], {}),
    ("PLAUS-022", [V(PCF, "2.10", "kgCO2e/kg"), V(PCFU, "kilogram")], {"pcf_benchmarks": {"steel": D("2.0")}, "product_material": {P: "steel"}}),
    ("PLAUS-023", [V(PCF, "2.05", "kgCO2e/kg"), V(PCFX, "2.00", "kgCO2e/kg")], {}),
    ("PLAUS-024", [V(PDS, "65", "%")], {}),
    ("PLAUS-025", [V(PREF, "2025-01-01/2025-12-31")], {}),
    ("PLAUS-026", [V(SD, "1.90", "tCO2e/t", ps=date(2026, 1, 1), pe=date(2026, 12, 31))], {}),
    ("PLAUS-027", [V(SD, "1.90", "tCO2e/t"), V(SI, "0.21", "tCO2e/t")], {}),
]


def test_catalog_has_all_rules():
    ids = {f"PLAUS-{i:03d}" for i in range(1, 28)} | {f"PLAUS-{i:03d}" for i in range(30, 36)}
    assert set(ALL_RULE_IDS) == ids and len(ALL_RULE_IDS) == 33
    assert {"PLAUS-009", "PLAUS-010", "PLAUS-013", "PLAUS-014"}.isdisjoint(CLIENT_SIDE_RULES)
    assert {"PLAUS-001", "PLAUS-008", "PLAUS-012", "PLAUS-015", "PLAUS-024"} <= CLIENT_SIDE_RULES


@pytest.mark.parametrize("rule_id,values,over,severity", POSITIVE, ids=[p[0] for p in POSITIVE])
def test_rule_positive(rule_id, values, over, severity):
    rep = run_plausibility(values, ctx(**over), only=frozenset({rule_id}))
    hits = rep.by_rule(rule_id)
    assert hits, f"{rule_id} should fire"
    assert hits[0].severity == severity
    assert rule_id in rep.basis


@pytest.mark.parametrize("rule_id,values,over", NEGATIVE, ids=[n[0] for n in NEGATIVE])
def test_rule_negative(rule_id, values, over):
    rep = run_plausibility(values, ctx(**over), only=frozenset({rule_id}))
    assert rep.by_rule(rule_id) == [], [h.message for h in rep.hits]


def test_positive_and_negative_cover_every_data_rule():
    assert {p[0] for p in POSITIVE} == set(RULES) == {n[0] for n in NEGATIVE}


def test_flag_mapping_unit_suspect_and_basis():
    rep = run_plausibility([V(SD, "1850", "tCO2e/t")], ctx())
    assert rep.flag == "unit_suspect" and rep.blocked
    assert rep.evidence_plausibility()["basis"].startswith("PLAUS-003:ratio=")
    # PLAUS-001/002 不与 003 重复命中
    assert not rep.by_rule("PLAUS-001") and not rep.by_rule("PLAUS-002")


def test_flag_outlier_for_warn_and_ok_for_info():
    assert run_plausibility([V(SD, "0.50", "tCO2e/t")], ctx(has_verification_report=True)).flag == "outlier"
    rep = run_plausibility([V(SD, "1.85", "tCO2e/t")], ctx())
    assert rep.flag == "ok" and rep.by_rule("PLAUS-014")  # info → ok


def test_unit_band_low_side():
    rep = run_plausibility([V(SD, "0.0019", "tCO2e/t")], ctx(), only=frozenset({"PLAUS-003"}))
    assert rep.by_rule("PLAUS-003")


def test_plaus_008_non_dec_str_and_zero():
    rep = run_plausibility([V(SD, "1e3", "tCO2e/t"), V(SI, "0", "tCO2e/t")], ctx(), only=frozenset({"PLAUS-008"}))
    assert len(rep.by_rule("PLAUS-008")) == 2


def test_plaus_012_variants():
    r = run_plausibility([V(CN, "84719000")], ctx(requested_cn_codes={}), only=frozenset({"PLAUS-012"}))
    assert r.hits[0].params["reason"] == "not_annex_i"
    r = run_plausibility([V(CN, "72104900")], ctx(), only=frozenset({"PLAUS-012"}))
    assert r.hits[0].params["reason"] == "product_mismatch"


def test_plaus_013_unit_missing():
    r = run_plausibility([V(CPP, "100", None)], ctx(origin_country={P: "CN"}), only=frozenset({"PLAUS-013"}))
    assert r.hits[0].params == {"reason": "unit"}


def test_plaus_015_details():
    for bad in (pt(-200.123456, 10.123456), BOWTIE, json.dumps({"type": "Polygon", "coordinates": [[[0.1, 0.1], [0.2, 0.2], [0.3, 0.1]]]}), "not json"):
        assert run_plausibility([V(GEO, bad)], ctx(), only=frozenset({"PLAUS-015"})).hits
    # 原文尾随 0 仍算 6 位小数
    raw = '{"type":"Point","coordinates":[-55.120000,-12.100000]}'
    assert not run_plausibility([V(GEO, raw)], ctx(), only=frozenset({"PLAUS-015"})).hits


def test_plaus_016_017_skip_without_oracle_or_unknown_country():
    assert not run_plausibility([V(GEO, pt(-30.123456, -15.123456))], ctx(geo_oracle=None), only=frozenset({"PLAUS-016"})).hits
    assert not run_plausibility([V(GEO, pt(10.123456, 10.123456)), V(CTRY, "XX")], ctx(), only=frozenset({"PLAUS-017"})).hits


def test_plaus_017_buffer_1km():
    # 巴西外接框东界 −34.7；0.005°（≈550 m）以外仍在缓冲内
    assert not run_plausibility([V(GEO, pt(-34.695000, -8.000000)), V(CTRY, "BR")], ctx(), only=frozenset({"PLAUS-017"})).hits
    assert run_plausibility([V(GEO, pt(-34.650000, -8.000000)), V(CTRY, "BR")], ctx(), only=frozenset({"PLAUS-017"})).hits


def test_plaus_019_yield_mismatch():
    r = run_plausibility([V(GEO, BR_POLY_10HA), V(QTY, "900000", "kg")], ctx(max_yield_kg_per_ha=D("5000")), only=frozenset({"PLAUS-019"}))
    assert r.hits and "kg_per_ha" in r.hits[0].params


def test_plaus_020_order_and_cutoff():
    r = run_plausibility([V(PDATE, "2026-06-30/2026-05-01"), V(PDATE, "2019-05-01", pid="p2")], ctx(), only=frozenset({"PLAUS-020"}))
    assert {h.params["reason"] for h in r.hits} == {"order", "pre_cutoff"} and all(h.severity == "warn" for h in r.hits)


def test_plaus_021_format():
    r = run_plausibility([V(DREF, "abc"), V(DVER, "x")], ctx(), only=frozenset({"PLAUS-021"}))
    assert r.hits[0].params["reason"] == "format"


def test_plaus_022_benchmark_ratio():
    r = run_plausibility([V(PCF, "15", "kgCO2e/kg"), V(PCFU, "kilogram")],
                         ctx(pcf_benchmarks={"steel": D("2.0")}, product_material={P: "steel"}), only=frozenset({"PLAUS-022"}))
    assert r.hits[0].severity == "warn"


def test_plaus_025_future():
    r = run_plausibility([V(PREF, "2027-01-01/2027-12-31")], ctx(), only=frozenset({"PLAUS-025"}))
    assert r.hits[0].params["reason"] == "future"


def test_plaus_027_equals_default_is_warn():
    r = run_plausibility([V(SD, "2.00", "tCO2e/t")], ctx(), only=frozenset({"PLAUS-027"}))
    assert r.hits[0].severity == "warn" and r.hits[0].params["reason"] == "equals_default"


def test_geo_area_close_to_expected():
    g = json.loads(BR_POLY_10HA)
    ha = geo.polygon_area_m2(g) / 10_000
    assert 9.5 < ha < 10.5
