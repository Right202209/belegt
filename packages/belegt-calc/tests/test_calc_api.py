# Spec: 05 §1–§8（API、注册表、各方法规则、因子库选择与冻结、错误码）
from __future__ import annotations

import copy

import pytest

from belegt_calc import (
    ERROR_CODES,
    FAMILIES,
    CalcError,
    InputValue,
    Period,
    calculate,
    list_methods,
    load_factor_set,
    replay,
)
from belegt_calc.factors import deprecate, factor_set_from_dict, freeze, select_row
from belegt_calc.methods.pcf import check_supplier_pcf, decide_allocation

from calc_testutil import FIXTURES, Y2025, inp

S1P = {"consolidation": "operational_control", "gwp_set": "ar6"}


def fs_of(rows, fs_id="fs:belegt-unittest@1"):
    src = fs_id[3:].split("@")[0]
    return freeze(factor_set_from_dict({"id": fs_id, "source": src, "license": "Apache-2.0", "published_at": "2026-01-01",
                                        "valid_from": "2020-01-01", "rows": rows, "status": "draft"}, allow_draft=True))


def row(key, value, unit, sid, geo="GLO", year="-", **q):
    r = {"factor_key": key, "geography": geo, "year": year, "value": value, "unit": unit, "source_row_id": sid}
    if q:
        r["quality"] = q
    return r


def code_of(fn):
    with pytest.raises(CalcError) as e:
        fn()
    return e.value.code


# ---------------- 注册表 / API ----------------

def test_list_methods_covers_05_1_5():
    ids = {(m.method_id, m.family) for m in list_methods()}
    expected = {
        "ghg.scope1.stationary_combustion", "ghg.scope1.mobile_combustion", "ghg.scope1.process", "ghg.scope1.fugitive",
        "ghg.scope1.total", "ghg.scope2.location_based", "ghg.scope2.market_based", "pcf.cradle_to_gate",
        "pcf.allocation", "pcf.data_quality", "cbam.see_simple", "cbam.see_complex", "cbam.see_default",
        "cbam.sefa_default", "cbam.cost_gap", "cbam.threshold_check", "period.aggregate", "period.time_proration",
    }
    assert expected <= {m for m, _ in ids}
    assert ("pcf.cradle_to_gate", "pcf-catenax@0.1.0") in ids
    assert set(FAMILIES) == {"ghg-scope1", "ghg-scope2-location", "ghg-scope2-market", "pcf-iso14067-pact",
                             "pcf-catenax", "cbam-see", "cbam-cost-gap", "cbam-threshold", "period-align"}
    by_id = {m.method_id: m for m in list_methods() if m.status == "available"}
    assert by_id["pcf.cradle_to_gate"].default_digits == 4
    assert by_id["cbam.see_default"].default_digits == 6
    assert by_id["cbam.cost_gap"].default_digits == 2
    assert by_id["ghg.scope1.total"].default_digits == 3
    assert by_id["pcf.data_quality"].default_digits == 2


def test_error_codes_complete():
    for c in ["CALC_UNKNOWN_METHOD", "CALC_METHODOLOGY_UNSUPPORTED", "CALC_INPUT_MISSING", "CALC_PARAM_REQUIRES_DECISION",
              "CALC_FACTOR_MISSING", "CALC_FACTOR_SET_DEPRECATED", "CALC_UNIT_INCOMPATIBLE", "CALC_DEC_INVALID",
              "CALC_PRECISION_OVERFLOW", "CALC_ALLOCATION_IMBALANCE", "CALC_PERIOD_MISMATCH"]:
        assert c in ERROR_CODES
    with pytest.raises(ValueError):
        CalcError("NOT_A_CODE")


def test_unknown_method_and_methodology(fs_example):
    i = [inp("electricity_purchased", "1", "MWh")]
    p = {"consolidation": "operational_control", "grid_region": "DE"}
    assert code_of(lambda: calculate("ghg.scope3.total", "ghg-scope1@1.0.0", fs_example, i, p, "organization", Y2025)) \
        == "CALC_UNKNOWN_METHOD"
    assert code_of(lambda: calculate("ghg.scope2.location_based", "ghg-scope2-location@2.0.0", fs_example, i, p,
                                     "organization", Y2025)) == "CALC_METHODOLOGY_UNSUPPORTED"
    assert code_of(lambda: calculate("pcf.cradle_to_gate", "pcf-catenax@0.1.0", fs_example, i, p,
                                     "product:prod_X", Y2025)) == "CALC_METHODOLOGY_UNSUPPORTED"


