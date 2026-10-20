# Spec: 05 §9（测试向量 TV-01…TV-11，容差 0）；03 §5.6（串联实例哈希）
"""测试向量逐位复现 + 文档中抄录的期望值与哈希的独立断言（不依赖向量文件里的 expected）。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from belegt_calc import calculate, input_hash, load_factor_set, output_hash, rows_hash
from belegt_calc.dec import Rat, round_str
from belegt_calc.vectors import iter_cases, verify_case

from calc_testutil import FIXTURES, VECTORS

FILES = sorted(VECTORS.glob("TV-*.json"))
CASES = [(p, c) for p in FILES for c in iter_cases(json.loads(p.read_text(encoding="utf-8")))]


def test_all_eleven_vectors_present():
    assert [p.stem for p in FILES] == [f"TV-{i:02d}" for i in range(1, 12)]


@pytest.mark.parametrize("path,case", CASES, ids=[c["id"] for _, c in CASES])
def test_vector(path, case):
    r = verify_case(case, path.parent, path.name)
    assert r.ok, r.failures


def _case(tv: str, cid: str) -> dict:
    for c in iter_cases(json.loads((VECTORS / f"{tv}.json").read_text(encoding="utf-8"))):
        if c["id"] == cid:
            return c
    raise KeyError(cid)


def _run(c: dict):
    r = c["request"]
    ref = c["factor_set_ref"]
    p = VECTORS / ref["path"]
    fs = load_factor_set(str(p) if p.exists() else ref["id"])
    return calculate(r["method_id"], r["methodology_version"], fs, r["inputs"], r["params"], r["boundary"], r["period"])


# ---- 文档值（从 05 §9 / §8.3 / 03 §5.6 抄录），与向量文件独立 ----
DOC = {
    ("TV-01", "TV-01"): ("560.563", "sha256:0952b50523eb3b8eaef1a5e3f481141494fcad7a8912c19fab4bf9558fa3728c"),
    ("TV-02", "TV-02"): ("556.415", "sha256:9e7df2a53e0fe14c472e8764d4cc23a8e302b5ba54a171f7ba6b69adb5350f84"),
    ("TV-03", "TV-03"): ("1658.08", "sha256:75bcf6a8932f70bfaca1053bb3b8a409fa3f80165e51b366d6297425801b3a67"),
    ("TV-04", "TV-04"): ("2027.2", "sha256:fa52b131cff377ebdaec89df24a2a00382ad4837e8dc577c3989a7a302ede446"),
    ("TV-05", "TV-05"): ("1717.2", "sha256:a4d9e6946ad1f718452a12076d29d27b44c615a77bcb247f363096a1106e236e"),
    ("TV-06", "TV-06"): ("2.91", "sha256:01b59ef8ea044d572b17cc6347510b91b5df742e685ddd42745e4eb9953e0eff"),
    ("TV-06", "TV-06.B"): ("3.91", None),
    ("TV-06", "TV-06.dq-A"): ("68.25", None),
    ("TV-06", "TV-06.dq-B"): ("76.37", None),
    ("TV-07", "TV-07"): ("2.2025", None),
    ("TV-07", "TV-07.indirect"): ("0.2422", None),
    ("TV-07", "TV-07.sec"): ("0.2", None),
    ("TV-08", "TV-08.see_default"): ("2.849", "sha256:65b9f40586c3c611e89840df278cb779a21bff0a7c6671c9d80b3e424bc63780"),
    ("TV-08", "TV-08.sefa_default"): ("1.33575", None),
    ("TV-08", "TV-08.cost_gap"): ("99315.29", "sha256:8ee5ebc0ad31181a23af55667021e985ca58cdf18669dcadb094ed77b2985058"),
    ("TV-08", "TV-08.see_default-2027"): ("3.108", None),
    ("TV-09", "TV-09"): ("225.148", None),
    ("TV-09", "TV-09.counterexample"): ("249.333", None),
    ("TV-10", "TV-10.tie-down"): ("2.012", None),
    ("TV-10", "TV-10.tie-up"): ("2.174", None),
    ("TV-10", "TV-10.zero"): ("0", None),
    ("TV-10", "TV-10.negative"): ("-2.012", None),
    ("TV-10", "TV-10.sum-then-round"): ("4.025", None),
    ("TV-11", "TV-11"): ("50", None),
    ("TV-11", "TV-11.exceed"): ("50.001", None),
}


@pytest.mark.parametrize("key", list(DOC), ids=[k[1] for k in DOC])
def test_doc_values(key):
    value, ih = DOC[key]
    res = _run(_case(*key))
    assert res.result["value"] == value
    if ih:
        assert res.input_hash == ih


def test_tv04_input_hash_equals_03_5_6_8():
    # 03 §5.6.1 原文 M（逐字）
    m = json.loads(
        '{"boundary":"organization","factor_set_rows_hash":"sha256:35f78fd009ad6f51b4973c5a50e77fdf25d81dfaa58395010609c6a7e3b1b460",'
        '"factor_set_version":"fs:aib-residual-mix@2025.1","inputs":[{"name":"electricity_contractual_renewable",'
        '"ref":"ev_01JD8Z4M5N6P7Q8R9S0T1V2W3X#v2","unit":"MWh","value":"1200"},{"name":"electricity_purchased",'
        '"ref":"ev_01JD8Z1A2B3C4D5E6F7G8H9J0K#v2","unit":"MWh","value":"4820"}],"method_id":"ghg.scope2.market_based",'
        '"methodology_version":"ghg-scope2-market@1.0.0","params":{"consolidation":"operational_control","grid_region":"DE"},'
        '"period":{"end":"2025-12-31","start":"2025-01-01"}}')
    assert input_hash(m) == "sha256:fa52b131cff377ebdaec89df24a2a00382ad4837e8dc577c3989a7a302ede446"
    assert _run(_case("TV-04", "TV-04")).input_hash == input_hash(m)
    # 旧 family 名（03 修订前）复现 sha256:1a0cbaa1…（05 §1.4）
    m["methodology_version"] = "ghg-protocol-scope2@1.0.0"
    assert input_hash(m).startswith("sha256:1a0cbaa1")


def test_03_5_6_8_output_hash_reproducible_from_its_own_result():
    """03 §5.6.8 的 output_hash 按其文中 result（digits=1）与 breakdown 可复现（验证 output_hash 实现）。"""
    result = {"value": "2027.2", "unit": "tCO2e", "rounding": {"mode": "half_even", "digits": 1}}
    bd = [{"name": "residual_mix_consumption", "value": "3620", "unit": "MWh"},
          {"name": "emissions_unrounded", "value": "2027.2", "unit": "tCO2e"}]
    assert output_hash(result, bd) == "sha256:12a0337f066e03cf2dfa6be6bc7263bf2d5ac3b4a476a1997464a7b24ae7611c"


def test_tv01_output_hash_matches_8_3():
    res = _run(_case("TV-01", "TV-01"))
    assert res.output_hash == "sha256:0213d8ab27d7beb9bf4aefa17f441eae300471cb0ad96d74ec3a670f777b99bc"
    assert len(res.factors_used) == 10      # 05 §8.3：完整应列出 TV-01 用到的全部 10 行
    assert {"factor_key": "gwp.ar6.hfc_125", "value": "3740", "unit": "1", "source_row_id": "EX-GWP6-HFC125"} in res.factors_used


def test_tv01_tv02_divergence_4_148():
    a = Decimal(_run(_case("TV-01", "TV-01")).value)
    b = Decimal(_run(_case("TV-02", "TV-02")).value)
    assert a - b == Decimal("4.148")


def test_tv06_conservation():
    a = _run(_case("TV-06", "TV-06"))
    b = _run(_case("TV-06", "TV-06.B"))
    tot = sum(Decimal(r.breakdown_value(n)) for r in (a, b) for n in ("material_emissions", "allocated_shared_emissions"))
    assert tot == Decimal("4665") == Decimal("3465") + Decimal("1200")


def test_tv08_doc_table_values():
    res = _run(_case("TV-08", "TV-08.cost_gap"))
    bd = {b["name"]: b["value"] for b in res.breakdown}
    assert round_str(Rat.of(bd["certificates_actual"]), 6) == "609.446064"
    assert round_str(Rat.of(bd["cost_default"]), 2) == "149484.89"
    assert round_str(Rat.of(bd["cost_actual"]), 2) == "50169.6"            # 50 169.60 €
    assert round_str(Rat.of(bd["cost_gap_per_tonne"]), 2) == "82.76"
    # 05 §6.6：任务书示意式 (SEE_def×(1+μ) − SEE_act) × M × P − 抵扣 = 74 766.82 €（错误，缺 SEFA 差且抵扣符号反）
    naive = (Rat.of("2.849") - Rat.of("1.95")) * Rat.of("1200") * Rat.of("82.32") - Rat.of("14040")
    assert round_str(naive, 2) == "74766.82"
    # 展开式（两个 max 都不触发）：(DV(1+μ) − SEE_act)·M·P − (SEFA_def − SEFA_act)·M·P + CPP
    expanded = ((Rat.of("2.849") - Rat.of("1.95")) * 1200 * Rat.of("82.32")
                - (Rat.of("1.33575") - Rat.of("1.3")) * 1200 * Rat.of("82.32") + Rat.of("14040"))
    assert round_str(expanded, 2) == res.value == "99315.29"


def test_tv10_round_then_sum_is_wrong():
    assert round_str(Rat.of("2.0125"), 3) == "2.012"
    assert str(Decimal("2.012") + Decimal("2.012")) == "4.024"           # 违反 §1.2 的错误实现
    assert _run(_case("TV-10", "TV-10.sum-then-round")).value == "4.025"


def test_example_factor_set_hashes():
    assert load_factor_set("fs:belegt-example@2026.10").rows_hash == \
        "sha256:25cd7633e6907491a142f0c6cf90224f4403b25fb49957bacb67987bb71faaf9"
    assert len(load_factor_set("fs:belegt-example@2026.10").rows) == 21
    assert load_factor_set("fs:belegt-example-cbam@2026.10").rows_hash == \
        "sha256:27358e8514ba616585981f7d8e9e3c04f136eb1f2053dd95f6ba92431d2916e5"
    assert load_factor_set("fs:belegt-example-cbam-price@2026Q3").rows_hash == \
        "sha256:03cd93e50c409df6741575ee3b7a72b2c72553d451baa6b71d88c83a66a90191"


def test_uba_example_rows_hash_7_1():
    fs = load_factor_set(FIXTURES / "uba-strommix@2026.03.yaml")
    assert fs.rows_hash == rows_hash([r.to_dict() for r in fs.rows]) == \
        "sha256:12af011e9ff09a062c0a581a253237c565316de013c42e4c527e91e74afce8d0"


def test_aib_placeholder_rows_hash_is_flagged():
    fs = load_factor_set("fs:aib-residual-mix@2025.1")
    assert fs.rows_hash.startswith("sha256:35f78fd0")
    assert fs.warnings and "placeholder" in fs.warnings[0]
