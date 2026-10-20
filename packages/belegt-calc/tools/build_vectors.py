# Spec: 05 §9（测试向量 TV-01…TV-11）
"""生成 tests/vectors/*.json。

每个情形的 request 按 05 §9（及 work/05_calc/tv.py 参考脚本）逐字构造；生成前先用 DOC 表中**从文档抄录**
的期望值（结果、关键分项、input_hash/output_hash）校验引擎输出——任何不一致都会让脚本失败，
而不是把引擎的值写进向量。breakdown 的完整列表取自引擎，其中文档给出的数值已在 DOC 中逐项断言。

用法：python tools/build_vectors.py   （在 packages/belegt-calc 目录下）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from belegt_calc import CalcError, calculate, load_factor_set  # noqa: E402
from belegt_calc.factors import dump_factor_set, factor_set_from_dict, freeze  # noqa: E402

VEC = ROOT / "tests" / "vectors"
FSDIR = VEC / "fs"
Y2025 = {"start": "2025-01-01", "end": "2025-12-31"}
Q3_2026 = {"start": "2026-07-01", "end": "2026-09-30"}
EX = "fs:belegt-example@2026.10"
EXC = "fs:belegt-example-cbam@2026.10"
EXP = "fs:belegt-example-cbam-price@2026Q3"
AIB = "fs:aib-residual-mix@2025.1"


def I(name, ref, value, unit):  # noqa: E743
    return {"name": name, "ref": ref, "value": value, "unit": unit}


def fsref(fs_id, path=None):
    fs = load_factor_set(str(VEC / path) if path else fs_id)
    return {"id": fs.id, "path": path or f"fs/{fs.id[3:]}.yaml", "rows_hash": fs.rows_hash}


def run(case):
    ref = case["factor_set_ref"]
    p = VEC / ref["path"]
    fs = load_factor_set(str(p) if p.exists() else ref["id"])
    r = case["request"]
    return calculate(r["method_id"], r["methodology_version"], fs, r["inputs"], r.get("params") or {},
                     r["boundary"], r["period"], r.get("customer_scope", "all"))


def build(case, doc: dict):
    """doc: 文档期望 {result, input_hash, output_hash, breakdown: {name: value}, error_code}。"""
    if doc.get("error_code"):
        try:
            res = run(case)
        except CalcError as e:
            assert e.code == doc["error_code"], (case["id"], e.code, e.detail)
            case["expected"] = {"error_code": e.code}
            case["tolerance"] = "0"
            return case
        raise AssertionError(f"{case['id']}: expected {doc['error_code']}, got {res.result}")
    res = run(case)
    assert res.result["value"] == doc["result"], (case["id"], res.result, doc["result"])
    for k in ("input_hash", "output_hash"):
        if k in doc:
            assert getattr(res, k) == doc[k], (case["id"], k, getattr(res, k), doc[k])
    for name, val in doc.get("breakdown", {}).items():
        got = res.breakdown_value(name)
        assert got == val, (case["id"], name, got, val)
    exp = {"result": res.result, "breakdown": res.breakdown, "input_hash": res.input_hash}
    if "output_hash" in doc:
        exp["output_hash"] = res.output_hash
    case["expected"] = exp
    case["tolerance"] = "0"
    if doc.get("note"):
        case["doc_note"] = doc["note"]
    return case


def case(cid, desc, method, mv, fs_id, inputs, params, boundary="organization", period=Y2025, fs_path=None, **extra):
    c = {
        "id": cid,
        "description": desc,
        "request": {"method_id": method, "methodology_version": mv, "factor_set_version": fs_id,
                    "boundary": boundary, "period": period, "params": params, "inputs": inputs},
        "factor_set_ref": fsref(fs_id, fs_path),
    }
    c.update(extra)
    return c


def write(tv, title, cases):
    doc = {"id": tv, "title": title, "spec": f"05 §9 {tv}", "cases": cases}
    (VEC / f"{tv}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(tv, len(cases), "cases")


def make_fixture_fs():
    """测试专用因子库：示例库去掉 fuel.natural_gas.hi_hs_ratio（TV-09 错误向量）。"""
    FSDIR.mkdir(parents=True, exist_ok=True)
    src = yaml.load((ROOT / "src/belegt_calc/fs/belegt-example@2026.10.yaml").read_text(), Loader=yaml.BaseLoader)
    rows = [r for r in src["rows"] if r["source_row_id"] != "EX-NG-HIHS"]
    d = {"id": "fs:belegt-test-no-hihs@2026.10", "source": "belegt-test-no-hihs", "license": "Apache-2.0",
         "published_at": "2026-10-18", "valid_from": "2025-01-01",
         "note": "测试专用：fs:belegt-example@2026.10 去掉 EX-NG-HIHS 行（TV-09 错误向量，05 §9）",
         "rows": rows, "status": "draft"}
    fs = freeze(factor_set_from_dict(d, allow_draft=True))
    (FSDIR / "belegt-test-no-hihs@2026.10.yaml").write_text(
        "# 测试专用因子库（示例值，非官方）。\n" + dump_factor_set(fs), encoding="utf-8")


S1_INPUTS = [I("diesel_volume", "ev_01JE0TV01DS000000000000001#v1", "18000", "L"),
             I("natural_gas_hi", "ev_01JE0TV01NG000000000000001#v1", "2400", "MWh"),
             I("refrigerant_r_410a_recharged", "ev_01JE0TV01RF000000000000001#v1", "12.5", "kg")]
EL = I("electricity_purchased", "ev_01JD8Z1A2B3C4D5E6F7G8H9J0K#v2", "4820", "MWh")
S2P = {"consolidation": "operational_control", "grid_region": "DE"}


def main():
    make_fixture_fs()

    # ---------------- TV-01 ----------------
    write("TV-01", "Scope 1，AR6", [
        build(case("TV-01", "Scope 1 总量（活动数据模式，05 §8.3 展示形态），AR6",
                   "ghg.scope1.total", "ghg-scope1@1.0.0", EX, S1_INPUTS,
                   {"consolidation": "operational_control", "gwp_set": "ar6"}),
              {"result": "560.563",
               "input_hash": "sha256:0952b50523eb3b8eaef1a5e3f481141494fcad7a8912c19fab4bf9558fa3728c",
               "output_hash": "sha256:0213d8ab27d7beb9bf4aefa17f441eae300471cb0ad96d74ec3a670f777b99bc",
               "breakdown": {"stationary_combustion": "484.309152", "mobile_combustion": "48.06",
                             "fugitive_refrigerant": "28.19375", "process": "0", "total_unrounded": "560.562902"}}),
        build(case("TV-01.stationary", "固定燃烧分项（CO2 483.84、CH4 0.23328、N2O 0.235872）",
                   "ghg.scope1.stationary_combustion", "ghg-scope1@1.0.0", EX, [S1_INPUTS[1]],
                   {"consolidation": "operational_control", "gwp_set": "ar6"}),
              {"result": "484.309", "breakdown": {"natural_gas_co2": "483.84", "natural_gas_ch4_co2e": "0.23328",
                                                  "natural_gas_n2o_co2e": "0.235872", "total_unrounded": "484.309152"}}),
        build(case("TV-01.fugitive", "R-410A GWP 2255.5，逸散 28.19375",
                   "ghg.scope1.fugitive", "ghg-scope1@1.0.0", EX, [S1_INPUTS[2]],
                   {"consolidation": "operational_control", "gwp_set": "ar6"}),
              {"result": "28.194", "breakdown": {"r_410a_gwp": "2255.5", "r_410a_co2e": "28.19375"}}),
        build(case("TV-01.err-gwp", "错误向量：缺 gwp_set → CALC_PARAM_REQUIRES_DECISION（CQ-METH-02）",
                   "ghg.scope1.total", "ghg-scope1@1.0.0", EX, S1_INPUTS, {"consolidation": "operational_control"}),
              {"error_code": "CALC_PARAM_REQUIRES_DECISION"}),
        build(case("TV-01.err-consolidation", "错误向量：缺 consolidation → CALC_PARAM_REQUIRES_DECISION（CQ-METH-03）",
                   "ghg.scope1.total", "ghg-scope1@1.0.0", EX, S1_INPUTS, {"gwp_set": "ar6"}),
              {"error_code": "CALC_PARAM_REQUIRES_DECISION"}),
    ])

    # ---------------- TV-02 ----------------
    write("TV-02", "Scope 1，AR5（同输入，客户口径不同）", [
        build(case("TV-02", "Scope 1 总量，AR5", "ghg.scope1.total", "ghg-scope1@1.0.0", EX, S1_INPUTS,
                   {"consolidation": "operational_control", "gwp_set": "ar5"}),
              {"result": "556.415",
               "input_hash": "sha256:9e7df2a53e0fe14c472e8764d4cc23a8e302b5ba54a171f7ba6b69adb5350f84",
               "breakdown": {"stationary_combustion": "484.31088", "fugitive_refrigerant": "24.04375",
                             "total_unrounded": "556.41463"}}),
        build(case("TV-02.stationary", "固定燃烧 AR5：CH4 0.24192、N2O 0.22896",
                   "ghg.scope1.stationary_combustion", "ghg-scope1@1.0.0", EX, [S1_INPUTS[1]],
                   {"consolidation": "operational_control", "gwp_set": "ar5"}),
              {"result": "484.311", "breakdown": {"natural_gas_ch4_co2e": "0.24192", "natural_gas_n2o_co2e": "0.22896",
                                                  "total_unrounded": "484.31088"}}),
        build(case("TV-02.fugitive", "R-410A AR5 GWP 1923.5 → 24.04375", "ghg.scope1.fugitive", "ghg-scope1@1.0.0", EX,
                   [S1_INPUTS[2]], {"consolidation": "operational_control", "gwp_set": "ar5"}),
              {"result": "24.044", "breakdown": {"r_410a_gwp": "1923.5", "r_410a_co2e": "24.04375"}}),
        build(case("TV-02.err-gwp", "错误向量：gwp_set=ar4 → CALC_METHODOLOGY_UNSUPPORTED", "ghg.scope1.total",
                   "ghg-scope1@1.0.0", EX, S1_INPUTS, {"consolidation": "operational_control", "gwp_set": "ar4"}),
              {"error_code": "CALC_METHODOLOGY_UNSUPPORTED"}),
    ])

    # ---------------- TV-03 ----------------
    write("TV-03", "Scope 2 location-based", [
        build(case("TV-03", "4820 MWh × 0.344（UBA 2025 估算值，仅 CO2）", "ghg.scope2.location_based",
                   "ghg-scope2-location@1.0.0", EX, [EL], dict(S2P)),
              {"result": "1658.08",
               "input_hash": "sha256:75bcf6a8932f70bfaca1053bb3b8a409fa3f80165e51b366d6297425801b3a67",
               "breakdown": {"electricity_consumption": "4820", "emissions_unrounded": "1658.08"}}),
        build(case("TV-03.err-unit", "错误向量：电量单位写成 t → CALC_UNIT_INCOMPATIBLE", "ghg.scope2.location_based",
                   "ghg-scope2-location@1.0.0", EX, [dict(EL, unit="t")], dict(S2P)),
              {"error_code": "CALC_UNIT_INCOMPATIBLE"}),
        build(case("TV-03.err-family", "错误向量：方法与 family 不符 → CALC_METHODOLOGY_UNSUPPORTED",
                   "ghg.scope2.location_based", "ghg-scope2-market@1.0.0", EX, [EL], dict(S2P)),
              {"error_code": "CALC_METHODOLOGY_UNSUPPORTED"}),
    ])

    # ---------------- TV-04 ----------------
    write("TV-04", "Scope 2 market-based（复现 03 §5.6.8）", [
        build(case("TV-04", "(4820 − 1200) × 0.56；因子库 fs:aib-residual-mix@2025.1（rows_hash 沿用 03 的占位值）",
                   "ghg.scope2.market_based", "ghg-scope2-market@1.0.0", AIB,
                   [I("electricity_contractual_renewable", "ev_01JD8Z4M5N6P7Q8R9S0T1V2W3X#v2", "1200", "MWh"), EL],
                   dict(S2P)),
              {"result": "2027.2",
               "input_hash": "sha256:fa52b131cff377ebdaec89df24a2a00382ad4837e8dc577c3989a7a302ede446",
               "breakdown": {"residual_mix_consumption": "3620", "emissions_unrounded": "2027.2"},
               "note": "03 §5.6.8 的 output_hash sha256:12a0337f… 对应 rounding.digits=1 且 breakdown 只有两项；"
                       "按 05 §1.2 默认 3 位（digits 覆盖须经 params.rounding_digits 进入 input_hash），"
                       "本引擎的 output_hash 不同，result.value 相同（见 README「文档问题」）。"}),
        build(case("TV-04.err-ref", "错误向量：ref 未钉住版本 → CALC_INPUT_MISSING", "ghg.scope2.market_based",
                   "ghg-scope2-market@1.0.0", AIB,
                   [I("electricity_contractual_renewable", "ev_01JD8Z4M5N6P7Q8R9S0T1V2W3X", "1200", "MWh"), EL],
                   dict(S2P)),
              {"error_code": "CALC_INPUT_MISSING"}),
    ])

    # ---------------- TV-05 ----------------
    t5 = [I("electricity_contractual_renewable", "ev_01JE0TV03G0000000000000001#v1", "1200", "MWh"), EL,
          I("electricity_supplier_specific", "ev_01JE0TV03SP000000000000001#v1", "1000", "MWh")]
    write("TV-05", "Scope 2 market-based，三层合同工具", [
        build(case("TV-05", "绿证 1200（0）+ 供应商特定 1000（0.25）+ 残余组合 2620（0.56）", "ghg.scope2.market_based",
                   "ghg-scope2-market@1.0.0", EX, t5, dict(S2P)),
              {"result": "1717.2",
               "input_hash": "sha256:a4d9e6946ad1f718452a12076d29d27b44c615a77bcb247f363096a1106e236e",
               "breakdown": {"eac_consumption": "1200", "eac_emissions": "0", "supplier_specific_consumption": "1000",
                             "supplier_specific_emissions": "250", "residual_mix_consumption": "2620",
                             "residual_mix_emissions": "1467.2", "emissions_unrounded": "1717.2"}}),
        build(case("TV-05.err-exceed", "错误向量：合同电量超过消耗 → CALC_ALLOCATION_IMBALANCE", "ghg.scope2.market_based",
                   "ghg-scope2-market@1.0.0", EX,
                   [dict(t5[0], value="4000"), EL, t5[2]], dict(S2P)),
              {"error_code": "CALC_ALLOCATION_IMBALANCE"}),
    ])

    # ---------------- TV-06 ----------------
    pa = [I("coil_consumed", "ev_01JE0TV04CA000000000000001#v1", "1320", "t"),
          I("coil_pcf", "ev_01JE0TV04CP000000000000001#v1", "2.1", "kgCO2e/kg"),
          I("machine_hours_product", "ev_01JE0TV04HA000000000000001#v1", "6000", "h"),
          I("machine_hours_site", "ev_01JE0TV04HS000000000000001#v1", "10000", "h"),
          I("output_product", "ev_01JE0TV04QA000000000000001#v1", "1200", "t"),
          I("site_scope1", "calc_01JE0TV04S1000000000000001#v1", "400", "tCO2e"),
          I("site_scope2_market", "calc_01JE0TV04S2000000000000001#v1", "800", "tCO2e")]
    pb = [I("coil_consumed", "ev_01JE0TV04CB000000000000001#v1", "330", "t"),
          I("coil_pcf", "ev_01JE0TV04CP000000000000001#v1", "2.1", "kgCO2e/kg"),
          I("machine_hours_product", "ev_01JE0TV04HB000000000000001#v1", "4000", "h"),
          I("machine_hours_site", "ev_01JE0TV04HS000000000000001#v1", "10000", "h"),
          I("output_product", "ev_01JE0TV04QB000000000000001#v1", "300", "t"),
          I("site_scope1", "calc_01JE0TV04S1000000000000001#v1", "400", "tCO2e"),
          I("site_scope2_market", "calc_01JE0TV04S2000000000000001#v1", "800", "tCO2e")]
    pp = {"allocation": "physical.machine_hours", "declared_unit": "kg", "pact_spec": "3.0.3", "recycling": "cut_off"}
    BA = "product:prod_01JE0TV04PA000000000000001"
    BB = "product:prod_01JE0TV04PB000000000000001"
    dqa = [I("coil_emissions", "calc_01JE0TV04MA000000000000001#v1", "2772", "tCO2e"),
           I("coil_pds", "ev_01JE0TV04CP000000000000001#v1", "60", "%"),
           I("coil_dqr_technological", "ev_01JE0TV04CP000000000000001#v1", "2", "1"),
           I("coil_dqr_geographical", "ev_01JE0TV04CP000000000000001#v1", "2", "1"),
           I("coil_dqr_temporal", "ev_01JE0TV04CP000000000000001#v1", "1", "1"),
           I("site_emissions", "calc_01JE0TV04AA000000000000001#v1", "720", "tCO2e"),
           I("site_pds", "ev_01JE0TV04DQ000000000000001#v1", "100", "%"),
           I("site_dqr_technological", "ev_01JE0TV04DQ000000000000001#v1", "1", "1"),
           I("site_dqr_geographical", "ev_01JE0TV04DQ000000000000001#v1", "1", "1"),
           I("site_dqr_temporal", "ev_01JE0TV04DQ000000000000001#v1", "1", "1")]
    dqb = [dict(dqa[0], ref="calc_01JE0TV04MB000000000000001#v1", value="693")] + dqa[1:5] + \
          [dict(dqa[5], ref="calc_01JE0TV04AB000000000000001#v1", value="480")] + dqa[6:]
    alloc_in = [I("shared_emissions", "calc_01JE0TV04SH000000000000001#v1", "1200", "tCO2e"),
                I("machine_hours_a", "ev_01JE0TV04HA000000000000001#v1", "6000", "h"),
                I("machine_hours_b", "ev_01JE0TV04HB000000000000001#v1", "4000", "h")]
    write("TV-06", "PCF 分摊（产品 A，cut-off + 机时分摊）", [
        build(case("TV-06", "产品 A：(2772 + 720) / 1200 = 2.91 kgCO2e/kg", "pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0",
                   EX, pa, dict(pp), boundary=BA),
              {"result": "2.91",
               "input_hash": "sha256:01b59ef8ea044d572b17cc6347510b91b5df742e685ddd42745e4eb9953e0eff",
               "breakdown": {"material_emissions": "2772", "allocation_share": "0.6", "allocated_shared_emissions": "720",
                             "total_emissions": "3492", "pcf_excluding_biogenic": "2.91"}}),
        build(case("TV-06.B", "产品 B：(693 + 480) / 300 = 3.91", "pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0",
                   EX, pb, dict(pp), boundary=BB),
              {"result": "3.91", "breakdown": {"material_emissions": "693", "allocation_share": "0.4",
                                               "allocated_shared_emissions": "480"}}),
        build(case("TV-06.dq-A", "产品 A 数据质量：PDS 68.25%，DQR 1.79 / 1.79 / 1", "pcf.data_quality",
                   "pcf-iso14067-pact@1.0.0", EX, dqa, {"pact_spec": "3.0.3"}, boundary=BA),
              {"result": "68.25", "breakdown": {"pds_unrounded": "68.2474226804", "dqr_technological": "1.79",
                                                "dqr_geographical": "1.79", "dqr_temporal": "1"}}),
        build(case("TV-06.dq-B", "产品 B 数据质量：PDS 895.8/1173 → 76.37%", "pcf.data_quality",
                   "pcf-iso14067-pact@1.0.0", EX, dqb, {"pact_spec": "3.0.3"}, boundary=BB),
              {"result": "76.37", "breakdown": {"pds_unrounded": "76.368286445"}}),
        build(case("TV-06.allocation", "共用排放 1200 按机时 A 6000 / B 4000：A 720、B 480，守恒逐位相等",
                   "pcf.allocation", "pcf-iso14067-pact@1.0.0", EX, alloc_in,
                   {"allocation": "physical.machine_hours", "product": "a"}, boundary="site:site_01JE0TV04ST000000000000001"),
              {"result": "720", "breakdown": {"allocated_a": "720", "allocated_b": "480", "rounding_residual": "0"}}),
        build(case("TV-06.err-allocation", "错误向量：有共用排放但无分摊键选择 → CALC_PARAM_REQUIRES_DECISION（CQ-METH-05）",
                   "pcf.cradle_to_gate", "pcf-iso14067-pact@1.0.0", EX, pa,
                   {k: v for k, v in pp.items() if k != "allocation"}, boundary=BA),
              {"error_code": "CALC_PARAM_REQUIRES_DECISION"}),
    ])

    # ---------------- TV-07 ----------------
    t7 = [I("activity_level", "ev_01JE0TV05AL000000000000001#v1", "20000", "t"),
          I("attributed_emissions_direct", "ev_01JE0TV05DE000000000000001#v1", "5200", "tCO2e"),
          I("electricity_consumed", "ev_01JE0TV05EC000000000000001#v1", "4000", "MWh"),
          I("electricity_emission_factor", "ev_01JE0TV05EF000000000000001#v1", "0.581", "tCO2e/MWh"),
          I("precursor_crude_steel_mass", "ev_01JE0TV05PM000000000000001#v1", "21000", "t"),
          I("precursor_crude_steel_see_direct", "ev_01JE0TV05PD000000000000001#v1", "1.85", "tCO2e/t"),
          I("precursor_crude_steel_see_indirect", "ev_01JE0TV05P1000000000000001#v1", "0.12", "tCO2e/t")]
    B7 = "product:prod_01JE0TV05P0000000000000001"
    p7 = {"cn_code": "72091600", "sector": "iron_steel"}
    t7bd = {"ee_inp_mat_direct": "38850", "see_direct": "2.2025", "attributed_emissions_indirect": "2324",
            "ee_inp_mat_indirect": "2520", "see_indirect": "0.2422", "specific_electricity_consumption": "0.2",
            "see_for_certificates": "2.2025"}
    write("TV-07", "CBAM 实际值，复杂货物（冷轧钢）", [
        build(case("TV-07", "SEE_dir = (5200 + 21000×1.85)/20000 = 2.2025", "cbam.see_complex", "cbam-see@1.0.0", EXC, t7,
                   dict(p7, component="direct"), boundary=B7),
              {"result": "2.2025", "breakdown": t7bd}),
        build(case("TV-07.indirect", "SEE_ind = (4000×0.581 + 21000×0.12)/20000 = 0.2422", "cbam.see_complex",
                   "cbam-see@1.0.0", EXC, t7, dict(p7, component="indirect"), boundary=B7),
              {"result": "0.2422", "breakdown": t7bd}),
        build(case("TV-07.sec", "SEC = 4000/20000 = 0.2 MWh/t", "cbam.see_complex", "cbam-see@1.0.0", EXC, t7,
                   dict(p7, component="specific_electricity_consumption"), boundary=B7),
              {"result": "0.2", "breakdown": t7bd}),
        build(case("TV-07.err-al", "错误向量：缺产量 activity_level → CALC_INPUT_MISSING", "cbam.see_complex",
                   "cbam-see@1.0.0", EXC, t7[1:], dict(p7, component="direct"), boundary=B7),
              {"error_code": "CALC_INPUT_MISSING"}),
    ])

    # ---------------- TV-08 ----------------
    B8 = "product:prod_01JE0TV06P0000000000000001"
    yr = I("import_year", "ev_01JE0TV06YR000000000000001#v1", "2026", "1")
    gap_in = [I("carbon_price_paid", "ev_01JE0TV06CP000000000000001#v1", "14040", "EUR"),
              I("import_mass", "ev_01JE0TV06MS000000000000001#v1", "1200", "t"),
              I("sefa_actual", "ev_01JE0TV06SA000000000000001#v1", "1.3", "tCO2e/t"),
              I("sefa_default", "calc_01JE0TV06SD000000000000001#v1", "1.33575", "tCO2e/t"),
              I("see_actual", "ev_01JE0TV06SE000000000000001#v1", "1.95", "tCO2e/t"),
              I("see_default", "calc_01JE0TV06DV000000000000001#v1", "2.849", "tCO2e/t")]
    gp = {"cn_code": "72081000", "quarter": "2026Q3", "sector": "iron_steel"}
    write("TV-08", "CBAM 默认值 + 成本差（2026Q3）", [
        build(case("TV-08.see_default", "SEE_def = 2.590 × 1.10 = 2.849", "cbam.see_default", "cbam-see@1.0.0", EXC, [yr],
                   {"cn_code": "72081000", "country": "XX", "sector": "iron_steel"}, boundary=B8, period=Q3_2026),
              {"result": "2.849",
               "input_hash": "sha256:65b9f40586c3c611e89840df278cb779a21bff0a7c6671c9d80b3e424bc63780",
               "breakdown": {"default_value_total": "2.59", "markup": "0.1"}}),
        build(case("TV-08.sefa_default", "SEFA_def = 0.975 × 1 × 1.370 = 1.33575", "cbam.sefa_default", "cbam-see@1.0.0",
                   EXC, [yr], {"cn_code": "72081000"}, boundary=B8, period=Q3_2026),
              {"result": "1.33575", "breakdown": {"cbam_factor": "0.975", "cscf": "1", "benchmark": "1.37"}}),
        build(case("TV-08.cost_gap", "cost_gap = (N_def − N_act) × P = 99 315.29 €（05 §6.6 修正公式）", "cbam.cost_gap",
                   "cbam-cost-gap@1.0.0", EXP, gap_in, dict(gp), boundary=B8, period=Q3_2026),
              {"result": "99315.29",
               "input_hash": "sha256:8ee5ebc0ad31181a23af55667021e985ca58cdf18669dcadb094ed77b2985058",
               "breakdown": {"certificates_default": "1815.9", "certificates_actual_gross": "780",
                             "carbon_price_deduction_certificates": "170.5539358601",
                             "cost_default": "149484.888", "cost_actual": "50169.6", "cost_gap_unrounded": "99315.288"}}),
        build(case("TV-08.see_default-2027", "同一货物 2027 年进口：加价 20% → 3.108", "cbam.see_default", "cbam-see@1.0.0",
                   EXC, [dict(yr, value="2027")], {"cn_code": "72081000", "country": "XX", "sector": "iron_steel"},
                   boundary=B8, period={"start": "2027-01-01", "end": "2027-03-31"}),
              {"result": "3.108", "breakdown": {"markup": "0.2"}}),
        build(case("TV-08.err-period", "错误向量：quarter 与计算期间不符 → CALC_PERIOD_MISMATCH", "cbam.cost_gap",
                   "cbam-cost-gap@1.0.0", EXP, gap_in, dict(gp, quarter="2026Q2"), boundary=B8, period=Q3_2026),
              {"error_code": "CALC_PERIOD_MISMATCH"}),
        build(case("TV-08.err-price", "错误向量：2026Q4 价格未公布（因子库无行）→ CALC_FACTOR_MISSING", "cbam.cost_gap",
                   "cbam-cost-gap@1.0.0", EXP, gap_in, dict(gp, quarter="2026Q4"), boundary=B8,
                   period={"start": "2026-10-01", "end": "2026-12-31"}),
              {"error_code": "CALC_FACTOR_MISSING"}),
    ])

    # ---------------- TV-09 ----------------
    gas = I("natural_gas_hs", "ev_01JE0TV07NG000000000000001#v1", "1234567", "kWh")
    p9 = {"consolidation": "operational_control", "ef_basis": "per_gj_hi", "gases": "co2", "grid_region": "DE"}
    write("TV-09", "单位换算边界（燃气 Hs 账单）", [
        build(case("TV-09", "1 234 567 kWh(Hs) → 4444.4412 GJ × 0.903 × 0.0561 = 225.148", "ghg.scope1.stationary_combustion",
                   "ghg-scope1@1.0.0", EX, [gas], dict(p9),
                   unit_checks=[{"value": "1234567", "from": "kWh", "to": "GJ", "expected": "4444.4412"},
                                {"value": "4444.4412", "from": "GJ", "to": "MWh", "expected": "1234.567"},
                                {"value": "3600000", "from": "GJ", "to": "MWh", "expected": "1000000"},
                                {"value": "1234.567", "from": "MWh", "to": "GJ", "expected": "4444.4412"}]),
              {"result": "225.148", "breakdown": {"natural_gas_energy_hs": "4444.4412", "natural_gas_energy_hi": "4013.3304036",
                                                  "natural_gas_co2": "225.147835642"}}),
        build(case("TV-09.counterexample", "反例：把 Hs 当 Hi（漏掉 Hs/Hi）→ 249.333（偏高 10.7%），仅用于说明",
                   "ghg.scope1.stationary_combustion", "ghg-scope1@1.0.0", EX,
                   [dict(gas, name="natural_gas_hi", value="4444.4412", unit="GJ")], dict(p9)),
              {"result": "249.333"}),
        build(case("TV-09.err-hihs", "错误向量：因子库缺 fuel.natural_gas.hi_hs_ratio → CALC_FACTOR_MISSING（不得默认 1）",
                   "ghg.scope1.stationary_combustion", "ghg-scope1@1.0.0", "fs:belegt-test-no-hihs@2026.10", [gas],
                   dict(p9), fs_path="fs/belegt-test-no-hihs@2026.10.yaml"),
              {"error_code": "CALC_FACTOR_MISSING"}),
    ])

    # ---------------- TV-10 ----------------
    p10 = {"consolidation": "operational_control", "grid_factor_key": "test.tie_factor", "grid_region": "DE"}

    def e10(v, ref="ev_01JE0TV08EL000000000000001#v1", name="electricity_purchased"):
        return I(name, ref, v, "MWh")
    write("TV-10", "舍入边界（ROUND_HALF_EVEN）", [
        build(case("TV-10.tie-down", "2.5 × 0.805 = 2.0125 → HALF_EVEN 2.012（HALF_UP 会得 2.013）",
                   "ghg.scope2.location_based", "ghg-scope2-location@1.0.0", EX, [e10("2.5")], dict(p10)),
              {"result": "2.012", "breakdown": {"emissions_unrounded": "2.0125"}}),
        build(case("TV-10.tie-up", "2.7 × 0.805 = 2.1735 → 2.174", "ghg.scope2.location_based",
                   "ghg-scope2-location@1.0.0", EX, [e10("2.7")], dict(p10)),
              {"result": "2.174", "breakdown": {"emissions_unrounded": "2.1735"}}),
        build(case("TV-10.zero", "0 → \"0\"（不是 \"0.000\"）", "ghg.scope2.location_based", "ghg-scope2-location@1.0.0",
                   EX, [e10("0")], dict(p10)),
              {"result": "0", "breakdown": {"emissions_unrounded": "0"}}),
        build(case("TV-10.negative", "期间更正差额 −2.5 MWh → −2.0125 → −2.012", "ghg.scope2.location_based",
                   "ghg-scope2-location@1.0.0", EX, [e10("-2.5")], dict(p10, delta="true")),
              {"result": "-2.012", "breakdown": {"emissions_unrounded": "-2.0125"}}),
        build(case("TV-10.sum-then-round", "两条 2.0125 分项同一计算内相加 → 4.025（先舍入再加会错得 4.024）",
                   "ghg.scope2.location_based", "ghg-scope2-location@1.0.0", EX,
                   [e10("2.5", name="electricity_purchased_a"),
                    e10("2.5", ref="ev_01JE0TV08EB000000000000001#v1", name="electricity_purchased_b")], dict(p10)),
              {"result": "4.025", "breakdown": {"electricity_purchased_a_emissions": "2.0125",
                                                "electricity_purchased_b_emissions": "2.0125",
                                                "emissions_unrounded": "4.025"}}),
        build(case("TV-10.err-negative", "错误向量：负值未声明 delta → CALC_DEC_INVALID", "ghg.scope2.location_based",
                   "ghg-scope2-location@1.0.0", EX, [e10("-2.5")], dict(p10)),
              {"error_code": "CALC_DEC_INVALID"}),
        build(case("TV-10.err-dec", "错误向量：科学计数法 1e3 → CALC_DEC_INVALID", "ghg.scope2.location_based",
                   "ghg-scope2-location@1.0.0", EX, [e10("1e3")], dict(p10)),
              {"error_code": "CALC_DEC_INVALID"}),
    ])

    # ---------------- TV-11 ----------------
    Y26 = {"start": "2026-01-01", "end": "2026-12-31"}
    lots = [I("lot_1", "ev_01JE0TV09L1000000000000001#v1", "18", "t"),
            I("lot_2", "ev_01JE0TV09L2000000000000001#v1", "25", "t"),
            I("lot_3", "ev_01JE0TV09L3000000000000001#v1", "6.5", "t"),
            I("lot_4", "ev_01JE0TV09L4000000000000001#v1", "0.5", "t"),
            I("lot_h2", "ev_01JE0TV09H2000000000000001#v1", "3", "t")]
    p11 = {"cn_lot_1": "72081000", "cn_lot_2": "76061200", "cn_lot_3": "73181500", "cn_lot_4": "76169910",
           "cn_lot_h2": "28041000"}
    write("TV-11", "CBAM 50 t 门槛", [
        build(case("TV-11", "18 + 25 + 6.5 + 0.5 = 50.000 t（氢 3 t 不计）→ exempt=true（不超过 50 t）",
                   "cbam.threshold_check", "cbam-threshold@1.0.0", EXC, lots, dict(p11), period=Y26),
              {"result": "50", "breakdown": {"threshold": "50", "excluded_mass": "3", "exempt": "1"}}),
        build(case("TV-11.exceed", "再进口 0.001 t → 50.001 t → exempt=false", "cbam.threshold_check", "cbam-threshold@1.0.0",
                   EXC, lots + [I("lot_5", "ev_01JE0TV09L5000000000000001#v1", "0.001", "t")],
                   dict(p11, cn_lot_5="72085120"), period=Y26),
              {"result": "50.001", "breakdown": {"exempt": "0"}}),
        build(case("TV-11.err-period", "错误向量：非日历年期间 → CALC_PERIOD_MISMATCH", "cbam.threshold_check",
                   "cbam-threshold@1.0.0", EXC, lots, dict(p11), period=Q3_2026),
              {"error_code": "CALC_PERIOD_MISMATCH"}),
        build(case("TV-11.err-cn", "错误向量：批次缺 CN 编码 → CALC_INPUT_MISSING", "cbam.threshold_check",
                   "cbam-threshold@1.0.0", EXC, lots, {k: v for k, v in p11.items() if k != "cn_lot_h2"}, period=Y26),
              {"error_code": "CALC_INPUT_MISSING"}),
    ])


if __name__ == "__main__":
    main()