def test_inputvalue_and_period_types(fs_example):
    res = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                    [InputValue("electricity_purchased", "ev_01JD8Z1A2B3C4D5E6F7G8H9J0K#v2", "4820.000", "MWh")],
                    {"consolidation": "operational_control", "grid_region": "DE"}, "organization",
                    Period("2025-01-01", "2025-12-31"))
    assert res.value == "1658.08"
    assert res.input_hash == "sha256:75bcf6a8932f70bfaca1053bb3b8a409fa3f80165e51b366d6297425801b3a67"
    assert res.inputs[0]["value"] == "4820"
    assert res.annotations.get("gas_scope_co2_only") is None   # 示例行无 quality
    d = res.to_dict()
    assert set(d) == {"method_id", "methodology_version", "factor_set_version", "engine_version", "inputs", "factors_used",
                      "params", "result", "breakdown", "input_hash", "output_hash", "boundary", "period",
                      "customer_scope", "status"}
    assert d["engine_version"].startswith("belegt-calc@")


def test_dec_validation(fs_example):
    p = {"consolidation": "operational_control", "grid_region": "DE"}
    for bad in ["1e3", "01", "1.", ".5", "NaN", "1234567890123456789", "+1"]:
        assert code_of(lambda: calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                                         [inp("electricity_purchased", bad, "MWh")], p, "organization", Y2025)) \
            == "CALC_DEC_INVALID", bad
    assert code_of(lambda: calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                                     [{"name": "electricity_purchased", "ref": "ev_A#v1", "value": 4820.0, "unit": "MWh"}],
                                     p, "organization", Y2025)) == "CALC_DEC_INVALID"
    assert code_of(lambda: calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                                     [inp("electricity_purchased", "1", "MWh")], {**p, "x": 1}, "organization", Y2025)) \
        == "CALC_DEC_INVALID"


def test_unused_input_rejected(fs_example):
    assert code_of(lambda: calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                                     [inp("electricity_purchased", "1", "MWh"), inp("bananas", "1", "t", "ev_B#v1")],
                                     {"consolidation": "operational_control", "grid_region": "DE"}, "organization",
                                     Y2025)) == "CALC_METHODOLOGY_UNSUPPORTED"


def test_rounding_digits_override_enters_hash(fs_example):
    p = {"consolidation": "operational_control", "grid_region": "DE"}
    a = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                  [inp("electricity_purchased", "4820", "MWh")], p, "organization", Y2025)
    b = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                  [inp("electricity_purchased", "4820", "MWh")], {**p, "rounding_digits": "1"}, "organization", Y2025)
    assert a.value == "1658.08"
    assert b.result["rounding"]["digits"] == 1 and b.value == "1658.1"
    assert a.input_hash != b.input_hash


# ---------------- 因子库 ----------------

def test_deprecated_factor_set_rejected_but_replayable(fs_example):
    p = {"consolidation": "operational_control", "grid_region": "DE"}
    i = [inp("electricity_purchased", "4820", "MWh")]
    calc = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example, i, p, "organization", Y2025)
    dep = deprecate(copy.deepcopy(fs_example), reason="upstream withdrawn", deprecated_at="2026-11-01")
    assert dep.rows_hash == fs_example.rows_hash
    assert code_of(lambda: calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", dep, i, p,
                                     "organization", Y2025)) == "CALC_FACTOR_SET_DEPRECATED"
    rep = replay(calc.to_dict(), dep)
    assert rep.ok and not rep.drift


def test_replay_detects_drift_and_input_tamper(fs_example):
    p = {"consolidation": "operational_control", "grid_region": "DE"}
    calc = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                     [inp("electricity_purchased", "4820", "MWh")], p, "organization", Y2025).to_dict()
    assert replay(calc, fs_example).ok
    drifted = copy.deepcopy(calc)
    drifted["result"]["value"] = "1658.081"
    drifted["output_hash"] = "sha256:" + "0" * 64
    rep = replay(drifted, fs_example)
    assert rep.drift and not rep.ok and rep.diffs
    tampered = copy.deepcopy(calc)
    tampered["inputs"][0]["value"] = "4821"
    rep2 = replay(tampered, fs_example)
    assert not rep2.input_hash_match and not rep2.drift


def test_rows_hash_integrity():
    good = {"id": "fs:x-test@1", "source": "x-test", "license": "MIT", "published_at": "2026-01-01", "valid_from": "2026-01-01",
            "rows": [row("a", "1", "1", "R1")], "status": "active", "rows_hash": "sha256:" + "1" * 64}
    assert code_of(lambda: factor_set_from_dict(good)) == "CALC_FACTOR_SET_INTEGRITY"
    draft = dict(good, status="draft")
    del draft["rows_hash"]
    assert code_of(lambda: factor_set_from_dict(draft)) == "CALC_FACTOR_SET_INTEGRITY"   # 未冻结草稿不可用
    fs = freeze(factor_set_from_dict(draft, allow_draft=True))
    assert fs.status == "active" and fs.rows_hash.startswith("sha256:")
    bad_unit = dict(draft, rows=[row("a", "1", "furlong", "R1")])
    assert code_of(lambda: factor_set_from_dict(bad_unit, allow_draft=True)) == "CALC_UNIT_INCOMPATIBLE"
    bad_val = dict(draft, rows=[row("a", "1.0e2", "1", "R1")])
    assert code_of(lambda: factor_set_from_dict(bad_val, allow_draft=True)) == "CALC_DEC_INVALID"


def test_factor_selection_rules_7_4():
    fs = fs_of([
        row("grid.electricity.location.DE", "0.379", "tCO2e/MWh", "DE23", "DE", "2023"),
        row("grid.electricity.location.DE", "0.353", "tCO2e/MWh", "DE24p", "DE", "2024", status="preliminary"),
        row("grid.electricity.location.DE", "0.360", "tCO2e/MWh", "DE24f", "DE", "2024", status="final"),
        row("grid.electricity.location.DE", "0.344", "tCO2e/MWh", "DE25", "DE", "2025", status="estimated"),
        row("residual_mix.electricity.EU", "0.5", "tCO2e/MWh", "EU-ANY", "EU", "-"),
        row("x.glo", "1", "1", "GLO1", "GLO", "-"),
    ])
    assert select_row(fs, "grid.electricity.location.DE", geography="DE", year=2024).row.source_row_id == "DE24f"  # final 优先
    s = select_row(fs, "grid.electricity.location.DE", geography="DE", year=2026)
    assert s.row.source_row_id == "DE25" and s.year_gap == 1                                                   # 最近不晚于
    with pytest.raises(CalcError) as e:
        select_row(fs, "grid.electricity.location.DE", geography="DE", year=2022)                               # 不用未来数据
    assert e.value.code == "CALC_FACTOR_MISSING"
    assert select_row(fs, "residual_mix.electricity.EU", geography="FR", year=2025).row.source_row_id == "EU-ANY"  # 上级区域
    assert select_row(fs, "x.glo", geography="CN", year=2025).row.source_row_id == "GLO1"                          # GLO
    with pytest.raises(CalcError) as e:
        select_row(fs, "grid.electricity.location.DE", geography="PL", year=2025)
    assert e.value.code == "CALC_FACTOR_MISSING"
    with pytest.raises(CalcError) as e:
        select_row(fs, "nope", geography=None, year=2025)
    assert e.value.code == "CALC_FACTOR_MISSING"


def test_gas_scope_and_year_gap_flags():
    fs = load_factor_set(FIXTURES / "uba-strommix@2026.03.yaml")
    res = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs,
                    [inp("electricity_purchased", "100", "MWh")], {"consolidation": "operational_control",
                                                                   "grid_region": "DE"},
                    "organization", {"start": "2026-01-01", "end": "2026-12-31"})
    assert res.value == "34.4"
    assert res.breakdown_value("gas_scope_co2_only") == "1"
    assert res.breakdown_value("factor_year_gap") == "1"
    assert res.annotations["factor_status"] == {"grid.electricity.location.DE": "estimated"}


# ---------------- Scope 1 ----------------

def _s1(fs, method, inputs, params):
    return calculate(method, "ghg-scope1@1.0.0", fs, inputs, params, "organization", Y2025)


def test_scope1_process_mass_balance_and_ef():
    fs = fs_of([row("process.limestone.co2e.per_t", "0.44", "tCO2e/t", "LS")])
    r = _s1(fs, "ghg.scope1.process", [inp("carbon_in_electrodes", "12", "tC"), inp("carbon_out_steel", "3", "tC", "ev_B#v1")],
            {"consolidation": "operational_control", "process_method": "mass_balance"})
    assert r.value == "33"                      # 44/12 × 9
    r = _s1(fs, "ghg.scope1.process", [inp("carbon_in_x", "1", "tC")],
            {"consolidation": "operational_control", "process_method": "mass_balance"})
    assert r.value == "3.667"                   # 44/12，除法只在输出时执行一次
    r = _s1(fs, "ghg.scope1.process", [inp("limestone_consumed", "100", "t"), inp("limestone_conversion", "90", "%", "ev_C#v1")],
            {"consolidation": "operational_control", "process_method": "emission_factor"})
    assert r.value == "39.6"
    assert code_of(lambda: _s1(fs, "ghg.scope1.process", [inp("limestone_consumed", "1", "t")],
                               {"consolidation": "operational_control"})) == "CALC_PARAM_REQUIRES_DECISION"
    r = _s1(fs, "ghg.scope1.process", [inp("none_confirmed", "0", "tCO2e")], {"consolidation": "operational_control"})
    assert r.value == "0"


def test_scope1_total_aggregation_mode_requires_all_four():
    null = load_factor_set("fs:belegt-null@1")
    ins = [inp("stationary_combustion", "484.309", "tCO2e", "calc_A#v1"), inp("mobile_combustion", "48.06", "tCO2e", "calc_B#v1"),
           inp("fugitive", "28.194", "tCO2e", "calc_C#v1"), inp("process", "0", "tCO2e", "calc_D#v1")]
    r = _s1(null, "ghg.scope1.total", ins, {"consolidation": "operational_control"})
    assert r.value == "560.563" and r.breakdown_value("fugitive_refrigerant") == "28.194"
    assert code_of(lambda: _s1(null, "ghg.scope1.total", ins[:3], {"consolidation": "operational_control"})) \
        == "CALC_INPUT_MISSING"


def test_scope1_mobile_distance_estimate_and_biomass_and_equity():
    fs = fs_of([
        row("vehicle.van.co2e.per_km", "0.2", "kgCO2e/km", "VAN"),
        row("fuel.wood.co2_biogenic.per_mwh_hi", "0.4", "tCO2/MWh", "WOOD-CO2"),
        row("fuel.wood.ch4.per_mwh_hi", "0.01", "kg/MWh", "WOOD-CH4"),
        row("fuel.wood.n2o.per_mwh_hi", "0.004", "kg/MWh", "WOOD-N2O"),
        row("gwp.ar6.ch4_non_fossil", "27", "1", "G1"), row("gwp.ar6.n2o", "273", "1", "G2"),
        row("fuel.diesel.co2e.per_l", "2.67", "kgCO2e/L", "DSL"),
    ])
    r = _s1(fs, "ghg.scope1.mobile_combustion", [inp("van_distance", "10000", "km")], {"consolidation": "operational_control"})
    assert r.value == "2" and r.annotations["is_estimate"] is True
    r = _s1(fs, "ghg.scope1.stationary_combustion", [inp("wood_hi", "1000", "MWh")], S1P)
    assert r.breakdown_value("biogenic_co2") == "400"     # 生物源 CO2 单列
    assert r.value == "1.362"                               # 仅 CH4 0.27 + N2O 1.092
    r = _s1(fs, "ghg.scope1.mobile_combustion", [inp("diesel_volume", "1000", "L"), inp("equity_share", "50", "%", "ev_E#v1")],
            {"consolidation": "equity_share"})
    assert r.value == "1.335"


def test_scope1_refrigerant_mass_balance(fs_example):
    r = _s1(fs_example, "ghg.scope1.fugitive",
            [inp("refrigerant_r_410a_inventory_decrease", "2", "kg"), inp("refrigerant_r_410a_purchased", "10", "kg", "ev_B#v1"),
             inp("refrigerant_r_410a_sold", "1", "kg", "ev_C#v1")], S1P)
    assert r.value == "24.81"                                # 11 kg × 2255.5 = 24.8105 → HALF_EVEN 24.810
    assert code_of(lambda: _s1(fs_example, "ghg.scope1.fugitive",
                               [inp("refrigerant_r_410a_recharged", "1", "kg"),
                                inp("refrigerant_r_410a_purchased", "1", "kg", "ev_B#v1")], S1P)) \
        == "CALC_METHODOLOGY_UNSUPPORTED"


def test_ef_basis_default_is_per_mwh(fs_example):
    r = _s1(fs_example, "ghg.scope1.stationary_combustion", [inp("natural_gas_hi", "2400", "MWh")], S1P)
    assert r.value == "484.309"
    # 选 per_gj_hi 但未排除 CH4/N2O：该 basis 下无 CH4 行 → 不得静默略去
    assert code_of(lambda: _s1(fs_example, "ghg.scope1.stationary_combustion", [inp("natural_gas_hi", "2400", "MWh")],
                               {**S1P, "ef_basis": "per_gj_hi"})) == "CALC_FACTOR_MISSING"


# ---------------- Scope 2 ----------------

def test_market_based_residual_fallback_to_grid():
    fs = fs_of([row("grid.electricity.location.PL", "0.7", "tCO2e/MWh", "PL", "PL", "2025"),
                row("contractual.electricity.goo", "0", "tCO2e/MWh", "GOO", "EU", "-")])
    r = calculate("ghg.scope2.market_based", "ghg-scope2-market@1.0.0", fs,
                  [inp("electricity_purchased", "100", "MWh"), inp("electricity_contractual_renewable", "40", "MWh", "ev_B#v1")],
                  {"consolidation": "operational_control", "grid_region": "PL"}, "organization", Y2025)
    assert r.value == "42" and r.breakdown_value("residual_mix_unavailable") == "1"


# ---------------- PCF ----------------

def test_pcf_secondary_factor_biogenic_and_exemption():
    fs = fs_of([row("material.cardboard.pcf", "1.2", "kgCO2e/kg", "CB")])
    base = [inp("cardboard_consumed", "10", "t"), inp("output_product", "100", "t", "ev_Q#v1")]
    p = {"declared_unit": "kg", "recycling": "cut_off"}
    r = calculate("pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0", fs, base, p, "product:prod_X", Y2025)
    assert r.value == "0.12"
    r = calculate("pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0", fs,
                  base + [inp("biogenic_co2_uptake", "-5", "tCO2e", "ev_U#v1"),
                          inp("exempted_emissions_percent", "2.5", "%", "ev_X#v1")], p, "product:prod_X", Y2025)
    assert r.value == "0.07" and r.breakdown_value("pcf_excluding_biogenic") == "0.12"
    assert code_of(lambda: calculate("pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0", fs,
                                     base + [inp("exempted_emissions_percent", "3.5", "%", "ev_X#v1")], p,
                                     "product:prod_X", Y2025)) == "CALC_METHODOLOGY_UNSUPPORTED"
    assert code_of(lambda: calculate("pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0", fs, base,
                                     {**p, "recycling": "avoided_burden"}, "product:prod_X", Y2025)) \
        == "CALC_METHODOLOGY_UNSUPPORTED"
    assert code_of(lambda: calculate("pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0", fs, base, p, "organization",
                                     Y2025)) == "CALC_METHODOLOGY_UNSUPPORTED"


def test_pcf_allocation_residual_to_largest_share():
    fs = load_factor_set("fs:belegt-null@1")
    r = calculate("pcf.allocation", "pcf-iso14067-pact@1.0.0", fs,
                  [inp("shared_emissions", "100", "tCO2e"), inp("mass_a", "1", "t", "ev_A#v1"), inp("mass_b", "1", "t", "ev_B#v1"),
                   inp("mass_c", "1", "t", "ev_C#v1")], {"allocation": "physical.mass", "product": "a"}, "site:site_X", Y2025)
    vals = {b["name"]: b["value"] for b in r.breakdown}
    assert vals["allocated_a"] == "33.3333333334" and vals["allocated_b"] == "33.3333333333"
    assert vals["rounding_residual"] == "0.0000000001"
    from decimal import Decimal
    assert sum(Decimal(vals[f"allocated_{p}"]) for p in "abc") == Decimal("100")


def test_pcf_allocation_economic_ratio_rule():
    fs = load_factor_set("fs:belegt-null@1")
    ins = [inp("shared_emissions", "100", "tCO2e"), inp("output_a", "10", "t", "ev_A#v1"), inp("output_b", "10", "t", "ev_B#v1"),
           inp("price_a", "600", "EUR/t", "ev_C#v1"), inp("price_b", "100", "EUR/t", "ev_D#v1")]
    r = calculate("pcf.allocation", "pcf-iso14067-pact@1.0.0", fs, ins, {"allocation": "economic", "product": "a"},
                  "site:site_X", Y2025)
    assert r.value == "85.714"
    ins2 = ins[:3] + [inp("price_a", "400", "EUR/t", "ev_C#v1"), inp("price_b", "100", "EUR/t", "ev_D#v1")]
    assert code_of(lambda: calculate("pcf.allocation", "pcf-iso14067-pact@1.0.0", fs, ins2,
                                     {"allocation": "economic", "product": "a"}, "site:site_X", Y2025)) \
        == "CALC_PARAM_REQUIRES_DECISION"


def test_decision_tree_5_4():
    assert decide_allocation(customer_rule="pcf-catenax").outcome == "family_rule"
    assert decide_allocation(can_subdivide=True).allocation == "direct_only"
    assert decide_allocation(can_subdivide=False).closed_question == "CQ-METH-06"
    assert decide_allocation(can_subdivide=False, output_kind="recycled_waste").outcome == "cut_off"
    assert decide_allocation(can_subdivide=False, output_kind="disposal_waste").outcome == "disposal"
    assert decide_allocation(can_subdivide=False, output_kind="co_product", dominant_substitute=True).outcome \
        == "system_expansion"
    d = decide_allocation(can_subdivide=False, output_kind="co_product", dominant_substitute=False, economic_value_ratio="6")
    assert d.allocation == "economic"
    d = decide_allocation(can_subdivide=False, output_kind="co_product", dominant_substitute=False, economic_value_ratio="5")
    assert d.closed_question == "CQ-METH-05"
    d = decide_allocation(can_subdivide=False, output_kind="co_product", dominant_substitute=False, economic_value_ratio="5",
                          unique_physical_key="mass")
    assert d.allocation == "physical.mass"


def test_supplier_pcf_check_4_5():
    ok = check_supplier_pcf({"declaredUnitOfMeasurement": "kilogram", "referencePeriodEnd": "2025-01-01",
                             "ipccCharacterizationFactors": ["AR6"], "primaryDataShare": "60",
                             "pcfIncludingBiogenicUptake": "2.1"},
                            our_unit="kg", our_reference_period_end="2025-12-31", our_gwp_set="ar6",
                            historical_values=["2.0", "2.2"])
    assert ok.accepted and ok.effective_pds == "60"
    bad = check_supplier_pcf({"declaredUnitOfMeasurement": "piece", "referencePeriodEnd": "2021-01-01",
                              "ipccCharacterizationFactors": ["AR5"], "pcfIncludingBiogenicUptake": "9"},
                             our_unit="kg", our_reference_period_end="2025-12-31", our_gwp_set="ar6",
                             historical_values=["2.0", "2.2"])
    rules = {i["rule"] for i in bad.issues}
    assert not bad.accepted and rules == {"unit", "validity", "gwp", "plausibility"}
    assert bad.effective_pds == "0" and bad.plausibility_flag == "outlier"
    piece = check_supplier_pcf({"declaredUnitOfMeasurement": "piece", "productMassPerDeclaredUnit": "0.25",
                                "referencePeriodEnd": "2025-06-01", "ipccCharacterizationFactors": ["AR6"]},
                               our_unit="kg", our_reference_period_end="2025-12-31", our_gwp_set="ar6")
    assert piece.accepted


# ---------------- CBAM ----------------

def test_cbam_see_simple_and_steel_direct_only(fs_cbam):
    r = calculate("cbam.see_simple", "cbam-see@1.0.0", fs_cbam,
                  [inp("attributed_emissions_direct", "1800", "tCO2e"), inp("activity_level", "1000", "t", "ev_B#v1"),
                   inp("electricity_consumed", "500", "MWh", "ev_C#v1"),
                   inp("electricity_emission_factor", "0.6", "tCO2e/MWh", "ev_D#v1")],
                  {"sector": "iron_steel"}, "product:prod_X", Y2025)
    assert r.value == "1.8" and r.breakdown_value("see_indirect") == "0.3" and r.breakdown_value("see_for_certificates") == "1.8"
    assert code_of(lambda: calculate("cbam.see_simple", "cbam-see@1.0.0", fs_cbam,
                                     [inp("attributed_emissions_direct", "1", "tCO2e"), inp("activity_level", "1", "t", "ev_B#v1"),
                                      inp("precursor_x_mass", "1", "t", "ev_C#v1"),
                                      inp("precursor_x_see_direct", "1", "tCO2e/t", "ev_D#v1")],
                                     {}, "product:prod_X", Y2025)) == "CALC_METHODOLOGY_UNSUPPORTED"


def test_cbam_markup_schedule_and_other_countries():
    fs = fs_of([
        row("cbam.default.total.7208.OTHER", "3", "tCO2e/t", "DV-OTH", "OTHER", "2026"),
        row("cbam.markup.iron_steel.2026", "0.1", "1", "MU26", "GLO", "2026"),
        row("cbam.markup.iron_steel.2027", "0.2", "1", "MU27", "GLO", "2027"),
        row("cbam.markup.iron_steel.2028", "0.3", "1", "MU28", "GLO", "2028"),
        row("cbam.markup.fertilisers.2026", "0.01", "1", "MUF", "GLO", "2026"),
        row("cbam.default.total.3102.OTHER", "2", "tCO2e/t", "DV-F", "OTHER", "2026"),
    ])

    def see(year, cn="72081000", sector="iron_steel"):
        return calculate("cbam.see_default", "cbam-see@1.0.0", fs, [inp("import_year", str(year), "1")],
                         {"cn_code": cn, "country": "ZZ", "sector": sector}, "product:prod_X",
                         {"start": f"{year}-01-01", "end": f"{year}-12-31"}).value
    assert see(2026) == "3.3" and see(2027) == "3.6" and see(2028) == "3.9" and see(2031) == "3.9"
    assert see(2030, "31021000", "fertilisers") == "2.02"          # 化肥 2026 年起 1%
    assert code_of(lambda: calculate("cbam.see_default", "cbam-see@1.0.0", fs, [inp("import_year", "2027", "1")],
                                     {"cn_code": "72081000", "country": "ZZ", "sector": "iron_steel"}, "product:prod_X",
                                     {"start": "2026-01-01", "end": "2026-12-31"})) == "CALC_PERIOD_MISMATCH"


def test_cbam_cost_gap_max_floors(fs_price):
    def gap(see_act, cpp):
        return calculate("cbam.cost_gap", "cbam-cost-gap@1.0.0", fs_price,
                         [inp("see_default", "1.2", "tCO2e/t"), inp("sefa_default", "1.33575", "tCO2e/t", "calc_B#v1"),
                          inp("see_actual", see_act, "tCO2e/t", "ev_C#v1"), inp("sefa_actual", "1.3", "tCO2e/t", "ev_D#v1"),
                          inp("import_mass", "100", "t", "ev_E#v1"), inp("carbon_price_paid", cpp, "EUR", "ev_F#v1")],
                         {"quarter": "2026Q3"}, "product:prod_X", {"start": "2026-07-01", "end": "2026-09-30"})
    r = gap("1.5", "100000")          # N_def = max(0, −) = 0；N_act = max(0, 20 − 1214.77…) = 0
    assert r.value == "0" and r.breakdown_value("certificates_default") == "0" and r.breakdown_value("certificates_actual") == "0"


def test_cbam_threshold_warning(fs_cbam):
    r = calculate("cbam.threshold_check", "cbam-threshold@1.0.0", fs_cbam, [inp("lot_1", "40", "t")],
                  {"cn_lot_1": "72081000"}, "organization", {"start": "2026-01-01", "end": "2026-12-31"})
    assert r.breakdown_value("exempt") == "1" and r.breakdown_value("approaching_threshold") == "1"
    assert r.annotations["exempt"] is True


# ---------------- period-align ----------------

def test_period_methods():
    null = load_factor_set("fs:belegt-null@1")
    r = calculate("period.aggregate", "period-align@1.0.0", null,
                  [inp(f"part_{m:02d}", "100", "kWh", f"ev_M{m:02d}#v1") for m in range(1, 13)],
                  {"expected_parts": "12"}, "organization", Y2025)
    assert r.value == "1.2" and r.result["unit"] == "MWh"
    assert code_of(lambda: calculate("period.time_proration", "period-align@1.0.0", null, [inp("value", "365", "MWh")],
                                     {"source_start": "2024-11-15", "source_end": "2025-11-14"}, "organization", Y2025)) \
        == "CALC_PARAM_REQUIRES_DECISION"
    r = calculate("period.time_proration", "period-align@1.0.0", null, [inp("value", "365", "MWh")],
                  {"source_start": "2024-11-15", "source_end": "2025-11-14", "decision_ref": "cq_01JE0X#v2"},
                  "organization", Y2025)
    assert r.value == "318" and r.annotations["is_estimate"] is True
